"""行为分析看板接口（SP4-2）：漏斗 + 看板 6 块指标。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_session_factory, require_roles
from app.core.clock import utc_now
from app.core.envelope import Envelope, ok
from app.core.logging import request_id_var
from app.domain.analytics.service import AnalyticsService
from app.infra.auth import Principal
from app.repository.analytics import SQLAlchemyAnalyticsSource
from app.repository.uow import UnitOfWork

router = APIRouter(prefix="/analytics", tags=["analytics"])

admin_dep = Annotated[Principal, Depends(require_roles("admin", "operator"))]


class FunnelView(BaseModel):
    steps: list[dict]
    conversion_rates: dict


class DashboardView(BaseModel):
    dau: int
    new_users: int
    pay_conversion_rate: float
    points_consumed: int
    stage_success_rates: dict
    content_usage: int


@router.get("/funnel", response_model=Envelope[FunnelView], summary="转化漏斗")
async def funnel(
    request: Request,
    principal: admin_dep,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[FunnelView]:
    """漏斗四步（注册→首题→付费→复购）。"""
    async with UnitOfWork(session_factory) as uow:
        result = await AnalyticsService(SQLAlchemyAnalyticsSource(uow.session)).funnel()
    return ok(FunnelView(**result), request_id_var.get())


@router.get("/dashboard", response_model=Envelope[DashboardView], summary="运营看板 6 块")
async def dashboard(
    request: Request,
    principal: admin_dep,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[DashboardView]:
    """看板 6 块指标（DAU/新增/付费转化/积分消耗/阶段成功率/内容使用）。"""
    since = utc_now()
    async with UnitOfWork(session_factory) as uow:
        m = await AnalyticsService(SQLAlchemyAnalyticsSource(uow.session)).dashboard(since)
        view = DashboardView(
            dau=m.dau,
            new_users=m.new_users,
            pay_conversion_rate=m.pay_conversion_rate,
            points_consumed=m.points_consumed,
            stage_success_rates=m.stage_success_rates,
            content_usage=m.content_usage,
        )
    return ok(view, request_id_var.get())
