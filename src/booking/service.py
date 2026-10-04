"""学校团体预约核心服务。

职责（对应领域契约四条不变量）：

* 团体拆分容量：团体按可配置的单组上限拆分为若干讲解组，名额账本按
  「暂占 + 已确认」合并计数，任何调整都在校验后原子提交。
* 多方案有限暂占：一次申请可给多个备选日期，全部满足的方案才暂占，
  暂占数量受 ``max_holds`` 限制，不满足的方案留拒绝原因但不占位。
* 候补递补一致性：名额释放时按优先级（无障碍刚需 > 申请时间 > 编号）
  原子递补，被跳过的较小团体也会记录原因；超时使用可注入时钟。
* 租户预约隔离：学校侧接口必须带 school_id，越权访问返回 404。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from itertools import count
from math import ceil

from .clock import Clock, SystemClock
from .errors import DomainError, bad_request, conflict, not_found
from .models import Decision, Option, ReservationRequest, Session, WaitlistEntry


def _require_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise bad_request(f"{field}不能为空")
    return value.strip()


def _require_int(value: object, field: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int,):
        raise bad_request(f"{field}必须是整数")
    if value < minimum:
        raise bad_request(f"{field}不能小于{minimum}")
    return value


class BookingService:
    """线程安全的预约受理服务（内存存储，单进程原子）。"""

    def __init__(
        self,
        clock: Clock | None = None,
        confirm_window: timedelta = timedelta(hours=24),
        waitlist_ttl: timedelta = timedelta(days=7),
        max_group_size: int = 20,
        max_holds: int = 3,
    ) -> None:
        self._clock = clock or SystemClock()
        self._confirm_window = confirm_window
        self._waitlist_ttl = waitlist_ttl
        self._max_group_size = max_group_size
        self._max_holds = max_holds

        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}
        self._requests: dict[str, ReservationRequest] = {}
        # session_id -> {request_id: seats}，暂占与确认分账，使用时合并。
        self._held: dict[str, dict[str, int]] = {}
        self._confirmed: dict[str, dict[str, int]] = {}
        # (school_id, idempotency_key) -> request_id
        self._idempotency: dict[tuple[str, str], str] = {}
        self._req_seq = count(1)
        self._wl_seq = count(1)
        self._decision_seq = count(1)

    # ============ 场次配置（运营侧） ============
    def add_session(
        self,
        session_id: str,
        date: str,
        label: str,
        capacity: int,
        accessibility: set[str] | frozenset[str] | None = None,
    ) -> Session:
        session_id = _require_str(session_id, "场次编号")
        _require_str(date, "日期")
        _require_str(label, "时段")
        capacity = _require_int(capacity, "容量")
        with self._lock:
            if session_id in self._sessions:
                raise conflict(f"场次{session_id}已存在")
            session = Session(
                id=session_id,
                date=date,
                label=label,
                capacity=capacity,
                accessibility=frozenset(accessibility or ()),
            )
            self._sessions[session_id] = session
            self._held[session_id] = {}
            self._confirmed[session_id] = {}
            return session

    def list_sessions(self, include_usage: bool = True) -> list[dict]:
        with self._lock:
            self._expire_due()
            result = []
            for session in self._sessions.values():
                used = self._used(session.id)
                result.append(
                    session.to_dict(used if include_usage else None)
                )
            return result

    def _used(self, session_id: str) -> int:
        return sum(self._held[session_id].values()) + sum(
            self._confirmed[session_id].values()
        )

    def _available(self, session_id: str) -> int:
        return self._sessions[session_id].capacity - self._used(session_id)

    # ============ 学校侧：申请 ============
    def apply(
        self,
        *,
        school_id: str,
        school_name: str,
        contact: str,
        headcount: int,
        grades: str,
        theme: str,
        accessibility: list[str] | set[str] | None,
        candidate_session_ids: list[str],
        idempotency_key: str | None = None,
        waitlist_if_full: bool = False,
    ) -> tuple[ReservationRequest, bool]:
        """提交预约意向，返回 (预约, 是否为重复请求重放)。

        每个候选场次独立评估：满足无障碍且有名额的方案按申请顺序暂占，
        最多暂占 ``max_holds`` 个；其余方案记录拒绝原因。
        """
        school_id = _require_str(school_id, "学校标识")
        _require_str(school_name, "学校名称")
        _require_str(contact, "联系人")
        headcount = _require_int(headcount, "人数")
        _require_str(grades, "年龄段")
        _require_str(theme, "主题")
        needs = frozenset(accessibility or [])
        if not isinstance(candidate_session_ids, list) or not candidate_session_ids:
            raise bad_request("至少提供一个备选场次")
        if len(candidate_session_ids) != len(set(candidate_session_ids)):
            raise bad_request("备选场次不能重复")

        with self._lock:
            self._expire_due()

            if idempotency_key:
                replay_id = self._idempotency.get((school_id, idempotency_key))
                if replay_id is not None:
                    request = self._requests[replay_id]
                    if request.status in ("已取消",):
                        # 同一请求键在取消后重提：允许新建。
                        pass
                    else:
                        self._log(
                            request,
                            "重复请求",
                            "命中幂等键，返回既有预约而未重复暂占名额",
                            {"idempotency_key": idempotency_key},
                        )
                        return request, True
                # 键与其他已取消预约绑定过也直接复用槽位（下面新建后覆盖）

            now = self._clock.now()
            request = ReservationRequest(
                id=f"R{next(self._req_seq):04d}",
                school_id=school_id,
                school_name=school_name,
                contact=contact,
                headcount=headcount,
                grades=grades,
                theme=theme,
                accessibility=needs,
                group_count=ceil(headcount / self._max_group_size),
                created_at=now,
                idempotency_key=idempotency_key,
            )

            held = 0
            for sid in candidate_session_ids:
                session = self._sessions.get(sid)
                if session is None:
                    request.options[sid] = Option(
                        session_id=sid,
                        seats=0,
                        group_count=0,
                        state="已拒绝",
                        reason="场次不存在或已下架",
                    )
                    continue
                missing = needs - session.accessibility
                if missing:
                    request.options[sid] = Option(
                        session_id=sid,
                        seats=0,
                        group_count=0,
                        state="已拒绝",
                        reason=f"场次不满足无障碍需求：{'、'.join(sorted(missing))}",
                    )
                    continue
                if held >= self._max_holds:
                    request.options[sid] = Option(
                        session_id=sid,
                        seats=headcount,
                        group_count=request.group_count,
                        state="已拒绝",
                        reason=f"暂占方案已达上限{self._max_holds}个，避免多方案过度占用资源",
                    )
                    continue
                available = self._available(sid)
                if available < headcount:
                    request.options[sid] = Option(
                        session_id=sid,
                        seats=headcount,
                        group_count=request.group_count,
                        state="已拒绝",
                        reason=f"剩余名额{available}不足{headcount}人",
                    )
                    continue
                # 满足条件：原子暂占
                self._held[sid][request.id] = (
                    self._held[sid].get(request.id, 0) + headcount
                )
                request.options[sid] = Option(
                    session_id=sid,
                    seats=headcount,
                    group_count=request.group_count,
                    state="已暂占",
                )
                held += 1

            self._requests[request.id] = request
            if idempotency_key:
                self._idempotency[(school_id, idempotency_key)] = request.id

            self._log(
                request,
                "申请",
                f"共{len(candidate_session_ids)}个备选方案，暂占{held}个，拒绝{len(candidate_session_ids) - held}个",
                {
                    "headcount": headcount,
                    "group_count": request.group_count,
                    "max_group_size": self._max_group_size,
                    "held_session_ids": [o.session_id for o in request.held_options()],
                },
            )

            if held:
                request.status = "待确认"
                request.confirm_deadline = now + self._confirm_window
            elif waitlist_if_full:
                # 全部方案落空且学校同意候补：为「仅因容量不足」的场次排队。
                for option in request.options.values():
                    if option.reason and option.reason.startswith("剩余名额"):
                        self._join_waitlist_locked(request, option.session_id, headcount,
                                                   "申请时名额不足，自动进入候补")
                request.status = "候补中" if request.active_waitlist() else "筹备"
            else:
                request.status = "筹备"
            return request, False

    # ============ 学校侧：确认 / 缩减 / 取消 / 改期 / 候补 ============
    def confirm(
        self, request_id: str, session_id: str, school_id: str
    ) -> ReservationRequest:
        """确认某个暂占方案；同申请的其他暂占立即释放并触发递补。"""
        with self._lock:
            self._expire_due()
            request = self._get_owned(request_id, school_id)
            option = request.options.get(session_id)
            if option is None or option.state != "已暂占":
                raise conflict("只能确认处于暂占状态的备选方案", "无可确认方案")
            if request.status != "待确认":
                raise conflict(f"当前状态为{request.status}，不能确认")

            other_held = [
                o.session_id for o in request.held_options() if o.session_id != session_id
            ]
            self._held[session_id].pop(request.id, None)
            self._confirmed[session_id][request.id] = option.seats
            option.state = "已确认"
            option.reason = None
            for sid in other_held:
                self._release_hold(request, sid, "确认其他方案，释放该备选暂占")

            # 确认后排他：该申请在所有场次的候补均不再需要。
            for entry in list(request.active_waitlist()):
                self._cancel_waitlist_locked(entry, f"已确认场次{session_id}")

            request.status = "已排定"
            request.confirm_deadline = None
            self._log(request, "确认", f"已确认场次{session_id}，{option.seats}人",
                      {"session_id": session_id, "released": other_held})

            for sid in other_held:
                self._promote_locked(sid)
            return request

    def reduce(
        self, request_id: str, new_headcount: int, school_id: str
    ) -> ReservationRequest:
        """缩减人数：待确认时同步缩减所有暂占，已排定时缩减确认名额并递补。"""
        new_headcount = _require_int(new_headcount, "缩减后人数")
        with self._lock:
            self._expire_due()
            request = self._get_owned(request_id, school_id)
            if new_headcount >= request.headcount:
                raise bad_request("缩减后人数必须小于当前人数，如需增加请改期或重新申请")
            new_groups = ceil(new_headcount / self._max_group_size)

            if request.status == "待确认":
                freed_sessions: list[str] = []
                for option in request.held_options():
                    sid = option.session_id
                    self._held[sid][request.id] = new_headcount
                    option.seats = new_headcount
                    option.group_count = new_groups
                    freed_sessions.append(sid)
            elif request.status == "已排定":
                confirmed = request.confirmed_option()
                sid = confirmed.session_id
                self._confirmed[sid][request.id] = new_headcount
                confirmed.seats = new_headcount
                confirmed.group_count = new_groups
                freed_sessions = [sid]
            else:
                raise conflict(f"当前状态为{request.status}，不能缩减")

            # 同步缩减该申请尚未生效的候补名额，避免日后按旧人数递补。
            for entry in request.active_waitlist():
                entry.seats = new_headcount
                entry.group_count = new_groups

            old = request.headcount
            request.headcount = new_headcount
            request.group_count = new_groups
            self._log(
                request,
                "缩减",
                f"人数由{old}缩减为{new_headcount}，团体数{new_groups}个",
                {"old_headcount": old, "new_headcount": new_headcount},
            )
            for sid in freed_sessions:
                self._promote_locked(sid)
            return request

    def cancel(self, request_id: str, school_id: str, reason: str = "学校主动取消") -> ReservationRequest:
        with self._lock:
            self._expire_due()
            request = self._get_owned(request_id, school_id)
            if request.status in ("已取消", "已结算"):
                raise conflict(f"当前状态为{request.status}，无需取消")
            released = self._release_all_locked(request, reason)
            for entry in list(request.active_waitlist()):
                self._cancel_waitlist_locked(entry, reason)
            request.status = "已取消"
            request.confirm_deadline = None
            self._log(request, "取消", reason, {"released_sessions": released})
            for sid in released:
                self._promote_locked(sid)
            return request

    def reschedule(
        self,
        request_id: str,
        new_session_ids: list[str],
        school_id: str,
        waitlist_if_full: bool = True,
    ) -> ReservationRequest:
        """改期：释放原方案，按新备选场次重新暂占；原已确认而新方案全满时转候补。"""
        if not isinstance(new_session_ids, list) or not new_session_ids:
            raise bad_request("改期至少提供一个新场次")
        if len(new_session_ids) != len(set(new_session_ids)):
            raise bad_request("备选场次不能重复")
        with self._lock:
            self._expire_due()
            request = self._get_owned(request_id, school_id)
            if request.status not in ("待确认", "已排定"):
                raise conflict(f"当前状态为{request.status}，不能改期")

            was_confirmed = request.status == "已排定"
            # 旧候补条目与改期前的意向绑定，先全部取消，再按新方案重新排队。
            for entry in list(request.active_waitlist()):
                self._cancel_waitlist_locked(entry, "改期重新选择场次")
            # 释放全部既有暂占/确认，但保留历史方案记录为已失效。
            released = self._release_all_locked(request, "改期释放原场次")

            now = self._clock.now()
            held = 0
            for sid in new_session_ids:
                session = self._sessions.get(sid)
                if session is None:
                    request.options[sid] = Option(
                        session_id=sid, seats=0, group_count=0,
                        state="已拒绝", reason="场次不存在或已下架",
                    )
                    continue
                missing = request.accessibility - session.accessibility
                if missing:
                    request.options[sid] = Option(
                        session_id=sid, seats=0, group_count=0,
                        state="已拒绝",
                        reason=f"场次不满足无障碍需求：{'、'.join(sorted(missing))}",
                    )
                    continue
                if held >= self._max_holds:
                    request.options[sid] = Option(
                        session_id=sid, seats=request.headcount,
                        group_count=request.group_count, state="已拒绝",
                        reason=f"暂占方案已达上限{self._max_holds}个",
                    )
                    continue
                if self._available(sid) < request.headcount:
                    request.options[sid] = Option(
                        session_id=sid, seats=request.headcount,
                        group_count=request.group_count, state="已拒绝",
                        reason=f"剩余名额{self._available(sid)}不足{request.headcount}人",
                    )
                    continue
                self._held[sid][request.id] = request.headcount
                request.options[sid] = Option(
                    session_id=sid, seats=request.headcount,
                    group_count=request.group_count, state="已暂占",
                )
                held += 1

            held_sessions = [o.session_id for o in request.held_options()]
            if was_confirmed and held == 1:
                # 唯一新方案直接重新确认，保持已排定。
                sid = held_sessions[0]
                option = request.options[sid]
                self._held[sid].pop(request.id, None)
                self._confirmed[sid][request.id] = option.seats
                option.state = "已确认"
                option.reason = None
                request.status = "已排定"
                request.confirm_deadline = None
            elif held:
                request.status = "待确认"
                request.confirm_deadline = now + self._confirm_window
            elif waitlist_if_full:
                for option in request.options.values():
                    if (option.state == "已拒绝"
                            and option.reason and option.reason.startswith("剩余名额")):
                        self._join_waitlist_locked(request, option.session_id,
                                                   request.headcount, "改期时新场次已满，自动候补")
                request.status = "候补中" if request.active_waitlist() else "筹备"
                request.confirm_deadline = None
            else:
                request.status = "筹备"
                request.confirm_deadline = None

            self._log(
                request, "改期",
                f"释放原场次{released}，新方案暂占{held}个"
                + ("，并已重新确认" if was_confirmed and held == 1 else ""),
                {"released": released, "held": held_sessions, "waitlist_if_full": waitlist_if_full},
            )
            for sid in released:
                self._promote_locked(sid)
            return request

    def join_waitlist(
        self, request_id: str, session_id: str, school_id: str, note: str = ""
    ) -> WaitlistEntry:
        with self._lock:
            self._expire_due()
            request = self._get_owned(request_id, school_id)
            if session_id not in self._sessions:
                raise bad_request("场次不存在或已下架")
            session = self._sessions[session_id]
            missing = request.accessibility - session.accessibility
            if missing:
                raise bad_request(
                    f"场次不满足无障碍需求：{'、'.join(sorted(missing))}，不能候补"
                )
            option = request.options.get(session_id)
            if option and option.state in ("已暂占", "已确认"):
                raise conflict("该场次已在您的暂占/确认方案中，无需候补")
            if any(e.session_id == session_id and e.state == "候补中"
                   for e in request.waitlist.values()):
                raise conflict("已在该场次的候补队列中")
            if request.status in ("已取消", "已排定", "执行中", "已结算"):
                raise conflict(f"当前状态为{request.status}，不能加入候补")
            if self._available(session_id) >= request.headcount:
                raise conflict("该场次仍有名额，请直接在申请中选择该场次，无需候补")
            entry = self._join_waitlist_locked(request, session_id, request.headcount,
                                               note or "学校主动候补")
            if request.status == "筹备":
                request.status = "候补中"
            self._log(request, "候补", f"进入场次{session_id}候补队列，{entry.seats}人",
                      {"waitlist_id": entry.id, "base_priority": entry.base_priority})
            return entry

    def leave_waitlist(
        self, request_id: str, session_id: str, school_id: str
    ) -> ReservationRequest:
        with self._lock:
            self._expire_due()
            request = self._get_owned(request_id, school_id)
            for entry in request.waitlist.values():
                if entry.session_id == session_id and entry.state == "候补中":
                    self._cancel_waitlist_locked(entry, "学校退出候补")
                    self._log(request, "候补", f"退出场次{session_id}候补",
                              {"waitlist_id": entry.id})
                    break
            else:
                raise not_found("候补记录不存在", "无候补记录")
            if not request.active_waitlist() and request.status == "候补中":
                request.status = "筹备"
            return request

    # ============ 超时处理（可注入时钟驱动） ============
    def expire_due(self) -> list[str]:
        """处理所有到期的确认与候补，返回受影响的预约编号。"""
        with self._lock:
            return self._expire_due()

    def _expire_due(self) -> list[str]:
        now = self._clock.now()
        affected: list[str] = []

        # 1) 确认期限超时：释放暂占，候补条目一并失效。
        for request in list(self._requests.values()):
            if (request.status == "待确认"
                    and request.confirm_deadline is not None
                    and now >= request.confirm_deadline):
                deadline = request.confirm_deadline
                released = self._release_all_locked(
                    request, f"超过确认期限{deadline.isoformat()}未确认"
                )
                for entry in list(request.active_waitlist()):
                    entry.state = "已失效"
                    entry.expired_at = now
                request.status = "已取消"
                request.confirm_deadline = None
                self._log(request, "超时",
                          "确认期限超时，暂占全部释放，预约取消",
                          {"deadline": deadline.isoformat(), "released_sessions": released})
                affected.append(request.id)
                for sid in released:
                    self._promote_locked(sid)

        # 2) 候补条目 TTL 超时。
        for request in list(self._requests.values()):
            for entry in list(request.waitlist.values()):
                if entry.state != "候补中":
                    continue
                if now >= entry.created_at + self._waitlist_ttl:
                    entry.state = "已失效"
                    entry.expired_at = now
                    self._log(request, "超时",
                              f"场次{entry.session_id}候补等待超过"
                              f"{int(self._waitlist_ttl.total_seconds())}秒，候补失效",
                              {"waitlist_id": entry.id, "session_id": entry.session_id})
                    affected.append(request.id)
            if request.status == "候补中" and not request.active_waitlist():
                request.status = "已取消"
                self._log(request, "超时", "全部候补均已失效，预约取消", {})

        return affected

    # ============ 候补递补 ============
    def _promote_locked(self, session_id: str) -> list[str]:
        """场次有空位时按优先级递补，返回递补成功的预约编号。

        优先级：无障碍刚需学校优先；同级按申请时间先到先得；再按预约编号。
        队首团体若塞不进剩余名额会被跳过并留痕，后续较小团体可递补。
        """
        queue = sorted(
            (
                e
                for request in self._requests.values()
                for e in request.waitlist.values()
                if e.session_id == session_id and e.state == "候补中"
            ),
            key=lambda e: (e.base_priority, e.created_at, _seq_of(e.request_id)),
        )
        promoted: list[str] = []
        skipped: list[dict] = []
        for entry in queue:
            request = self._requests[entry.request_id]
            available = self._available(session_id)
            if available < entry.seats:
                skipped.append({
                    "request": request,
                    "waitlist_id": entry.id,
                    "request_id": entry.request_id,
                    "seats": entry.seats,
                    "available": available,
                    "reason": "剩余名额不足以容纳该团体，跳过并继续考察后续较小团体",
                })
                self._log(
                    request, "候补",
                    f"场次{session_id}出现空位但未能递补：当前剩余{available}个名额，"
                    f"该团体需要{entry.seats}个；按规则跳过并继续考察后续较小团体",
                    {"session_id": session_id, "waitlist_id": entry.id,
                     "available": available, "seats": entry.seats,
                     "base_priority": entry.base_priority, "note": entry.note},
                )
                continue

            # 递补只新增一个暂占方案；该申请在其他场次的暂占与候补仍然保留，
            # 由学校在确认期内统一选择；确认或超时时再原子释放其余方案。
            option = request.options.get(session_id)
            if option is None:
                request.options[session_id] = Option(
                    session_id=session_id, seats=entry.seats,
                    group_count=entry.group_count, state="已暂占",
                )
            else:
                option.state = "已暂占"
                option.seats = entry.seats
                option.group_count = entry.group_count
                option.reason = None
            self._held[session_id][request.id] = entry.seats

            entry.state = "已递补"
            entry.promoted_at = self._clock.now()
            request.status = "待确认"
            request.confirm_deadline = self._clock.now() + self._confirm_window
            promoted.append(request.id)
            self._log(
                request, "候补",
                f"场次{session_id}释放名额，候补递补为暂占，{entry.seats}人，"
                f"请于{request.confirm_deadline.isoformat()}前确认",
                {
                    "waitlist_id": entry.id,
                    "session_id": session_id,
                    "base_priority": entry.base_priority,
                    "queue_position_before": len(promoted) + len(skipped),
                    "skipped_before": [
                        {"request_id": s["request_id"], "seats": s["seats"],
                         "available": s["available"]}
                        for s in skipped
                    ],
                    "note": entry.note,
                },
            )
        return promoted

    # ============ 查询（学校侧受租户隔离约束） ============
    def get_request(self, request_id: str, school_id: str) -> ReservationRequest:
        with self._lock:
            self._expire_due()
            return self._get_owned(request_id, school_id)

    def list_my_requests(self, school_id: str) -> list[dict]:
        with self._lock:
            self._expire_due()
            return [
                r.to_dict()
                for r in self._requests.values()
                if r.school_id == school_id
            ]

    # ============ 运营侧：履约状态流转 ============
    def mark_in_session(self, request_id: str) -> ReservationRequest:
        """场馆管理员标记团体到场，已排定 → 执行中。"""
        with self._lock:
            self._expire_due()
            request = self._requests.get(request_id)
            if request is None:
                raise not_found("预约不存在")
            if request.status != "已排定":
                raise conflict(f"当前状态为{request.status}，只有已排定的预约可标记执行中")
            request.status = "执行中"
            self._log(request, "运营", "团体到场，预约进入执行中", {})
            return request

    def mark_settled(self, request_id: str) -> ReservationRequest:
        """活动统筹员结算，执行中 → 已结算。"""
        with self._lock:
            request = self._requests.get(request_id)
            if request is None:
                raise not_found("预约不存在")
            if request.status != "执行中":
                raise conflict(f"当前状态为{request.status}，只有执行中的预约可结算")
            request.status = "已结算"
            self._log(request, "运营", "活动结束并结算", {})
            return request

    # ============ 运营侧：全局视图与解释 ============
    def list_all_requests(self) -> list[dict]:
        with self._lock:
            self._expire_due()
            return [r.to_dict() for r in self._requests.values()]

    def waitlist_overview(self, session_id: str | None = None) -> list[dict]:
        with self._lock:
            self._expire_due()
            entries = [
                e.to_dict()
                for request in self._requests.values()
                for e in request.waitlist.values()
                if session_id is None or e.session_id == session_id
            ]
            entries.sort(key=lambda d: (d["session_id"], d["state"] != "候补中",
                                        d["base_priority"], d["created_at"]))
            return entries

    def explain(self, request_id: str) -> dict:
        """运营解释：给出预约每个方案被接受/拒绝/递补的完整决策链。"""
        with self._lock:
            self._expire_due()
            request = self._requests.get(request_id)
            if request is None:
                raise not_found("预约不存在")
            option_lines = []
            for option in request.options.values():
                session = self._sessions.get(option.session_id)
                line = {
                    "session_id": option.session_id,
                    "state": option.state,
                    "seats": option.seats,
                    "group_count": option.group_count,
                    "reason": option.reason,
                }
                if session is not None:
                    line["session"] = session.to_dict(self._used(session.id))
                option_lines.append(line)
            return {
                "request_id": request.id,
                "school_id": request.school_id,
                "school_name": request.school_name,
                "status": request.status,
                "headcount": request.headcount,
                "group_count": request.group_count,
                "accessibility": sorted(request.accessibility),
                "confirm_deadline": request.confirm_deadline.isoformat()
                if request.confirm_deadline else None,
                "options": option_lines,
                "waitlist": [e.to_dict() for e in request.waitlist.values()],
                "decision_chain": [d.to_dict() for d in request.decisions],
            }

    # ============ 内部工具 ============
    def _get_owned(self, request_id: str, school_id: str) -> ReservationRequest:
        request = self._requests.get(request_id)
        # 不区分「不存在」与「他人所有」，统一 404，满足租户预约隔离。
        if request is None or request.school_id != school_id:
            raise not_found("预约不存在或不属于该学校")
        return request

    def _join_waitlist_locked(
        self,
        request: ReservationRequest,
        session_id: str,
        seats: int,
        note: str,
    ) -> WaitlistEntry:
        entry = WaitlistEntry(
            id=f"W{next(self._wl_seq):04d}",
            request_id=request.id,
            school_id=request.school_id,
            session_id=session_id,
            seats=seats,
            group_count=ceil(seats / self._max_group_size),
            state="候补中",
            created_at=self._clock.now(),
            base_priority=0 if request.accessibility else 1,
            note=note,
        )
        request.waitlist[entry.id] = entry
        return entry

    def _cancel_waitlist_locked(self, entry: WaitlistEntry, reason: str) -> None:
        entry.state = "已取消"
        entry.expired_at = self._clock.now()
        entry.note = (entry.note + "；" if entry.note else "") + reason

    def _release_hold(self, request: ReservationRequest, session_id: str, reason: str) -> None:
        self._held[session_id].pop(request.id, None)
        option = request.options.get(session_id)
        if option and option.state == "已暂占":
            option.state = "已失效"
            option.reason = reason

    def _release_confirmed(self, request: ReservationRequest, session_id: str, reason: str) -> None:
        self._confirmed[session_id].pop(request.id, None)
        option = request.options.get(session_id)
        if option and option.state == "已确认":
            option.state = "已失效"
            option.reason = reason

    def _release_all_locked(self, request: ReservationRequest, reason: str) -> list[str]:
        released: list[str] = []
        for sid, holders in list(self._held.items()):
            if request.id in holders:
                holders.pop(request.id, None)
                option = request.options.get(sid)
                if option and option.state == "已暂占":
                    option.state = "已失效"
                    option.reason = reason
                released.append(sid)
        for sid, holders in list(self._confirmed.items()):
            if request.id in holders:
                holders.pop(request.id, None)
                option = request.options.get(sid)
                if option and option.state == "已确认":
                    option.state = "已失效"
                    option.reason = reason
                released.append(sid)
        return released

    def _log(
        self,
        request: ReservationRequest,
        trigger: str,
        message: str,
        related: dict | None = None,
    ) -> None:
        request.decisions.append(
            Decision(
                seq=next(self._decision_seq),
                time=self._clock.now(),
                trigger=trigger,
                code="决策留痕",
                message=message,
                related=related or {},
            )
        )


def _seq_of(request_id: str) -> int:
    try:
        return int(request_id.lstrip("R"))
    except ValueError:
        return 10**9
