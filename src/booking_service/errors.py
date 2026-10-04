"""预约受理领域错误。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BookingError(Exception):
    """业务拒绝异常，code 稳定可机读，reason 面向运营解释。"""

    code: str
    reason: str
    details: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return f"[{self.code}] {self.reason}"
