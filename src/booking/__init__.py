"""学校团体预约受理服务端。

核心用法::

    from booking import BookingService, FakeClock

    service = BookingService(clock=FakeClock(start))
    service.add_session(...)
    request, replayed = service.apply(...)
"""
from .clock import Clock, FakeClock, SystemClock
from .errors import DomainError
from .http_app import build_server, create_service
from .service import BookingService

__all__ = [
    "BookingService",
    "Clock",
    "SystemClock",
    "FakeClock",
    "DomainError",
    "build_server",
    "create_service",
]
