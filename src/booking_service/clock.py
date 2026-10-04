"""可注入时钟，供超时任务使用。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    """返回带时区的当前时间。"""

    def now(self) -> datetime: ...


class SystemClock:
    """生产环境使用的系统时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """测试/重放使用的固定时钟，可手动推进。"""

    def __init__(self, moment: datetime) -> None:
        self._moment = self._ensure_aware(moment)

    @staticmethod
    def _ensure_aware(moment: datetime) -> datetime:
        if moment.tzinfo is None:
            raise ValueError("时钟时间必须带时区")
        return moment

    def now(self) -> datetime:
        return self._moment

    def advance(self, **delta) -> datetime:
        from datetime import timedelta

        self._moment = self._moment + timedelta(**delta)
        return self._moment

    def set(self, moment: datetime) -> None:
        self._moment = self._ensure_aware(moment)
