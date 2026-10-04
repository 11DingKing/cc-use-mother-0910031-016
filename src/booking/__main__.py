"""服务入口：python3 -m booking，可通过环境变量配置监听地址与运营令牌。"""
from __future__ import annotations

import os

from .http_app import build_server


def main() -> None:
    host = os.environ.get("BOOKING_HOST", "127.0.0.1")
    port = int(os.environ.get("BOOKING_PORT", "8080"))
    token = os.environ.get("BOOKING_OPS_TOKEN", "ops-dev-token")
    httpd = build_server(host, port, operator_token=token)
    print(f"预约受理服务监听 http://{host}:{port}（运营令牌来自 BOOKING_OPS_TOKEN）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
