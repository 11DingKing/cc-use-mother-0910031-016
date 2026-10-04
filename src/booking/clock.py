"""可注入时钟。

超时任务（确认期限、候补递补）统一依赖 ``Clock`` 协议取时间：
生产使用 :class:`SystemClock`，测试使用 :class:`FakeClock` 精确推进，
保证超时行为确定可测。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """系统 UTC 时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock:
    """测试用固定/可控时钟。"""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock 必须使用带时区的时间")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float = 0, **kwargs: float) -> None:
        self._now += timedelta(seconds=seconds, **kwargs)
