"""统一响应信封（与 contracts/openapi.yaml 的 Envelope schema 对齐）。"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.core.errors import OK, ErrorSpec


class Envelope[T](BaseModel):
    """统一的 API 响应信封。

    - code=0 表示成功，data 携带业务数据；
    - code 非 0 取自契约错误码（app.core.errors.ErrorSpec）。
    """

    code: int = Field(description="业务码，0 表示成功")
    message: str = Field(description="面向使用者的提示文案")
    detail: str | None = Field(default=None, description="可选的错误上下文")
    data: T | None = Field(default=None, description="业务数据")
    request_id: str = Field(description="请求追踪 ID")
    timestamp: datetime = Field(description="服务端 UTC 时间")


def ok[T](data: T | None, request_id: str, timestamp: datetime | None = None) -> Envelope[T]:
    """构造成功信封。"""
    return Envelope(
        code=OK.code,
        message=OK.message,
        data=data,
        request_id=request_id,
        timestamp=timestamp or _now(),
    )


def fail(
    spec: ErrorSpec,
    request_id: str,
    detail: str | None = None,
    timestamp: datetime | None = None,
) -> Envelope[None]:
    """构造失败信封（code/message 取自契约错误码）。"""
    return Envelope(
        code=spec.code,
        message=spec.message,
        detail=detail,
        data=None,
        request_id=request_id,
        timestamp=timestamp or _now(),
    )


def _now() -> datetime:
    from app.core.clock import utc_now

    return utc_now()