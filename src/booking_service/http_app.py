"""HTTP 服务端：学校自助接口与运营解释接口。

仅依赖标准库；领域服务本身线程安全，可直接配合 ThreadingHTTPServer 使用。
鉴权：
- 学校接口要求请求头 ``X-School-ID``，且只能操作/查看本校意向；
- 运营接口要求请求头 ``X-Admin-Key`` 等于启动时配置的密钥。
"""
from __future__ import annotations

import json
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from .errors import BookingError
from .serializers import request_to_dict
from .service import BookingService


def _json_default(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"无法序列化 {type(value)!r}")


class BookingHTTPServerApp:
    def __init__(self, service: BookingService, admin_key: str = "ops-key") -> None:
        self.service = service
        self.admin_key = admin_key
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ 生命周期

    def serve_foreground(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        self._httpd = ThreadingHTTPServer((host, port), self._handler_factory())
        self._httpd.serve_forever()

    def start_background(self, host: str = "127.0.0.1", port: int = 8080) -> tuple[str, int]:
        self._httpd = ThreadingHTTPServer((host, port), self._handler_factory())
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self._httpd.server_address[0], self._httpd.server_address[1]

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    # ------------------------------------------------------------ 路由

    def _handler_factory(app: "BookingHTTPServerApp") -> type[BaseHTTPRequestHandler]:  # noqa: N805
        class Handler(BaseHTTPRequestHandler):
            server_version = "BookingService/1.0"

            def log_message(self, fmt: str, *args: Any) -> None:  # 静默，避免污染测试输出
                return

            # --- 基础工具 ---

            def _send_json(self, status: int, payload: Any) -> None:
                body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _error(self, exc: BookingError) -> None:
                status = HTTPStatus.BAD_REQUEST
                if exc.code in ("NOT_FOUND", "PLAN_NOT_FOUND", "SPLIT_NOT_FOUND", "UNKNOWN_SESSION"):
                    status = HTTPStatus.NOT_FOUND
                elif exc.code == "FORBIDDEN":
                    status = HTTPStatus.FORBIDDEN
                elif exc.code in ("UNKNOWN_SCHOOL",):
                    status = HTTPStatus.UNAUTHORIZED
                self._send_json(
                    status,
                    {"error": {"code": exc.code, "message": exc.reason, "details": exc.details}},
                )

            def _read_body(self) -> dict:
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length == 0:
                    return {}
                try:
                    raw = self.rfile.read(length)
                    value = json.loads(raw.decode("utf-8"))
                    if not isinstance(value, dict):
                        raise ValueError
                    return value
                except (ValueError, UnicodeDecodeError):
                    raise BookingError("INVALID_BODY", "请求体必须是 JSON 对象")

            def _school_id(self) -> str:
                school_id = self.headers.get("X-School-ID", "").strip()
                if not school_id:
                    raise BookingError("UNKNOWN_SCHOOL", "缺少 X-School-ID 请求头")
                return school_id

            def _require_admin(self) -> None:
                if self.headers.get("X-Admin-Key", "") != app.admin_key:
                    raise BookingError("FORBIDDEN", "运营接口需要有效的 X-Admin-Key")

            # --- 分发 ---

            def do_GET(self) -> None:  # noqa: N802
                self._dispatch("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._dispatch("POST")

            def _dispatch(self, method: str) -> None:
                try:
                    parsed = urlparse(self.path)
                    path = parsed.path.rstrip("/") or "/"
                    path_known = False
                    for pattern, verbs, target in ROUTES:
                        match = pattern.fullmatch(path)
                        if match:
                            path_known = True
                            if method in verbs:
                                target(self, app, **match.groupdict())
                                return
                    if path_known:
                        self._send_json(
                            HTTPStatus.METHOD_NOT_ALLOWED,
                            {"error": {"code": "METHOD_NOT_ALLOWED", "message": "不支持的请求方法"}},
                        )
                    else:
                        self._send_json(
                            HTTPStatus.NOT_FOUND,
                            {"error": {"code": "NOT_FOUND", "message": "未知接口"}},
                        )
                except BookingError as exc:
                    self._error(exc)
                except Exception:  # noqa: BLE001
                    import sys
                    import traceback

                    print(traceback.format_exc(), file=sys.stderr)
                    self._send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": {"code": "INTERNAL_ERROR", "message": "服务内部错误"}},
                    )

        return Handler


# -------------------------------------------------------------- 端点处理

def _body_get(body: dict, key: str, required: bool = True, default: Any = None) -> Any:
    value = body.get(key, default)
    if required and value is None:
        raise BookingError("INVALID_BODY", f"缺少字段：{key}")
    return value


def _body_int(body: dict, key: str, default: Optional[int] = None) -> int:
    if key not in body or body[key] is None:
        if default is not None:
            return default
        raise BookingError("INVALID_BODY", f"缺少字段：{key}")
    value = body[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise BookingError("INVALID_BODY", f"字段 {key} 必须是整数")
    return value


def _body_str_list(body: dict, key: str) -> list[str]:
    value = _body_get(body, key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise BookingError("INVALID_BODY", f"字段 {key} 必须是字符串数组")
    return value


def _body_int_list(body: dict, key: str) -> Optional[list[int]]:
    if key not in body or body[key] is None:
        return None
    value = body[key]
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in value
    ):
        raise BookingError("INVALID_BODY", f"字段 {key} 必须是整数数组")
    return value


# --- 学校自助 ---

def api_apply(h: BaseHTTPRequestHandler, app: BookingHTTPServerApp) -> None:
    body = h._read_body()  # type: ignore[attr-defined]
    request = app.service.apply(
        school_id=h._school_id(),  # type: ignore[attr-defined]
        idempotency_key=_body_get(body, "idempotency_key"),
        people_count=_body_int(body, "people_count"),
        age_band=_body_get(body, "age_band"),
        theme=_body_get(body, "theme"),
        preferred_dates=_body_str_list(body, "preferred_dates"),
        split_sizes=_body_int_list(body, "split_sizes"),
        accessibility_required=bool(body.get("accessibility_required", False)),
        priority=_body_int(body, "priority", default=0),
    )
    h._send_json(HTTPStatus.CREATED, request_to_dict(request))  # type: ignore[attr-defined]


def api_list_mine(h: BaseHTTPRequestHandler, app: BookingHTTPServerApp) -> None:
    school_id = h._school_id()  # type: ignore[attr-defined]
    rows = [request_to_dict(r) for r in app.service.list_requests(school_id)]
    h._send_json(HTTPStatus.OK, {"requests": rows})  # type: ignore[attr-defined]


def api_get_mine(h: BaseHTTPRequestHandler, app: BookingHTTPServerApp, request_id: str) -> None:
    request = app.service.get_request(request_id, h._school_id())  # type: ignore[attr-defined]
    h._send_json(HTTPStatus.OK, request_to_dict(request))  # type: ignore[attr-defined]


def api_confirm(h: BaseHTTPRequestHandler, app: BookingHTTPServerApp, request_id: str) -> None:
    body = h._read_body()  # type: ignore[attr-defined]
    request = app.service.confirm(
        request_id, h._school_id(), _body_get(body, "plan_id")  # type: ignore[attr-defined]
    )
    h._send_json(HTTPStatus.OK, request_to_dict(request))  # type: ignore[attr-defined]


def api_reduce(h: BaseHTTPRequestHandler, app: BookingHTTPServerApp, request_id: str) -> None:
    body = h._read_body()  # type: ignore[attr-defined]
    new_size = body.get("new_size")
    if new_size is not None and (isinstance(new_size, bool) or not isinstance(new_size, int)):
        raise BookingError("INVALID_BODY", "字段 new_size 必须是整数或 null")
    request = app.service.reduce_split(
        request_id,
        h._school_id(),  # type: ignore[attr-defined]
        split_id=_body_get(body, "split_id"),
        new_size=new_size,
    )
    h._send_json(HTTPStatus.OK, request_to_dict(request))  # type: ignore[attr-defined]


def api_cancel(h: BaseHTTPRequestHandler, app: BookingHTTPServerApp, request_id: str) -> None:
    request = app.service.cancel(request_id, h._school_id())  # type: ignore[attr-defined]
    h._send_json(HTTPStatus.OK, request_to_dict(request))  # type: ignore[attr-defined]


def api_reschedule(h: BaseHTTPRequestHandler, app: BookingHTTPServerApp, request_id: str) -> None:
    body = h._read_body()  # type: ignore[attr-defined]
    request = app.service.reschedule(
        request_id,
        h._school_id(),  # type: ignore[attr-defined]
        preferred_dates=_body_str_list(body, "preferred_dates"),
    )
    h._send_json(HTTPStatus.OK, request_to_dict(request))  # type: ignore[attr-defined]


# --- 运营 ---

def ops_register_school(h: BaseHTTPRequestHandler, app: BookingHTTPServerApp) -> None:
    h._require_admin()  # type: ignore[attr-defined]
    body = h._read_body()  # type: ignore[attr-defined]
    school = app.service.register_school(_body_get(body, "school_id"), _body_get(body, "name"))
    h._send_json(HTTPStatus.CREATED, {"school_id": school.school_id, "name": school.name})  # type: ignore[attr-defined]


def ops_create_session(h: BaseHTTPRequestHandler, app: BookingHTTPServerApp) -> None:
    h._require_admin()  # type: ignore[attr-defined]
    body = h._read_body()  # type: ignore[attr-defined]
    session = app.service.create_session(
        session_id=_body_get(body, "session_id"),
        date=_body_get(body, "date"),
        time_slot=_body_get(body, "time_slot"),
        theme=_body_get(body, "theme"),
        capacity=_body_int(body, "capacity"),
        accessible=bool(body.get("accessible", False)),
    )
    h._send_json(HTTPStatus.CREATED, session.__dict__)  # type: ignore[attr-defined]


def ops_list_requests(h: BaseHTTPRequestHandler, app: BookingHTTPServerApp) -> None:
    h._require_admin()  # type: ignore[attr-defined]
    rows = [request_to_dict(r) for r in app.service.ops_list_requests()]
    h._send_json(HTTPStatus.OK, {"requests": rows})  # type: ignore[attr-defined]


def ops_explanation(h: BaseHTTPRequestHandler, app: BookingHTTPServerApp, request_id: str) -> None:
    h._require_admin()  # type: ignore[attr-defined]
    h._send_json(HTTPStatus.OK, app.service.explanation(request_id))  # type: ignore[attr-defined]


def ops_waitlist(h: BaseHTTPRequestHandler, app: BookingHTTPServerApp) -> None:
    h._require_admin()  # type: ignore[attr-defined]
    h._send_json(HTTPStatus.OK, {"waitlist": app.service.ops_waitlist()})  # type: ignore[attr-defined]


def ops_sessions(h: BaseHTTPRequestHandler, app: BookingHTTPServerApp) -> None:
    h._require_admin()  # type: ignore[attr-defined]
    h._send_json(HTTPStatus.OK, {"sessions": app.service.ops_sessions()})  # type: ignore[attr-defined]


def ops_audit(h: BaseHTTPRequestHandler, app: BookingHTTPServerApp) -> None:
    h._require_admin()  # type: ignore[attr-defined]
    h._send_json(HTTPStatus.OK, {"events": app.service.ops_audit()})  # type: ignore[attr-defined]


def ops_sweep(h: BaseHTTPRequestHandler, app: BookingHTTPServerApp) -> None:
    h._require_admin()  # type: ignore[attr-defined]
    expired = app.service.sweep_expired()
    h._send_json(HTTPStatus.OK, {"expired_request_ids": expired})  # type: ignore[attr-defined]


Route = tuple[re.Pattern[str], frozenset[str], Callable[..., None]]

ROUTES: list[Route] = [
    (re.compile(r"^/api/schools/applications$"), frozenset({"POST"}), api_apply),
    (re.compile(r"^/api/schools/requests$"), frozenset({"GET"}), api_list_mine),
    (re.compile(r"^/api/schools/requests/(?P<request_id>[^/]+)$"), frozenset({"GET"}), api_get_mine),
    (re.compile(r"^/api/schools/requests/(?P<request_id>[^/]+)/confirm$"), frozenset({"POST"}), api_confirm),
    (re.compile(r"^/api/schools/requests/(?P<request_id>[^/]+)/reduce$"), frozenset({"POST"}), api_reduce),
    (re.compile(r"^/api/schools/requests/(?P<request_id>[^/]+)/cancel$"), frozenset({"POST"}), api_cancel),
    (re.compile(r"^/api/schools/requests/(?P<request_id>[^/]+)/reschedule$"), frozenset({"POST"}), api_reschedule),
    (re.compile(r"^/api/ops/schools$"), frozenset({"POST"}), ops_register_school),
    (re.compile(r"^/api/ops/sessions$"), frozenset({"POST"}), ops_create_session),
    (re.compile(r"^/api/ops/requests$"), frozenset({"GET"}), ops_list_requests),
    (re.compile(r"^/api/ops/requests/(?P<request_id>[^/]+)/explanation$"), frozenset({"GET"}), ops_explanation),
    (re.compile(r"^/api/ops/waitlist$"), frozenset({"GET"}), ops_waitlist),
    (re.compile(r"^/api/ops/sessions$"), frozenset({"GET"}), ops_sessions),
    (re.compile(r"^/api/ops/audit$"), frozenset({"GET"}), ops_audit),
    (re.compile(r"^/api/ops/sweep$"), frozenset({"POST"}), ops_sweep),
]
