"""领域对象到可 JSON 序列化字典的转换。"""
from __future__ import annotations

from .models import Request


def _dt(value) -> str | None:
    return value.isoformat() if value is not None else None


def request_to_dict(request: Request, *, include_decisions: bool = False) -> dict:
    return {
        "request_id": request.request_id,
        "school_id": request.school_id,
        "status": request.status,
        "priority": request.priority,
        "people_count": request.people_count,
        "age_band": request.age_band,
        "theme": request.theme,
        "accessibility_required": request.accessibility_required,
        "preferred_dates": list(request.preferred_dates),
        "split_sizes": list(request.split_sizes),
        "confirmed_plan_id": request.confirmed_plan_id,
        "waitlist_session_id": request.waitlist_session_id,
        "created_at": _dt(request.created_at),
        "updated_at": _dt(request.updated_at),
        "splits": [
            {
                "split_id": s.split_id,
                "size": s.size,
                "age_band": s.age_band,
                "accessibility_required": s.accessibility_required,
                "session_id": s.session_id,
            }
            for s in request.splits
        ],
        "plans": [
            {
                "plan_id": p.plan_id,
                "date": p.date,
                "state": p.state,
                "allocations": dict(p.allocations),
                "held_seats": dict(p.held_seats),
                "priority": p.priority,
                "expires_at": _dt(p.expires_at),
                "decided_at": _dt(p.decided_at),
            }
            for p in request.plans
        ],
        **({"decisions": list(request.decisions)} if include_decisions else {}),
    }
