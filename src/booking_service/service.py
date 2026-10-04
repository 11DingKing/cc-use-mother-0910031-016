"""预约受理核心领域服务。

所有变更在同一把锁内完成，保证多方案暂占、确认、缩减、候补、改期、
重复请求等操作对场次名额的调整是原子的。
"""
from __future__ import annotations

import threading
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from .clock import Clock, SystemClock
from .errors import BookingError
from .models import (
    EVENT_TYPES,
    PLAN_ALTERNATIVE,
    PLAN_CONFIRMED,
    PLAN_EXPIRED,
    PLAN_HELD,
    PLAN_REJECTED,
    PLAN_RELEASED,
    STATUS_CANCELLED,
    STATUS_DRAFT,
    STATUS_IN_PROGRESS,
    STATUS_PENDING_CONFIRMATION,
    STATUS_REJECTED,
    STATUS_SCHEDULED,
    STATUS_SETTLED,
    STATUS_WAITLISTED,
    AuditEvent,
    GroupSplit,
    Plan,
    Request,
    School,
    Session,
    WaitlistEntry,
)


class BookingService:
    def __init__(
        self,
        clock: Optional[Clock] = None,
        confirm_ttl: timedelta = timedelta(hours=24),
        max_held_plans: int = 2,
    ) -> None:
        self._clock = clock or SystemClock()
        self._confirm_ttl = confirm_ttl
        self._max_held_plans = max_held_plans
        self._lock = threading.RLock()

        self._schools: dict[str, School] = {}
        self._sessions: dict[str, Session] = {}
        self._requests: dict[str, Request] = {}
        self._idempotency: dict[tuple[str, str], str] = {}
        self._waitlist: dict[str, list[WaitlistEntry]] = defaultdict(list)
        self._audit: list[AuditEvent] = []
        self._audit_seq = 0

    # ------------------------------------------------------------------ 基础

    @property
    def clock(self) -> Clock:
        return self._clock

    def _now(self) -> datetime:
        return self._clock.now()

    def register_school(self, school_id: str, name: str) -> School:
        with self._lock:
            school = School(school_id=school_id, name=name)
            self._schools[school_id] = school
            return school

    def create_session(
        self,
        session_id: str,
        date: str,
        time_slot: str,
        theme: str,
        capacity: int,
        accessible: bool = False,
    ) -> Session:
        with self._lock:
            if capacity <= 0:
                raise BookingError("INVALID_CAPACITY", "场次容量必须为正数")
            session = Session(
                session_id=session_id,
                date=date,
                time_slot=time_slot,
                theme=theme,
                capacity=capacity,
                accessible=accessible,
            )
            self._sessions[session_id] = session
            return session

    def _log(self, event_type: str, request_id: str, message: str, **data) -> AuditEvent:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"未知事件类型：{event_type}")
        self._audit_seq += 1
        event = AuditEvent(
            seq=self._audit_seq,
            at=self._now(),
            event_type=event_type,
            request_id=request_id,
            message=message,
            data=data,
        )
        self._audit.append(event)
        return event

    def _decide(self, request: Request, code: str, message: str, **data) -> None:
        request.decisions.append(
            {"at": self._now().isoformat(), "code": code, "message": message, "data": data}
        )

    # ---------------------------------------------------------- 容量核算

    def _committed_seats(self, session_id: str) -> int:
        """全场已确认名额。"""
        total = 0
        for request in self._requests.values():
            for plan in request.plans:
                if plan.state == PLAN_CONFIRMED:
                    total += plan.held_seats.get(session_id, 0)
        return total

    def _held_seats(self, session_id: str, *, exclude_request: Optional[str] = None) -> int:
        """全场待确认暂占名额（确认后不再以暂占计）。"""
        total = 0
        for request in self._requests.values():
            if exclude_request is not None and request.request_id == exclude_request:
                continue
            for plan in request.plans:
                if plan.state == PLAN_HELD:
                    total += plan.held_seats.get(session_id, 0)
        return total

    def session_availability(self, session_id: str) -> dict:
        """运营视图：场次容量、已确认、暂占、可售。"""
        with self._lock:
            session = self._require_session(session_id)
            confirmed = self._committed_seats(session_id)
            held = self._held_seats(session_id)
            return {
                "session_id": session_id,
                "capacity": session.capacity,
                "confirmed": confirmed,
                "held": held,
                "available": session.capacity - confirmed - held,
            }

    def _pack_splits(
        self,
        splits: list[GroupSplit],
        sessions: list[Session],
        *,
        exclude_request: Optional[str] = None,
        pinned: Optional[tuple[str, str]] = None,
    ) -> tuple[Optional[dict[str, str]], dict[str, int], dict[str, object]]:
        """把各分团贪心装入当日场次。

        pinned=(split_id, session_id) 要求某分团必须落到指定场次（候补递补用）。
        返回 (allocations 或 None, 每场次占用, 失败原因)。
        """
        allocations: dict[str, str] = {}
        used: dict[str, int] = defaultdict(int)
        reason: dict[str, object] = {}

        def fits(session: Session, size: int) -> bool:
            available = (
                session.capacity
                - self._committed_seats(session.session_id)
                - self._held_seats(session.session_id, exclude_request=exclude_request)
                - used.get(session.session_id, 0)
            )
            return available >= size

        def remaining(session: Session) -> int:
            return (
                session.capacity
                - self._committed_seats(session.session_id)
                - self._held_seats(session.session_id, exclude_request=exclude_request)
                - used.get(session.session_id, 0)
            )

        # 调用方已按主题过滤场次；无障碍为分团硬性约束
        ordered = sorted(splits, key=lambda s: s.size, reverse=True)
        for split in ordered:
            pool = [
                s for s in sessions
                if (not split.accessibility_required or s.accessible)
            ]
            if pinned and split.split_id == pinned[0]:
                pool = [s for s in pool if s.session_id == pinned[1]]
            # 最佳适应；无无障碍需求的分团最后才使用无障碍场次，为刚需团体保留
            accessible_last = not split.accessibility_required
            pool.sort(key=lambda s: (1 if accessible_last and s.accessible else 0, remaining(s)))
            target = next((s for s in pool if fits(s, split.size)), None)
            if target is None:
                if not pool:
                    code = "NO_ACCESSIBLE_SESSION"
                    message = "当日没有满足无障碍需求的场次"
                else:
                    code = "CAPACITY_SHORTAGE"
                    message = f"分团 {split.split_id}（{split.size} 人）无场次可容纳"
                reason = {
                    "code": code,
                    "message": message,
                    "split_id": split.split_id,
                    "required_seats": split.size,
                    "candidate_sessions": [s.session_id for s in pool],
                }
                return None, {}, reason
            allocations[split.split_id] = target.session_id
            used[target.session_id] += split.size
        return allocations, dict(used), {}

    # ---------------------------------------------------------- 申请

    def apply(
        self,
        *,
        school_id: str,
        idempotency_key: str,
        people_count: int,
        age_band: str,
        theme: str,
        preferred_dates: list[str],
        split_sizes: Optional[list[int]] = None,
        accessibility_required: bool = False,
        priority: int = 0,
    ) -> Request:
        """学校提交预约意向；多日期产生多方案，仅有限方案暂占资源。"""
        with self._lock:
            self._lazy_sweep()
            self._require_school(school_id)
            if not preferred_dates:
                raise BookingError("INVALID_REQUEST", "至少需要一个备选日期")
            if people_count <= 0:
                raise BookingError("INVALID_REQUEST", "人数必须为正数")

            key = (school_id, idempotency_key)
            if key in self._idempotency:
                original = self._requests[self._idempotency[key]]
                self._log(
                    "duplicate_request",
                    original.request_id,
                    f"学校 {school_id} 使用相同幂等键重复提交，返回原意向 {original.request_id}",
                    idempotency_key=idempotency_key,
                )
                self._decide(
                    original,
                    "DUPLICATE_REQUEST",
                    "检测到重复请求，未重复占用名额",
                    idempotency_key=idempotency_key,
                )
                original.updated_at = self._now()
                return original

            sizes = list(split_sizes) if split_sizes else [people_count]
            if sum(sizes) != people_count:
                raise BookingError("INVALID_REQUEST", "各分团人数之和必须等于总人数")
            if any(size <= 0 for size in sizes):
                raise BookingError("INVALID_REQUEST", "分团人数必须为正数")

            request_id = f"REQ-{uuid.uuid4().hex[:12]}"
            now = self._now()
            splits = [
                GroupSplit(
                    split_id=f"{request_id}-G{i + 1}",
                    size=size,
                    age_band=age_band,
                    accessibility_required=accessibility_required,
                )
                for i, size in enumerate(sizes)
            ]
            request = Request(
                request_id=request_id,
                school_id=school_id,
                idempotency_key=idempotency_key,
                people_count=people_count,
                age_band=age_band,
                theme=theme,
                accessibility_required=accessibility_required,
                preferred_dates=list(dict.fromkeys(preferred_dates)),
                split_sizes=list(sizes),
                priority=priority,
                splits=splits,
                status=STATUS_DRAFT,
                created_at=now,
                updated_at=now,
            )
            self._requests[request_id] = request
            self._idempotency[key] = request_id
            self._log(
                "application_received",
                request_id,
                f"收到 {school_id} 的预约意向：{people_count} 人，{len(splits)} 个分团，"
                f"备选日期 {', '.join(request.preferred_dates)}",
                preferred_dates=request.preferred_dates,
            )

            held_count = 0
            for date in request.preferred_dates:
                day_sessions = [
                    s for s in self._sessions.values() if s.date == date and s.theme == theme
                ]
                plan = self._build_plan(request, splits, day_sessions, date, hold=held_count < self._max_held_plans)
                request.plans.append(plan)
                if plan.state == PLAN_HELD:
                    held_count += 1
                    self._decide(
                        request,
                        "PLAN_HELD",
                        f"日期 {date} 的方案 {plan.plan_id} 已暂占名额，"
                        f"须在 {plan.expires_at.isoformat()} 前确认",
                        plan_id=plan.plan_id,
                        date=date,
                        held_seats=dict(plan.held_seats),
                        expires_at=plan.expires_at.isoformat(),
                    )
                elif plan.state == PLAN_ALTERNATIVE:
                    self._decide(
                        request,
                        "PLAN_ALTERNATIVE",
                        f"日期 {date} 的方案 {plan.plan_id} 可成行，但超出每单暂占上限，"
                        "仅作备选不占名额，先到先得",
                        plan_id=plan.plan_id,
                        date=date,
                    )

            held_plans = [p for p in request.plans if p.state == PLAN_HELD]
            in_waitlist = any(
                any(e.request_id == request_id for e in queue)
                for queue in self._waitlist.values()
            )
            if held_plans:
                request.status = STATUS_PENDING_CONFIRMATION
            elif in_waitlist:
                request.status = STATUS_WAITLISTED
            else:
                request.status = STATUS_REJECTED
            return request

    def _build_plan(
        self,
        request: Request,
        splits: list[GroupSplit],
        day_sessions: list[Session],
        date: str,
        *,
        hold: bool,
        pinned: Optional[tuple[str, str]] = None,
    ) -> Plan:
        plan_id = f"PLAN-{uuid.uuid4().hex[:12]}"
        now = self._now()
        if not day_sessions:
            reason = {"code": "NO_SESSION", "message": f"日期 {date} 没有开放可预约场次", "shortage": 0}
        else:
            # 同一次申请的多个方案互斥（确认其一会释放其余暂占），
            # 评估任一方案时都不计入本意向自身的占用
            allocations, used, reason = self._pack_splits(
                splits,
                day_sessions,
                exclude_request=request.request_id,
                pinned=pinned,
            )
        if reason:
            plan = Plan(
                plan_id=plan_id,
                request_id=request.request_id,
                school_id=request.school_id,
                date=date,
                allocations={},
                held_seats={},
                state=PLAN_REJECTED,
                priority=request.priority,
                created_at=now,
                decided_at=now,
                idempotency_key=request.idempotency_key,
            )
            self._log(
                "plan_rejected",
                request.request_id,
                f"日期 {date} 方案被拒：{reason['message']}",
                plan_id=plan_id,
                date=date,
                **{k: v for k, v in reason.items() if k != "message"},
            )
            self._decide(request, reason["code"], f"日期 {date}：{reason['message']}", plan_id=plan_id, date=date)
            # 因容量不足 → 自动进入该场次候补队列
            if reason.get("code") == "CAPACITY_SHORTAGE" and hold:
                self._join_waitlist(request, splits, day_sessions, date, plan_id)
            return plan

        assert allocations is not None
        if hold:
            state, expires_at = PLAN_HELD, now + self._confirm_ttl
        else:
            state, expires_at = PLAN_ALTERNATIVE, None
        plan = Plan(
            plan_id=plan_id,
            request_id=request.request_id,
            school_id=request.school_id,
            date=date,
            allocations=allocations,
            held_seats=used,
            state=state,
            priority=request.priority,
            created_at=now,
            expires_at=expires_at,
            idempotency_key=request.idempotency_key,
        )
        if hold:
            self._log(
                "plan_held",
                request.request_id,
                f"日期 {date} 方案 {plan_id} 暂占 {sum(used.values())} 个席位",
                plan_id=plan_id,
                date=date,
                held_seats=dict(used),
                expires_at=expires_at.isoformat(),
            )
        return plan

    def _join_waitlist(
        self,
        request: Request,
        splits: list[GroupSplit],
        day_sessions: list[Session],
        date: str,
        plan_id: str,
    ) -> None:
        for split in sorted(splits, key=lambda s: s.size, reverse=True):
            pool = [s for s in day_sessions if not split.accessibility_required or s.accessible]
            if not pool:
                continue
            # 候补挂在当日容量最大的同主题场次
            pool = [s for s in pool if s.theme == request.theme] or pool
            target = max(pool, key=lambda s: s.capacity)
            entry = WaitlistEntry(
                request_id=request.request_id,
                school_id=request.school_id,
                session_id=target.session_id,
                split_id=split.split_id,
                seats=split.size,
                priority=request.priority,
                created_at=self._now(),
            )
            queue = self._waitlist[target.session_id]
            if not any(
                e.request_id == request.request_id and e.split_id == split.split_id for e in queue
            ):
                queue.append(entry)
                queue.sort(key=lambda e: (-e.priority, e.created_at))
                rank = queue.index(entry) + 1
                self._log(
                    "waitlisted",
                    request.request_id,
                    f"分团 {split.split_id} 进入场次 {target.session_id} 候补队列，当前第 {rank} 位",
                    plan_id=plan_id,
                    session_id=target.session_id,
                    split_id=split.split_id,
                    seats=split.size,
                    rank=rank,
                )
                self._decide(
                    request,
                    "WAITLISTED",
                    f"日期 {date} 容量不足，分团 {split.split_id}（{split.size} 人）"
                    f"已在场次 {target.session_id} 候补，第 {rank} 位",
                    session_id=target.session_id,
                    split_id=split.split_id,
                    rank=rank,
                )
                request.waitlist_session_id = target.session_id

    # ---------------------------------------------------------- 确认/释放

    def _owned(self, request_id: str, school_id: str) -> Request:
        request = self._requests.get(request_id)
        if request is None:
            raise BookingError("NOT_FOUND", "预约意向不存在")
        if request.school_id != school_id:
            # 租户隔离：不向其他学校泄露存在性
            raise BookingError("FORBIDDEN", "无权访问该预约意向")
        return request

    def _require_school(self, school_id: str) -> School:
        school = self._schools.get(school_id)
        if school is None:
            raise BookingError("UNKNOWN_SCHOOL", "学校未登记")
        return school

    def _require_session(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise BookingError("UNKNOWN_SESSION", "场次不存在")
        return session

    def confirm(self, request_id: str, school_id: str, plan_id: str) -> Request:
        """确认某个暂占方案；同意向其余暂占方案原子释放并触发递补。"""
        with self._lock:
            self._lazy_sweep()
            request = self._owned(request_id, school_id)
            chosen = next((p for p in request.plans if p.plan_id == plan_id), None)
            if chosen is None:
                raise BookingError("PLAN_NOT_FOUND", "方案不存在")
            # 幂等：对已确认方案的重复确认直接返回当前意向
            if chosen.state == PLAN_CONFIRMED and request.confirmed_plan_id == chosen.plan_id:
                return request
            if chosen.state not in (PLAN_HELD, PLAN_ALTERNATIVE):
                raise BookingError(
                    "PLAN_NOT_HOLDABLE",
                    f"方案当前状态为 {chosen.state}，无法确认",
                    {"plan_state": chosen.state},
                )
            now = self._now()
            if chosen.state == PLAN_HELD and chosen.expires_at is not None and now > chosen.expires_at:
                raise BookingError("PLAN_EXPIRED", "方案已超过确认期限")

            # 备选方案不占名额；确认前原子复核容量，被抢走则拒绝（此时尚未释放任何暂占）
            if chosen.state == PLAN_ALTERNATIVE:
                day_sessions = [
                    s for s in self._sessions.values()
                    if s.date == chosen.date and s.theme == request.theme
                ]
                allocations, used, reason = self._pack_splits(
                    request.splits, day_sessions, exclude_request=request.request_id
                )
                if allocations is None:
                    raise BookingError(
                        "ALTERNATIVE_TAKEN",
                        f"备选方案的名额已被占用：{reason.get('message', '容量不足')}",
                        {"plan_id": chosen.plan_id},
                    )

            freed_sessions: set[str] = set()
            for plan in request.plans:
                if plan is chosen:
                    continue
                if plan.state == PLAN_HELD:
                    freed_sessions.update(plan.held_seats.keys())
                    self._release_plan(request, plan, PLAN_RELEASED, "确认其他方案，原子释放本方案暂占")

            if chosen.state == PLAN_ALTERNATIVE:
                chosen.allocations = allocations
                chosen.held_seats = used

            chosen.state = PLAN_CONFIRMED
            chosen.decided_at = now
            request.confirmed_plan_id = chosen.plan_id
            request.status = STATUS_SCHEDULED
            request.waitlist_session_id = None
            request.waitlist_rank = None
            for split in request.splits:
                split.session_id = chosen.allocations.get(split.split_id)
            self._remove_waitlist_entries(request.request_id, cancel_reason="意向已确认")
            request.updated_at = now
            self._log(
                "plan_confirmed",
                request_id,
                f"方案 {plan_id} 已确认，正式占用 {sum(chosen.held_seats.values())} 个席位",
                plan_id=plan_id,
                allocations=dict(chosen.allocations),
            )
            self._decide(request, "CONFIRMED", f"方案 {plan_id} 已确认排定", plan_id=plan_id)
            self._promote(freed_sessions | set(chosen.held_seats.keys()))
            return request

    def _release_plan(self, request: Request, plan: Plan, state: str, message: str) -> None:
        plan.state = state
        plan.decided_at = self._now()
        event_type = "plan_expired" if state == PLAN_EXPIRED else "plan_released"
        self._log(
            event_type,
            request.request_id,
            message + f"（方案 {plan.plan_id}）",
            plan_id=plan.plan_id,
            released_seats=dict(plan.held_seats),
        )

    # ---------------------------------------------------------- 缩减/取消

    def reduce_split(
        self,
        request_id: str,
        school_id: str,
        split_id: str,
        new_size: Optional[int] = None,
    ) -> Request:
        """缩减分团人数；new_size 为 None 或 0 表示整个班级取消。原子释放名额。"""
        with self._lock:
            self._lazy_sweep()
            request = self._owned(request_id, school_id)
            split = next((s for s in request.splits if s.split_id == split_id), None)
            if split is None:
                raise BookingError("SPLIT_NOT_FOUND", "分团不存在")
            active_plans = [p for p in request.plans if p.state in (PLAN_HELD, PLAN_CONFIRMED)]
            if not active_plans:
                raise BookingError("NO_ACTIVE_PLAN", "当前没有可缩减的有效方案")

            removing = new_size is None or new_size == 0
            if not removing and new_size > split.size:
                raise BookingError("REDUCE_ONLY", "缩减操作不能增加人数，请改期或重新申请")
            if not removing and new_size <= 0:
                raise BookingError("INVALID_SIZE", "分团人数必须为正数")

            old_size = split.size
            freed: set[str] = set()

            def apply_to_plan(target_plan: Plan) -> None:
                sid = target_plan.allocations.get(split_id)
                if sid is None:
                    return
                target_plan.held_seats[sid] = target_plan.held_seats.get(sid, 0) - (old_size if removing else delta)
                if target_plan.held_seats[sid] <= 0:
                    target_plan.held_seats.pop(sid, None)
                freed.add(sid)

            if removing:
                request.splits.remove(split)
                request.split_sizes = [s.size for s in request.splits]
                request.people_count -= old_size
                for target_plan in active_plans:
                    target_plan.allocations.pop(split_id, None)
                    apply_to_plan(target_plan)
                message = f"分团 {split_id}（{old_size} 人）整团取消，席位已释放"
            else:
                delta = old_size - new_size
                split.size = new_size
                request.split_sizes = [s.size for s in request.splits]
                request.people_count -= delta
                for target_plan in active_plans:
                    apply_to_plan(target_plan)
                message = f"分团 {split_id} 由 {old_size} 人缩减为 {new_size} 人，释放 {delta} 个席位"

            request.updated_at = self._now()
            if not request.splits:
                self._cancel_request(request, "全部分团取消，意向关闭")
            self._log("reduced", request_id, message, split_id=split_id, freed_sessions=sorted(freed))
            self._decide(request, "REDUCED", message, split_id=split_id)
            self._promote(freed)
            return request

    def cancel(self, request_id: str, school_id: str) -> Request:
        """学校取消整个意向。"""
        with self._lock:
            self._lazy_sweep()
            request = self._owned(request_id, school_id)
            if request.status in (STATUS_SETTLED, STATUS_CANCELLED):
                raise BookingError("INVALID_STATE", f"意向当前状态 {request.status}，不可取消")
            freed = self._cancel_request(request, "学校主动取消")
            self._promote(freed)
            return request

    def _cancel_request(self, request: Request, message: str) -> set[str]:
        freed: set[str] = set()
        for plan in request.plans:
            if plan.state in (PLAN_HELD, PLAN_CONFIRMED):
                freed.update(plan.held_seats.keys())
                self._release_plan(request, plan, PLAN_RELEASED, message)
        self._remove_waitlist_entries(request.request_id, cancel_reason=message)
        request.status = STATUS_CANCELLED
        request.confirmed_plan_id = None
        request.updated_at = self._now()
        self._log("cancelled", request.request_id, message)
        self._decide(request, "CANCELLED", message)
        return freed

    # ---------------------------------------------------------- 改期

    def reschedule(self, request_id: str, school_id: str, preferred_dates: list[str]) -> Request:
        """原子改期：新日期方案可行才释放旧名额，失败则原排定保持不变。"""
        with self._lock:
            self._lazy_sweep()
            request = self._owned(request_id, school_id)
            if not preferred_dates:
                raise BookingError("INVALID_REQUEST", "改期至少需要一个备选日期")
            old_plan = self._active_plan(request)
            if old_plan is None:
                raise BookingError("NO_ACTIVE_PLAN", "没有可改期的有效方案")
            was_confirmed = old_plan.state == PLAN_CONFIRMED

            # 先在“排除本意向占用”的视图里探测新方案
            candidate: Optional[Plan] = None
            candidate_date: Optional[str] = None
            for date in dict.fromkeys(preferred_dates):
                day_sessions = [
                    s for s in self._sessions.values()
                    if s.date == date and s.theme == request.theme
                ]
                allocations, used, reason = self._pack_splits(
                    request.splits, day_sessions, exclude_request=request.request_id
                )
                if allocations is None:
                    self._decide(
                        request,
                        f"RESCHEDULE_BLOCKED:{date}",
                        f"改期至 {date} 未成功：{reason.get('message', '无可行场次')}",
                        date=date,
                    )
                    continue
                now = self._now()
                candidate = Plan(
                    plan_id=f"PLAN-{uuid.uuid4().hex[:12]}",
                    request_id=request.request_id,
                    school_id=school_id,
                    date=date,
                    allocations=allocations,
                    held_seats=used,
                    state=PLAN_CONFIRMED if was_confirmed else PLAN_HELD,
                    priority=request.priority,
                    created_at=now,
                    decided_at=now if was_confirmed else None,
                    expires_at=None if was_confirmed else now + self._confirm_ttl,
                    idempotency_key=request.idempotency_key,
                )
                candidate_date = date
                break

            if candidate is None:
                raise BookingError(
                    "RESCHEDULE_IMPOSSIBLE",
                    "所有备选日期均无足够容量，原排定保持不变",
                    {"preferred_dates": list(preferred_dates)},
                )

            freed: set[str] = set()
            for plan in request.plans:
                if plan.state in (PLAN_HELD, PLAN_CONFIRMED):
                    freed.update(plan.held_seats.keys())
                    self._release_plan(request, plan, PLAN_RELEASED, f"改期至 {candidate_date}，释放原方案")
            request.plans.append(candidate)
            request.preferred_dates = list(dict.fromkeys(preferred_dates))
            for split in request.splits:
                split.session_id = candidate.allocations.get(split.split_id)
            if was_confirmed:
                request.confirmed_plan_id = candidate.plan_id
                request.status = STATUS_SCHEDULED
            else:
                request.confirmed_plan_id = None
                request.status = STATUS_PENDING_CONFIRMATION
            request.updated_at = self._now()
            self._log(
                "rescheduled",
                request_id,
                f"已改期至 {candidate_date}（方案 {candidate.plan_id}）",
                plan_id=candidate.plan_id,
                date=candidate_date,
                allocations=dict(candidate.allocations),
            )
            self._decide(request, "RESCHEDULED", f"已原子改期至 {candidate_date}", date=candidate_date)
            # 旧席位释放可递补，新方案占用的场次同样参与评估
            self._promote(freed | set(candidate.held_seats.keys()))
            return request

    def _active_plan(self, request: Request) -> Optional[Plan]:
        confirmed = next((p for p in request.plans if p.state == PLAN_CONFIRMED), None)
        if confirmed:
            return confirmed
        held = [p for p in request.plans if p.state == PLAN_HELD]
        return held[0] if len(held) == 1 else (held[0] if held else None)

    # ---------------------------------------------------------- 超时/候补

    def sweep_expired(self) -> list[str]:
        """超时任务：释放所有过期暂占并尝试递补。时钟由外部注入。"""
        with self._lock:
            return self._lazy_sweep()

    def start_sweeper(self, interval: timedelta) -> threading.Event:
        """启动后台定时扫描（生产环境配合 SystemClock 使用）。

        返回停止信号 Event，调用 set() 即可停止。测试中使用注入时钟时
        应直接调用 sweep_expired()，避免依赖真实墙钟时间。
        """
        stop = threading.Event()

        def loop() -> None:
            while not stop.wait(interval.total_seconds()):
                self.sweep_expired()

        thread = threading.Thread(target=loop, name="booking-sweeper", daemon=True)
        thread.start()
        return stop

    def _lazy_sweep(self) -> list[str]:
        now = self._now()
        expired_requests: dict[str, Request] = {}
        freed_sessions: set[str] = set()
        for request in self._requests.values():
            for plan in request.plans:
                if plan.state == PLAN_HELD and plan.expires_at is not None and now > plan.expires_at:
                    freed_sessions.update(plan.held_seats.keys())
                    expired_requests[request.request_id] = request
                    self._release_plan(
                        request,
                        plan,
                        PLAN_EXPIRED,
                        f"超过确认期限 {plan.expires_at.isoformat()} 未确认，暂占自动释放",
                    )
        for request in expired_requests.values():
            if not any(p.state == PLAN_HELD for p in request.plans):
                if request.status == STATUS_PENDING_CONFIRMATION:
                    in_queue = any(
                        any(e.request_id == request.request_id for e in queue)
                        for queue in self._waitlist.values()
                    )
                    request.status = STATUS_WAITLISTED if in_queue else STATUS_DRAFT
                    request.confirmed_plan_id = None
                    request.updated_at = now
                    self._decide(
                        request,
                        "EXPIRED",
                        "全部暂占方案超时未确认，名额已释放；意向回到候补/筹备，可稍后重新选择日期",
                    )
        if freed_sessions:
            self._promote(freed_sessions)
        return sorted(expired_requests.keys())

    def _remove_waitlist_entries(self, request_id: str, *, cancel_reason: str) -> None:
        for session_id, queue in self._waitlist.items():
            remaining = [e for e in queue if e.request_id != request_id]
            if len(remaining) != len(queue):
                self._waitlist[session_id] = remaining
                self._log(
                    "waitlist_withdrawn",
                    request_id,
                    f"候补登记已撤销：{cancel_reason}",
                    session_id=session_id,
                )

    def _promote(self, session_ids: set[str]) -> None:
        """容量释放后按优先级递补；队头不满足则不跳过（保证公平）。

        候补按场次排队，但学校关心的是日期：某天任一场次释放名额时，
        评估该天所有场次的候补队列；仅当释放的就是队列自身场次时，
        才要求对应分团落回该场次（pinned）。
        """
        freed_set = {sid for sid in session_ids if sid in self._sessions}
        freed_dates = {self._sessions[sid].date for sid in freed_set}
        same_day = [
            sid for sid, session in self._sessions.items() if session.date in freed_dates
        ]
        # 先评估释放场次自身的队列（pinned 回原场次），再看同日兄弟场次
        candidate_sessions = sorted(freed_set) + sorted(sid for sid in same_day if sid not in freed_set)
        for session_id in candidate_sessions:
            while True:
                queue = self._waitlist.get(session_id)
                if not queue:
                    break
                entry = queue[0]
                request = self._requests.get(entry.request_id)
                if request is None or request.status in (STATUS_CANCELLED, STATUS_SETTLED):
                    queue.pop(0)
                    continue
                date = self._sessions[session_id].date
                day_sessions = [
                    s for s in self._sessions.values()
                    if s.date == date and s.theme == request.theme
                ]
                pinned = (entry.split_id, session_id) if session_id in freed_set else None
                allocations, used, reason = self._pack_splits(
                    request.splits,
                    day_sessions,
                    exclude_request=request.request_id,
                    pinned=pinned,
                )
                if allocations is None:
                    self._decide(
                        request,
                        "PROMOTION_BLOCKED",
                        f"场次 {session_id} 有空位但暂不递补：{reason.get('message', '整体方案仍不可行')}",
                        session_id=session_id,
                        split_id=entry.split_id,
                    )
                    self._log(
                        "promoted",
                        request.request_id,
                        f"递补未成功：{reason.get('message', '整体方案仍不可行')}",
                        session_id=session_id,
                        succeeded=False,
                    )
                    break  # 队头不行，不跳过其后的排队者
                # 递补成功：建立全新暂占方案
                now = self._now()
                plan = Plan(
                    plan_id=f"PLAN-{uuid.uuid4().hex[:12]}",
                    request_id=request.request_id,
                    school_id=request.school_id,
                    date=date,
                    allocations=allocations,
                    held_seats=used,
                    state=PLAN_HELD,
                    priority=request.priority,
                    created_at=now,
                    expires_at=now + self._confirm_ttl,
                    idempotency_key=request.idempotency_key,
                )
                request.plans.append(plan)
                request.status = STATUS_PENDING_CONFIRMATION
                request.confirmed_plan_id = None
                request.waitlist_session_id = None
                request.updated_at = now
                queue.pop(0)
                # 该意向在其他场次的候补登记一并撤销
                self._remove_waitlist_entries(request.request_id, cancel_reason="候补递补成功")
                self._log(
                    "promoted",
                    request.request_id,
                    f"候补递补成功：场次 {session_id} 释放名额，意向获得方案 {plan.plan_id}，"
                    f"须在 {plan.expires_at.isoformat()} 前确认",
                    session_id=session_id,
                    plan_id=plan.plan_id,
                    succeeded=True,
                    expires_at=plan.expires_at.isoformat(),
                )
                self._decide(
                    request,
                    "PROMOTED",
                    f"场次 {session_id} 有名额释放，按候补优先级递补成功，方案 {plan.plan_id} 已暂占",
                    session_id=session_id,
                    plan_id=plan.plan_id,
                    expires_at=plan.expires_at.isoformat(),
                )
                # 继续尝试队列后续

    # ---------------------------------------------------------- 生命周期

    def mark_in_progress(self, request_id: str) -> Request:
        with self._lock:
            request = self._requests.get(request_id)
            if request is None:
                raise BookingError("NOT_FOUND", "预约意向不存在")
            if request.status != STATUS_SCHEDULED:
                raise BookingError("INVALID_STATE", f"状态 {request.status} 不可进入执行中")
            request.status = STATUS_IN_PROGRESS
            request.updated_at = self._now()
            return request

    def settle(self, request_id: str) -> Request:
        with self._lock:
            request = self._requests.get(request_id)
            if request is None:
                raise BookingError("NOT_FOUND", "预约意向不存在")
            if request.status not in (STATUS_SCHEDULED, STATUS_IN_PROGRESS):
                raise BookingError("INVALID_STATE", f"状态 {request.status} 不可结算")
            request.status = STATUS_SETTLED
            request.updated_at = self._now()
            return request

    # ---------------------------------------------------------- 查询

    def get_request(self, request_id: str, school_id: str) -> Request:
        """学校只能查看自身预约（租户隔离）。"""
        with self._lock:
            self._lazy_sweep()
            return self._owned(request_id, school_id)

    def list_requests(self, school_id: str) -> list[Request]:
        with self._lock:
            self._lazy_sweep()
            return [r for r in self._requests.values() if r.school_id == school_id]

    # - - - - - - - - - - - - - - 运营视图（可解释）- - - - - - - - - - - -

    def ops_list_requests(self) -> list[Request]:
        with self._lock:
            self._lazy_sweep()
            return list(self._requests.values())

    def ops_get_request(self, request_id: str) -> Request:
        with self._lock:
            self._lazy_sweep()
            request = self._requests.get(request_id)
            if request is None:
                raise BookingError("NOT_FOUND", "预约意向不存在")
            return request

    def explanation(self, request_id: str) -> dict:
        """运营接口：解释拒绝、候补与递补原因。"""
        with self._lock:
            self._lazy_sweep()
            request = self.ops_get_request(request_id)
            waitlist_rows = []
            for session_id, queue in self._waitlist.items():
                for rank, entry in enumerate(queue, start=1):
                    if entry.request_id == request_id:
                        waitlist_rows.append(
                            {
                                "session_id": session_id,
                                "split_id": entry.split_id,
                                "seats": entry.seats,
                                "priority": entry.priority,
                                "rank": rank,
                            }
                        )
            return {
                "request_id": request_id,
                "school_id": request.school_id,
                "status": request.status,
                "priority": request.priority,
                "plans": [
                    {
                        "plan_id": p.plan_id,
                        "date": p.date,
                        "state": p.state,
                        "allocations": dict(p.allocations),
                        "held_seats": dict(p.held_seats),
                        "expires_at": p.expires_at.isoformat() if p.expires_at else None,
                    }
                    for p in request.plans
                ],
                "waitlist": waitlist_rows,
                "decisions": list(request.decisions),
            }

    def ops_waitlist(self) -> dict:
        with self._lock:
            self._lazy_sweep()
            return {
                session_id: [
                    {
                        "request_id": e.request_id,
                        "school_id": e.school_id,
                        "split_id": e.split_id,
                        "seats": e.seats,
                        "priority": e.priority,
                        "rank": i + 1,
                        "created_at": e.created_at.isoformat(),
                    }
                    for i, e in enumerate(queue)
                ]
                for session_id, queue in sorted(self._waitlist.items())
                if queue
            }

    def ops_audit(self, limit: Optional[int] = None) -> list[dict]:
        with self._lock:
            events = self._audit if limit is None else self._audit[-limit:]
            return [
                {
                    "seq": e.seq,
                    "at": e.at.isoformat(),
                    "event_type": e.event_type,
                    "request_id": e.request_id,
                    "message": e.message,
                    "data": e.data,
                }
                for e in events
            ]

    def ops_sessions(self) -> list[dict]:
        with self._lock:
            return [self.session_availability(sid) for sid in sorted(self._sessions)]
