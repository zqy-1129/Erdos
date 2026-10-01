"""全局异常处理：一切异常收敛为统一信封（错误码严格取自契约）。

- AppError：业务异常，直接映射对应错误码
- RequestValidationError：参数校验失败 -> BAD_REQUEST
- HTTPException（含路由 404/405）：按状态码映射契约错误码
- 未捕获异常：记录完整堆栈 -> INTERNAL_ERROR（不泄漏内部细节）
"""

from typing import cast

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.envelope import fail
from app.core.errors import (
    BAD_REQUEST,
    INTERNAL_ERROR,
    METHOD_NOT_ALLOWED,
    NOT_FOUND,
    PERMISSION_DENIED,
    UNAUTHENTICATED,
    AppError,
    ErrorSpec,
)
from app.core.logging import get_logger, request_id_var

logger = get_logger("erdos.server.api")

_HTTP_STATUS_TO_SPEC: dict[int, ErrorSpec] = {
    401: UNAUTHENTICATED,
    403: PERMISSION_DENIED,
    404: NOT_FOUND,
    405: METHOD_NOT_ALLOWED,
}


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """业务异常 -> 契约错误码信封。"""
    err = cast(AppError, exc)  # Starlette 按注册类型分发，此处必为 AppError
    return _response(request, err.spec, detail=err.detail)


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI 参数校验失败 -> BAD_REQUEST（detail 摘要注入）。"""
    validation = cast(RequestValidationError, exc)
    parts = []
    for err in validation.errors():
        loc = ".".join(str(part) for part in err.get("loc", ()))
        parts.append(f"{loc or 'body'}: {err.get('msg', 'invalid')}")
    return _response(request, BAD_REQUEST, detail="; ".join(parts))


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """框架层 HTTP 异常（路由不存在 / 方法不允许等）-> 信封。"""
    http_exc = cast(StarletteHTTPException, exc)
    spec = _HTTP_STATUS_TO_SPEC.get(http_exc.status_code, INTERNAL_ERROR)
    detail = str(http_exc.detail) if http_exc.detail and http_exc.status_code != 500 else None
    return _response(request, spec, detail=detail)


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底处理器：记录堆栈，返回通用内部错误（不泄漏内部实现细节）。"""
    logger.exception("unhandled exception", exc_info=exc)
    return _response(request, INTERNAL_ERROR)


def _response(request: Request, spec: ErrorSpec, detail: str | None = None) -> JSONResponse:
    """构造统一信封响应（响应头 X-Request-Id 由 RequestIdMiddleware 统一回显）。"""
    request_id = str(getattr(request.state, "request_id", None) or request_id_var.get())
    return JSONResponse(
        status_code=spec.http_status,
        content=fail(spec, request_id, detail=detail).model_dump(mode="json"),
    )