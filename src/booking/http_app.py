"""HTTP 接口层（标准库实现，无第三方依赖）。

学校侧 ``/api/...`` 必须携带 ``X-School-Id`` 头，只能查看与操作本校预约；
运营侧 ``/admin/...`` 必须携带配置的运营令牌 ``X-Operator-Token``，
可查看全局队列与每个预约的决策解释。
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .clock import Clock, SystemClock
from .errors import DomainError, unauthorized
from .service import BookingService

PATH_REQUEST = re.compile(r"^/api/requests/(R\d+)$")
PATH_CONFIRM = re.compile(r"^/api/requests/(R\d+)/confirm$")
PATH_REDUCE = re.compile(r"^/api/requests/(R\d+)/reduce$")
PATH_CANCEL = re.compile(r"^/api/requests/(R\d+)/cancel$")
PATH_RESCHEDULE = re.compile(r"^/api/requests/(R\d+)/reschedule$")
PATH_WAITLIST = re.compile(r"^/api/requests/(R\d+)/waitlist$")
PATH_WAITLIST_LEAVE = re.compile(r"^/api/requests/(R\d+)/waitlist/([A-Za-z0-9_.:\-]+)$")
PATH_ADMIN_REQUEST = re.compile(r"^/admin/requests/(R\d+)/explain$")
PATH_ADMIN_EXECUTE = re.compile(r"^/admin/requests/(R\d+)/execute$")
PATH_ADMIN_SETTLE = re.compile(r"^/admin/requests/(R\d+)/settle$")


def create_service(
    clock: Clock | None = None,
    *,
    confirm_window_hours: float = 24.0,
    waitlist_ttl_days: float = 7.0,
    max_group_size: int = 20,
    max_holds: int = 3,
) -> BookingService:
    return BookingService(
        clock=clock or SystemClock(),
        confirm_window=timedelta(hours=confirm_window_hours),
        waitlist_ttl=timedelta(days=waitlist_ttl_days),
        max_group_size=max_group_size,
        max_holds=max_holds,
    )


class BookingHandler(BaseHTTPRequestHandler):
    service: BookingService = None  # 由 build_server 注入到类属性
    operator_token: str = ""

    server_version = "BookingServer/1.0"

    # ---- 基础收发 ----
    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise DomainError("参数无效", "请求体必须是合法 JSON", 400)
        if not isinstance(data, dict):
            raise DomainError("参数无效", "请求体必须是 JSON 对象", 400)
        return data

    def _school_id(self) -> str:
        school_id = self.headers.get("X-School-Id", "").strip()
        if not school_id:
            raise unauthorized("学校侧请求必须携带 X-School-Id 头")
        return school_id

    def _require_operator(self) -> None:
        token = self.headers.get("X-Operator-Token", "").strip()
        if not self.operator_token or token != self.operator_token:
            raise unauthorized("运营接口需要有效的 X-Operator-Token")

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, DomainError):
            self._send_json(exc.to_dict(), exc.http_status)
        else:
            self._send_json(
                {"code": "服务器内部错误", "message": str(exc), "details": []},
                500,
            )

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
        return

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            if path == "/api/sessions":
                self._school_id()
                self._send_json({"sessions": self.service.list_sessions()})
            elif path == "/api/requests":
                school_id = self._school_id()
                self._send_json({"requests": self.service.list_my_requests(school_id)})
            elif (m := PATH_REQUEST.match(path)):
                school_id = self._school_id()
                request = self.service.get_request(m.group(1), school_id)
                self._send_json(request.to_dict(self.service._sessions))
            elif path == "/admin/requests":
                self._require_operator()
                self._send_json({"requests": self.service.list_all_requests()})
            elif (m := PATH_ADMIN_REQUEST.match(path)):
                self._require_operator()
                self._send_json(self.service.explain(m.group(1)))
            elif path == "/admin/waitlist":
                self._require_operator()
                sid = query.get("session_id", [None])[0]
                self._send_json({"waitlist": self.service.waitlist_overview(sid)})
            elif path == "/admin/sessions":
                self._require_operator()
                self._send_json({"sessions": self.service.list_sessions()})
            else:
                self._send_json({"code": "未找到", "message": f"无此路径：{path}",
                                 "details": []}, 404)
        except Exception as exc:  # noqa: BLE001
            self._handle_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path
            data = self._read_json()

            if path == "/api/requests":
                school_id = self._school_id()
                request, replayed = self.service.apply(
                    school_id=school_id,
                    school_name=str(data.get("school_name", "")),
                    contact=str(data.get("contact", "")),
                    headcount=data.get("headcount"),
                    grades=str(data.get("grades", "")),
                    theme=str(data.get("theme", "")),
                    accessibility=data.get("accessibility") or [],
                    candidate_session_ids=data.get("candidate_session_ids") or [],
                    idempotency_key=data.get("idempotency_key"),
                    waitlist_if_full=bool(data.get("waitlist_if_full", False)),
                )
                self._send_json(
                    {"request": request.to_dict(self.service._sessions),
                     "replayed": replayed},
                    200 if replayed else 201,
                )
            elif (m := PATH_CONFIRM.match(path)):
                school_id = self._school_id()
                sid = str(data.get("session_id", "")).strip()
                if not sid:
                    raise DomainError("参数无效", "缺少 session_id", 400)
                request = self.service.confirm(m.group(1), sid, school_id)
                self._send_json({"request": request.to_dict(self.service._sessions)})
            elif (m := PATH_REDUCE.match(path)):
                school_id = self._school_id()
                request = self.service.reduce(
                    m.group(1), data.get("headcount"), school_id
                )
                self._send_json({"request": request.to_dict(self.service._sessions)})
            elif (m := PATH_CANCEL.match(path)):
                school_id = self._school_id()
                request = self.service.cancel(
                    m.group(1), school_id, str(data.get("reason") or "学校主动取消")
                )
                self._send_json({"request": request.to_dict(self.service._sessions)})
            elif (m := PATH_RESCHEDULE.match(path)):
                school_id = self._school_id()
                request = self.service.reschedule(
                    m.group(1),
                    data.get("candidate_session_ids") or [],
                    school_id,
                    bool(data.get("waitlist_if_full", True)),
                )
                self._send_json({"request": request.to_dict(self.service._sessions)})
            elif (m := PATH_WAITLIST.match(path)):
                school_id = self._school_id()
                sid = str(data.get("session_id", "")).strip()
                if not sid:
                    raise DomainError("参数无效", "缺少 session_id", 400)
                entry = self.service.join_waitlist(
                    m.group(1), sid, school_id, str(data.get("note") or "")
                )
                self._send_json({"waitlist_entry": entry.to_dict()}, 201)
            elif (m := PATH_WAITLIST_LEAVE.match(path)):
                school_id = self._school_id()
                request = self.service.leave_waitlist(
                    m.group(1), m.group(2), school_id
                )
                self._send_json({"request": request.to_dict(self.service._sessions)})
            elif path == "/admin/sessions":
                self._require_operator()
                session = self.service.add_session(
                    session_id=str(data.get("id", "")).strip(),
                    date=str(data.get("date", "")).strip(),
                    label=str(data.get("label", "")).strip(),
                    capacity=data.get("capacity") if data.get("capacity") is not None else 0,
                    accessibility=set(data.get("accessibility") or []),
                )
                self._send_json({"session": session.to_dict()}, 201)
            elif path == "/admin/expire":
                self._require_operator()
                affected = self.service.expire_due()
                self._send_json({"expired_request_ids": affected})
            elif (m := PATH_ADMIN_EXECUTE.match(path)):
                self._require_operator()
                request = self.service.mark_in_session(m.group(1))
                self._send_json({"request": request.to_dict(self.service._sessions)})
            elif (m := PATH_ADMIN_SETTLE.match(path)):
                self._require_operator()
                request = self.service.mark_settled(m.group(1))
                self._send_json({"request": request.to_dict(self.service._sessions)})
            else:
                self._send_json({"code": "未找到", "message": f"无此路径：{path}",
                                 "details": []}, 404)
        except Exception as exc:  # noqa: BLE001
            self._handle_error(exc)


def build_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    *,
    service: BookingService | None = None,
    clock: Clock | None = None,
    operator_token: str = "ops-dev-token",
) -> ThreadingHTTPServer:
    service = service or create_service(clock=clock)

    handler = type("BoundBookingHandler", (BookingHandler,), {
        "service": service,
        "operator_token": operator_token,
    })
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.service = service  # type: ignore[attr-defined]
    return httpd
