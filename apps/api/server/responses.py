from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from contracts.api import ApiEnvelope, ErrorBody
from contracts.errors import ErrorCode
from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse


def encode_utc_datetime(value: datetime) -> str:
    """Metadata 时间按 UTC ISO 8601 输出并带 Z。

    SQLite 读回的 DateTime 常丢失 tzinfo；无时区 ISO 会被浏览器当成本地时间。
    """

    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat().replace("+00:00", "Z")


def _json_content(envelope: ApiEnvelope[Any]) -> Any:
    return jsonable_encoder(envelope, custom_encoder={datetime: encode_utc_datetime})


def get_request_id(request: Request) -> str:
    """读取本次请求的 request_id，兜底值只用于中间件未执行的测试场景。"""

    return getattr(request.state, "request_id", "req_unknown")


def success_response(request: Request, data: Any, *, status_code: int = 200) -> JSONResponse:
    """返回统一成功信封，除 health、SSE 和下载外的 JSON API 都使用它。"""

    envelope = ApiEnvelope[Any](data=data, request_id=get_request_id(request), error=None)
    return JSONResponse(status_code=status_code, content=_json_content(envelope))


def error_response(
    request: Request,
    *,
    code: ErrorCode | str,
    message: str,
    status_code: int,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """返回统一错误信封，保证前端始终能拿到稳定错误码和 request_id。"""

    envelope = ApiEnvelope[Any](
        data=None,
        request_id=get_request_id(request),
        error=ErrorBody(code=str(code), message=message, details=details or {}),
    )
    return JSONResponse(status_code=status_code, content=_json_content(envelope))
