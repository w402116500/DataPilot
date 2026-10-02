from __future__ import annotations

import logging
from typing import Any

from contracts.api import ApiEnvelope, ErrorBody
from contracts.datalink import DataLinkErrorCode
from contracts.errors import ErrorCode
from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class DataLinkServiceError(Exception):
    """DataLink 的预期失败，携带可安全暴露给调用方的稳定错误码。"""

    def __init__(
        self,
        code: DataLinkErrorCode,
        message: str,
        *,
        status_code: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(message)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "req_unknown")


def _error_response(
    request: Request,
    *,
    code: DataLinkErrorCode | ErrorCode,
    message: str,
    status_code: int,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """按主后端的统一信封返回错误，避免把异常对象直接暴露出去。"""

    envelope = ApiEnvelope[Any](
        data=None,
        request_id=_request_id(request),
        error=ErrorBody(code=str(code), message=message, details=details or {}),
    )
    return JSONResponse(status_code=status_code, content=jsonable_encoder(envelope))


async def handle_datalink_error(request: Request, exc: DataLinkServiceError) -> JSONResponse:
    """将可预期的服务错误映射为稳定、安全的 HTTP 响应。"""

    return _error_response(
        request,
        code=exc.code,
        message=exc.message,
        status_code=exc.status_code,
        details=exc.details,
    )


async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """返回字段位置和校验类别，不回显请求中的潜在敏感值。"""

    path_error = any(
        error.get("type") == "value_error"
        and tuple(error.get("loc", ()))[:1] == ("body",)
        and tuple(error.get("loc", ()))[-1:] == ("source_ref",)
        for error in exc.errors()
    )
    details = {
        "errors": [
            {
                "loc": list(error.get("loc", ())),
                "msg": error.get("msg", "Invalid value"),
                "type": error.get("type", "validation_error"),
            }
            for error in exc.errors()
        ]
    }
    return _error_response(
        request,
        code=DataLinkErrorCode.PATH_OUTSIDE_ROOT if path_error else ErrorCode.VALIDATION_ERROR,
        message=(
            "Data source path reference is invalid" if path_error else "Request validation failed"
        ),
        status_code=400 if path_error else 422,
        details=details,
    )


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """隐藏未预期异常的具体内容，避免错误链泄露路径或配置。"""

    logger.error("Unhandled DataLink error", extra={"request_id": _request_id(request)})
    return _error_response(
        request,
        code=ErrorCode.INTERNAL_ERROR,
        message="Internal server error",
        status_code=500,
    )
