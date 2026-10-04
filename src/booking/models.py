"""领域模型：场次、预约意向、备选方案、候补条目、决策留痕。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

OptionState = Literal["已暂占", "已确认", "已拒绝", "已失效"]
RequestState = Literal["筹备", "待确认", "已排定", "候补中", "已取消", "执行中", "已结算"]
WaitlistState = Literal["候补中", "已递补", "已取消", "已失效"]


@dataclass(frozen=True)
class Decision:
    """运营可追溯的决策记录（拒绝、暂占、递补、释放等）。"""

    seq: int
    time: datetime
    trigger: str  # 申请 | 确认 | 缩减 | 候补 | 改期 | 重复请求 | 超时 | 运营
    code: str
    message: str
    related: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "time": self.time.isoformat(),
            "trigger": self.trigger,
            "code": self.code,
            "message": self.message,
            "related": self.related,
        }


@dataclass
class Session:
    """参观场次（某个日期的某个时段），含容量与无障碍条件。"""

    id: str
    date: str
    label: str
    capacity: int
    accessibility: frozenset[str] = frozenset()

    def to_dict(self, used: int | None = None) -> dict[str, Any]:
        data = {
            "id": self.id,
            "date": self.date,
            "label": self.label,
            "capacity": self.capacity,
            "accessibility": sorted(self.accessibility),
        }
        if used is not None:
            data["used"] = used
            data["available"] = self.capacity - used
        return data


@dataclass
class Option:
    """一个备选日期方案。"""

    session_id: str
    seats: int
    group_count: int
    state: OptionState
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "seats": self.seats,
            "group_count": self.group_count,
            "state": self.state,
            "reason": self.reason,
        }


@dataclass
class WaitlistEntry:
    """候补条目：一所学校在某场次排队等待名额。"""

    id: str
    request_id: str
    school_id: str
    session_id: str
    seats: int
    group_count: int
    state: WaitlistState
    created_at: datetime
    base_priority: int  # 数值越小优先级越高
    note: str = ""
    promoted_at: datetime | None = None
    expired_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "request_id": self.request_id,
            "school_id": self.school_id,
            "session_id": self.session_id,
            "seats": self.seats,
            "group_count": self.group_count,
            "state": self.state,
            "created_at": self.created_at.isoformat(),
            "base_priority": self.base_priority,
            "priority_rule": "无障碍刚需优先；同优先级按申请时间先到先得；再按预约编号",
            "note": self.note,
            "promoted_at": self.promoted_at.isoformat() if self.promoted_at else None,
            "expired_at": self.expired_at.isoformat() if self.expired_at else None,
        }


@dataclass
class ReservationRequest:
    """学校团体预约意向（含团体拆分与多个备选方案）。"""

    id: str
    school_id: str
    school_name: str
    contact: str
    headcount: int
    grades: str
    theme: str
    accessibility: frozenset[str]
    group_count: int
    options: dict[str, Option] = field(default_factory=dict)  # session_id -> Option
    status: RequestState = "筹备"
    created_at: datetime | None = None
    confirm_deadline: datetime | None = None
    idempotency_key: str | None = None
    decisions: list[Decision] = field(default_factory=list)
    waitlist: dict[str, WaitlistEntry] = field(default_factory=dict)  # entry_id -> entry

    # ---- 派生信息 ----
    def held_options(self) -> list[Option]:
        return [o for o in self.options.values() if o.state == "已暂占"]

    def confirmed_option(self) -> Option | None:
        for option in self.options.values():
            if option.state == "已确认":
                return option
        return None

    def active_waitlist(self) -> list[WaitlistEntry]:
        return [w for w in self.waitlist.values() if w.state == "候补中"]

    def to_dict(self, sessions: dict[str, Session] | None = None) -> dict[str, Any]:
        confirmed = self.confirmed_option()
        data: dict[str, Any] = {
            "id": self.id,
            "school_id": self.school_id,
            "school_name": self.school_name,
            "contact": self.contact,
            "headcount": self.headcount,
            "grades": self.grades,
            "theme": self.theme,
            "accessibility": sorted(self.accessibility),
            "group_count": self.group_count,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "confirm_deadline": self.confirm_deadline.isoformat() if self.confirm_deadline else None,
            "options": [o.to_dict() for o in self.options.values()],
            "waitlist": [w.to_dict() for w in self.waitlist.values()],
            "confirmed_session_id": confirmed.session_id if confirmed else None,
            "confirmed_seats": confirmed.seats if confirmed else 0,
            "decisions": [d.to_dict() for d in self.decisions],
        }
        if sessions is not None:
            data["options_detail"] = [
                {
                    **o.to_dict(),
                    "session": sessions[o.session_id].to_dict()
                    if o.session_id in sessions
                    else None,
                }
                for o in self.options.values()
            ]
        return data
