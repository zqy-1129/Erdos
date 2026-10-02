"""看板运营报表接口（SP2-4 收尾 P3 积分分析 + SP2-5 收尾 P4 资产明细）。

访问控制：与管理端看板一致，全部接口强制凭证 + dashboard_admin_roles 角色（FR-6）。
日序列零值填充：请求窗口内无数据的天补 0，保证前端折线连续。
金额字段一律整数分（分）；展示侧负责换算为元。
"""

from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session, require_dashboard_admin
from app.core.envelope import Envelope, ok
from app.core.errors import BAD_REQUEST, AppError
from app.core.logging import request_id_var
from app.infra.auth import Principal
from app.repository.reporting import (
    SQLAlchemyBillingReportingRepository,
    SQLAlchemyPointsReportingRepository,
)

router = APIRouter(prefix="/admin/dashboard", tags=["dashboard"])


class PointsSummaryView(BaseModel):
    """积分域总览视图。"""

    total_balance: int
    total_purchased: int
    total_monthly: int
    account_count: int
    frozen_count: int
    reserved_points: int
    ledger_count: int
    offline_sync_last_24h: int


class PointsDailyView(BaseModel):
    """积分日聚合点视图。"""

    stat_date: date
    granted: int
    consumed: int
    refunded: int


class PointsTrendView(BaseModel):
    """积分日趋势视图。"""

    days: int
    series: list[PointsDailyView]


class PointDistributionRow(BaseModel):
    """流水类型×状态分布行。"""

    kind: str
    status: str
    count: int


class PointsDistributionView(BaseModel):
    """流水分布视图。"""

    items: list[PointDistributionRow]


class BillingSummaryView(BaseModel):
    """计费域总览视图（金额为分）。"""

    gmv_cents: int
    refunded_cents: int
    paid_order_count: int
    refunded_order_count: int
    closed_order_count: int
    active_subscriptions: int
    expired_subscriptions: int


class RevenueDailyView(BaseModel):
    """收入日聚合点视图（金额为分）。"""

    stat_date: date
    revenue_cents: int
    refund_cents: int


class RevenueTrendView(BaseModel):
    """收入日趋势视图。"""

    days: int
    series: list[RevenueDailyView]


class ProductSalesView(BaseModel):
    """商品销售分布行（金额为分）。"""

    product_code: str
    product_name: str
    order_count: int
    amount_cents: int


class ProductSalesListView(BaseModel):
    """商品销售分布视图。"""

    items: list[ProductSalesView]


def _zero_fill(days: int, end: date) -> list[date]:
    """最近 days 天（含今天）的日期序列。"""
    start = end - timedelta(days=days - 1)
    return [start + timedelta(days=i) for i in range(days)]


def _today_utc() -> date:
    """UTC 当日（与 DB 侧 date() 聚合口径一致）。"""
    return datetime.now(UTC).date()


def _parse_days(days: int) -> int:
    if not 1 <= days <= 90:
        raise AppError(BAD_REQUEST, detail="days 仅支持 1~90")
    return days


@router.get(
    "/points/summary",
    response_model=Envelope[PointsSummaryView],
    summary="积分域总览（P3）",
)
async def points_summary(
    admin: Annotated[Principal, Depends(require_dashboard_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Envelope[PointsSummaryView]:
    """积分域运营总览：双余额总量/挂起预扣/冻结账户/流水量与 24h 离线补报。"""
    repo = SQLAlchemyPointsReportingRepository(session)
    s = await repo.summary()
    return ok(
        PointsSummaryView(
            total_balance=s.total_balance,
            total_purchased=s.total_purchased,
            total_monthly=s.total_monthly,
            account_count=s.account_count,
            frozen_count=s.frozen_count,
            reserved_points=s.reserved_points,
            ledger_count=s.ledger_count,
            offline_sync_last_24h=s.offline_sync_last_24h,
        ),
        request_id_var.get(),
    )


@router.get(
    "/points/trend",
    response_model=Envelope[PointsTrendView],
    summary="积分发放/消耗/退还日趋势（P3）",
)
async def points_trend(
    admin: Annotated[Principal, Depends(require_dashboard_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    days: int = 30,
) -> Envelope[PointsTrendView]:
    """最近 days 天积分日聚合（无数据日补 0）。"""
    days = _parse_days(days)
    end = _today_utc()
    repo = SQLAlchemyPointsReportingRepository(session)
    rows = await repo.daily_series(end - timedelta(days=days - 1), end)
    by_day = {r.stat_date: r for r in rows}
    series = [
        PointsDailyView(
            stat_date=day,
            granted=by_day[day].granted if day in by_day else 0,
            consumed=by_day[day].consumed if day in by_day else 0,
            refunded=by_day[day].refunded if day in by_day else 0,
        )
        for day in _zero_fill(days, end)
    ]
    return ok(PointsTrendView(days=days, series=series), request_id_var.get())


@router.get(
    "/points/distribution",
    response_model=Envelope[PointsDistributionView],
    summary="积分流水类型×状态分布（P3）",
)
async def points_distribution(
    admin: Annotated[Principal, Depends(require_dashboard_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Envelope[PointsDistributionView]:
    """流水类型×状态计数分布（资金域审计辅助视图）。"""
    repo = SQLAlchemyPointsReportingRepository(session)
    items = await repo.kind_distribution()
    return ok(
        PointsDistributionView(
            items=[
                PointDistributionRow(kind=i.kind, status=i.status, count=i.count)
                for i in items
            ]
        ),
        request_id_var.get(),
    )


@router.get(
    "/billing/summary",
    response_model=Envelope[BillingSummaryView],
    summary="计费资产总览（P4）",
)
async def billing_summary(
    admin: Annotated[Principal, Depends(require_dashboard_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Envelope[BillingSummaryView]:
    """计费域资产总览：GMV/退款/订单状态计数/订阅状态计数。"""
    repo = SQLAlchemyBillingReportingRepository(session)
    s = await repo.summary()
    return ok(
        BillingSummaryView(
            gmv_cents=s.gmv_cents,
            refunded_cents=s.refunded_cents,
            paid_order_count=s.paid_order_count,
            refunded_order_count=s.refunded_order_count,
            closed_order_count=s.closed_order_count,
            active_subscriptions=s.active_subscriptions,
            expired_subscriptions=s.expired_subscriptions,
        ),
        request_id_var.get(),
    )


@router.get(
    "/billing/revenue",
    response_model=Envelope[RevenueTrendView],
    summary="收入/退款日趋势（P4）",
)
async def billing_revenue(
    admin: Annotated[Principal, Depends(require_dashboard_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    days: int = 30,
) -> Envelope[RevenueTrendView]:
    """最近 days 天收入/退款日聚合（无数据日补 0，金额为分）。"""
    days = _parse_days(days)
    end = _today_utc()
    repo = SQLAlchemyBillingReportingRepository(session)
    rows = await repo.daily_revenue(end - timedelta(days=days - 1), end)
    by_day = {r.stat_date: r for r in rows}
    series = [
        RevenueDailyView(
            stat_date=day,
            revenue_cents=by_day[day].revenue_cents if day in by_day else 0,
            refund_cents=by_day[day].refund_cents if day in by_day else 0,
        )
        for day in _zero_fill(days, end)
    ]
    return ok(RevenueTrendView(days=days, series=series), request_id_var.get())


@router.get(
    "/billing/products",
    response_model=Envelope[ProductSalesListView],
    summary="商品销售分布（P4）",
)
async def billing_products(
    admin: Annotated[Principal, Depends(require_dashboard_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Envelope[ProductSalesListView]:
    """商品维度销售分布（已支付口径，金额为分）。"""
    repo = SQLAlchemyBillingReportingRepository(session)
    items = await repo.product_sales()
    return ok(
        ProductSalesListView(
            items=[
                ProductSalesView(
                    product_code=i.product_code,
                    product_name=i.product_name,
                    order_count=i.order_count,
                    amount_cents=i.amount_cents,
                )
                for i in items
            ]
        ),
        request_id_var.get(),
    )