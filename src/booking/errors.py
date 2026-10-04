"""领域错误类型。

所有业务错误都携带稳定的机器可读 ``code``、中文说明与对应的 HTTP 状态码，
便于电话受理端与运营接口统一处理。
"""
from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """业务规则错误基类。"""

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int = 400,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or []

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


def bad_request(message: str, code: str = "参数无效", details: Any | None = None) -> DomainError:
    return DomainError(code, message, 400, details)


def unauthorized(message: str = "缺少或未识别的访问凭证", code: str = "未认证") -> DomainError:
    return DomainError(code, message, 401)


def not_found(message: str = "预约不存在", code: str = "未找到") -> DomainError:
    # 学校越权访问他人预约时同样返回 404，避免预约存在性泄漏（租户隔离）。
    return DomainError(code, message, 404)


def conflict(message: str, code: str = "状态冲突", details: Any | None = None) -> DomainError:
    return DomainError(code, message, 409, details)
