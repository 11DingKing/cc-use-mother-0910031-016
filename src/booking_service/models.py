"""领域模型：学校、场次容量、预约意向、拆分团体、暂占方案。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# ---- 预约意向状态（对齐契约 states：筹备/待确认/已排定/执行中/已结算）----
STATUS_DRAFT = "筹备"
STATUS_PENDING_CONFIRMATION = "待确认"
STATUS_SCHEDULED = "已排定"
STATUS_IN_PROGRESS = "执行中"
STATUS_SETTLED = "已结算"
STATUS_CANCELLED = "已取消"
STATUS_REJECTED = "已拒绝"
STATUS_WAITLISTED = "候补"

# ---- 方案占用状态 ----
PLAN_HELD = "held"            # 暂占（待学校确认）
PLAN_ALTERNATIVE = "alternative"  # 可行备选（超出暂占上限，不占名额）
PLAN_CONFIRMED = "confirmed"  # 已确认，占用正式名额
PLAN_RELEASED = "released"    # 已主动释放
PLAN_EXPIRED = "expired"      # 超时未确认被释放
PLAN_REJECTED = "rejected"    # 容量不足未获暂占

# ---- 运营审计事件类型 ----
EVENT_TYPES = (
    "application_received",
    "plan_held",
    "plan_rejected",
    "plan_confirmed",
    "plan_released",
    "plan_expired",
    "reduced",
    "waitlisted",
    "waitlist_withdrawn",
    "promoted",
    "rescheduled",
    "duplicate_request",
    "cancelled",
)


@dataclass
class School:
    school_id: str
    name: str


@dataclass
class Session:
    """一个可预约场次（日期+时段+主题），容量按席位计。"""

    session_id: str
    date: str
    time_slot: str
    theme: str
    capacity: int
    accessible: bool = False


@dataclass
class GroupSplit:
    """团体拆分：一个意向可拆成多个分团，分别落到不同场次。"""

    split_id: str
    size: int
    age_band: str
    accessibility_required: bool
    session_id: Optional[str] = None  # 已排定后填入


@dataclass
class Plan:
    """多方案中的一个候选方案：每个分团各占一个场次。"""

    plan_id: str
    request_id: str
    school_id: str
    date: str
    allocations: dict[str, str]  # split_id -> session_id
    held_seats: dict[str, int]   # session_id -> 暂占席位数
    state: str = PLAN_HELD
    priority: int = 0
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    decided_at: Optional[datetime] = None
    idempotency_key: Optional[str] = None


@dataclass
class Request:
    """学校提交的预约意向（一次申请）。"""

    request_id: str
    school_id: str
    idempotency_key: str
    people_count: int
    age_band: str
    theme: str
    accessibility_required: bool
    preferred_dates: list[str]
    split_sizes: list[int]
    status: str = STATUS_DRAFT
    priority: int = 0
    splits: list[GroupSplit] = field(default_factory=list)
    plans: list[Plan] = field(default_factory=list)
    confirmed_plan_id: Optional[str] = None
    waitlist_session_id: Optional[str] = None
    waitlist_rank: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # 拒绝/递补的可解释原因链（运营接口使用）
    decisions: list[dict] = field(default_factory=list)


@dataclass
class WaitlistEntry:
    request_id: str
    school_id: str
    session_id: str
    split_id: str
    seats: int
    priority: int
    created_at: datetime


@dataclass
class AuditEvent:
    seq: int
    at: datetime
    event_type: str
    request_id: str
    message: str
    data: dict = field(default_factory=dict)
