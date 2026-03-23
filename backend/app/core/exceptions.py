from __future__ import annotations

from typing import Any, Optional, cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    def __init__(
        self,
        message: str,
        statusCode: int = 400,
        code: str = "bad_request",
        detail: Optional[Any] = None,
    ):
        super().__init__(message)
        self.message = message
        self.statusCode = statusCode
        self.code = code
        self.detail = detail


class ServiceConfigurationError(AppError):
    def __init__(self, message: str, detail: Optional[Any] = None):
        super().__init__(message, statusCode=500, code="service_configuration_error", detail=detail)


class UpstreamServiceError(AppError):
    def __init__(self, message: str, detail: Optional[Any] = None):
        super().__init__(message, statusCode=502, code="upstream_service_error", detail=detail)


async def appErrorHandler(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.statusCode,
        content={"code": exc.code, "message": exc.message, "detail": exc.detail},
    )


async def genericExceptionHandler(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "code": "internal_server_error",
            "message": "服务内部异常，请检查后端日志。",
            "detail": {"reason": str(exc)},
        },
    )


def registerExceptionHandlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, cast(Any, appErrorHandler))
    app.add_exception_handler(Exception, cast(Any, genericExceptionHandler))
