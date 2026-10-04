"""命令行入口：python3 -m booking_service [--host 127.0.0.1] [--port 8080] [--admin-key KEY]"""
from __future__ import annotations

import argparse

from .clock import SystemClock
from .http_app import BookingHTTPServerApp
from .service import BookingService


def main() -> None:
    parser = argparse.ArgumentParser(description="学校团体预约受理服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--admin-key", default="ops-key")
    parser.add_argument("--confirm-ttl-hours", type=float, default=24.0)
    parser.add_argument("--max-held-plans", type=int, default=2)
    args = parser.parse_args()

    from datetime import timedelta

    service = BookingService(
        clock=SystemClock(),
        confirm_ttl=timedelta(hours=args.confirm_ttl_hours),
        max_held_plans=args.max_held_plans,
    )
    app = BookingHTTPServerApp(service, admin_key=args.admin_key)
    print(f"预约受理服务监听 http://{args.host}:{args.port}")
    try:
        app.serve_foreground(args.host, args.port)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
