from __future__ import annotations

import logging

from contracts.errors import AppError, ErrorCode
from fastapi import Request
from fastapi.exceptions import RequestValidationError

from server.responses import error_response

logger = logging.getLogger(__name__)


async def handle_app_error(request: Request, exc: AppError):
    return error_response(
        request,
        code=exc.code,
        message=exc.message,
        status_code=exc.status_code,
        details=exc.details,
    )


async def handle_validation_error(request: Request, exc: RequestValidationError):
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
    return error_response(
        request,
        code=ErrorCode.VALIDATION_ERROR,
        message="Request validation failed",
        status_code=422,
        details=details,
    )


async def handle_unexpected_error(request: Request, exc: Exception):
    logger.exception(
        "Unhandled API error",
        extra={"request_id": getattr(request.state, "request_id", None)},
    )
    return error_response(
        request,
        code=ErrorCode.INTERNAL_ERROR,
        message="Internal server error",
        status_code=500,
    )
