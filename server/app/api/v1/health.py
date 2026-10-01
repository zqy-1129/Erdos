"""健康检查（含数据库连通性探测）。"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.api.deps import get_session
from app.core.envelope import Envelope, ok
from app.core.errors import DB_UNAVAILABLE, AppError
from app.core.logging import get_logger, request_id_var

router = APIRouter(tags=["health"])

logger = get_logger("erdos.server.health")


class HealthView(BaseModel):
    """健康状态视图。"""

    status: str
    version: str


@router.get("/health", response_model=Envelope[HealthView], summary="健康检查")
async def get_health(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Envelope[HealthView]:
    """服务与依赖（数据库）连通性检查，供负载均衡与探活使用。"""
    try:
        await session.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.exception("数据库健康检查失败")
        raise AppError(DB_UNAVAILABLE) from exc
    return ok(
        HealthView(status="ok", version=__version__),
        request_id_var.get(),
    )