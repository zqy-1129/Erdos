"""报表仓储落库实现（看板 P3 积分分析 + P4 资产明细，只读聚合）。

时间聚合用 DB 侧 date() 函数（SQLite/PostgreSQL 双驱动可用），
口径为存储 UTC 日；SQLite 返回字符串日期，读回时按 date.fromisoformat 解析。
金额一律整数分；全聚合结果保证非负（coalesce 兜底空表）。
"""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.reporting.ports import (
    BillingReportingRepository,
    BillingSummary,
    LedgerKindCount,
    PointsDailyPoint,
    PointsReportingRepository,
    PointsSummary,
    ProductSales,
    RevenueDailyPoint,
)
from app.repository.models import (
    Order,
    PointAccount,
    PointLedger,
    Product,
    Subscription,
)

_PAID_STATUSES = ("paid", "refunded")


def _day_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """[start, end] 闭区间转 UTC 半开区间 [start 00:00, end+1d 00:00)。"""
    start_dt = datetime.combine(start, time.min, tzinfo=UTC)
    end_dt = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
    return start_dt, end_dt


def _parse_date(value: object) -> date:
    """date() 函数返回值归一化（SQLite 为 str，PostgreSQL 为 date）。"""
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _to_int(value: int | Decimal | None) -> int:
    return int(value or 0)


class SQLAlchemyPointsReportingRepository(PointsReportingRepository):
    """point_accounts / point_ledgers 聚合实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def summary(self) -> PointsSummary:
        account_row = (
            await self._session.execute(
                select(
                    func.coalesce(func.sum(PointAccount.purchased_balance), 0),
                    func.coalesce(func.sum(PointAccount.monthly_balance), 0),
                    func.count(PointAccount.user_id),
                    func.coalesce(
                        func.sum(case((PointAccount.frozen.is_(True), 1), else_=0)), 0
                    ),
                )
            )
        ).one()
        reserved_points = (
            await self._session.execute(
                select(func.coalesce(func.sum(PointLedger.delta), 0)).where(
                    PointLedger.status == "reserved"
                )
            )
        ).scalar_one()
        ledger_count = (
            await self._session.execute(
                select(func.count(PointLedger.id)).select_from(PointLedger)
            )
        ).scalar_one()
        offline_since = datetime.now(UTC) - timedelta(hours=24)
        offline_sync = (
            await self._session.execute(
                select(func.count(PointLedger.id))
                .select_from(PointLedger)
                .where(
                    PointLedger.kind == "offline_sync",
                    PointLedger.created_at >= offline_since,
                )
            )
        ).scalar_one()
        return PointsSummary(
            total_purchased=_to_int(account_row[0]),
            total_monthly=_to_int(account_row[1]),
            account_count=_to_int(account_row[2]),
            frozen_count=_to_int(account_row[3]),
            reserved_points=_to_int(reserved_points),
            ledger_count=_to_int(ledger_count),
            offline_sync_last_24h=_to_int(offline_sync),
        )

    async def daily_series(self, start: date, end: date) -> list[PointsDailyPoint]:
        start_dt, end_dt = _day_bounds(start, end)
        day_col = func.date(PointLedger.created_at).label("stat_date")
        stmt = (
            select(
                day_col,
                func.coalesce(
                    func.sum(case((PointLedger.kind == "grant", PointLedger.delta), else_=0)),
                    0,
                ).label("granted"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                (PointLedger.kind == "reserve")
                                & (PointLedger.status == "confirmed"),
                                PointLedger.delta,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("consumed"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                (PointLedger.kind == "reserve")
                                & (PointLedger.status == "refunded"),
                                PointLedger.delta,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("refunded"),
            )
            .where(PointLedger.created_at >= start_dt, PointLedger.created_at < end_dt)
            .group_by(day_col)
            .order_by(day_col)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            PointsDailyPoint(
                stat_date=_parse_date(row.stat_date),
                granted=_to_int(row.granted),
                consumed=_to_int(row.consumed),
                refunded=_to_int(row.refunded),
            )
            for row in rows
        ]

    async def kind_distribution(self) -> list[LedgerKindCount]:
        stmt = (
            select(
                PointLedger.kind,
                PointLedger.status,
                func.count(PointLedger.id).label("cnt"),  # 避免与 Row.count() 方法重名
            )
            .group_by(PointLedger.kind, PointLedger.status)
            .order_by(PointLedger.kind, func.count(PointLedger.id).desc(), PointLedger.status)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            LedgerKindCount(kind=row.kind, status=row.status, count=_to_int(row.cnt))
            for row in rows
        ]


class SQLAlchemyBillingReportingRepository(BillingReportingRepository):
    """orders / subscriptions / products 聚合实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def summary(self) -> BillingSummary:
        order_row = (
            await self._session.execute(
                select(
                    func.coalesce(
                        func.sum(
                            case((Order.status.in_(_PAID_STATUSES), Order.price_cents), else_=0)
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            case((Order.status == "refunded", Order.price_cents), else_=0)
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(case((Order.status == "paid", 1), else_=0)), 0
                    ),
                    func.coalesce(
                        func.sum(case((Order.status == "refunded", 1), else_=0)), 0
                    ),
                    func.coalesce(
                        func.sum(case((Order.status == "closed", 1), else_=0)), 0
                    ),
                )
            )
        ).one()
        subscription_row = (
            await self._session.execute(
                select(
                    func.coalesce(
                        func.sum(case((Subscription.status == "active", 1), else_=0)), 0
                    ),
                    func.coalesce(
                        func.sum(case((Subscription.status == "expired", 1), else_=0)), 0
                    ),
                )
            )
        ).one()
        return BillingSummary(
            gmv_cents=_to_int(order_row[0]),
            refunded_cents=_to_int(order_row[1]),
            paid_order_count=_to_int(order_row[2]),
            refunded_order_count=_to_int(order_row[3]),
            closed_order_count=_to_int(order_row[4]),
            active_subscriptions=_to_int(subscription_row[0]),
            expired_subscriptions=_to_int(subscription_row[1]),
        )

    async def daily_revenue(self, start: date, end: date) -> list[RevenueDailyPoint]:
        start_dt, end_dt = _day_bounds(start, end)
        day_col = func.date(Order.paid_at).label("stat_date")
        stmt = (
            select(
                day_col,
                func.coalesce(
                    func.sum(
                        case((Order.status == "refunded", 0), else_=Order.price_cents)
                    ),
                    0,
                ).label("revenue_cents"),
                func.coalesce(
                    func.sum(
                        case((Order.status == "refunded", Order.price_cents), else_=0)
                    ),
                    0,
                ).label("refund_cents"),
            )
            .where(Order.paid_at.is_not(None), Order.paid_at >= start_dt, Order.paid_at < end_dt)
            .group_by(day_col)
            .order_by(day_col)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            RevenueDailyPoint(
                stat_date=_parse_date(row.stat_date),
                revenue_cents=_to_int(row.revenue_cents),
                refund_cents=_to_int(row.refund_cents),
            )
            for row in rows
        ]

    async def product_sales(self) -> list[ProductSales]:
        stmt = (
            select(
                Product.code.label("product_code"),
                Product.name.label("product_name"),
                func.count(Order.id).label("order_count"),
                func.coalesce(func.sum(Order.price_cents), 0).label("amount_cents"),
            )
            .join(Product, Order.product_id == Product.id)
            .where(Order.status.in_(_PAID_STATUSES))
            .group_by(Product.code, Product.name)
            .order_by(func.sum(Order.price_cents).desc(), Product.code)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            ProductSales(
                product_code=row.product_code,
                product_name=row.product_name,
                order_count=_to_int(row.order_count),
                amount_cents=_to_int(row.amount_cents),
            )
            for row in rows
        ]