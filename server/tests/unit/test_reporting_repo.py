"""报表仓储聚合单元测试（看板 P3 积分分析 + P4 资产明细）。

直接以 ORM 模型种子数据，验证聚合口径：
- 积分：grant 发放为正值；reserve 行存负 delta，消耗/退还/挂起预扣取 -delta 为正值；
- 计费：GMV=paid/refunded 金额合计、退款单列、按 paid_at 日分组、商品聚合。
"""

from datetime import UTC, datetime, timedelta

from app.repository.models import (
    Order,
    PointAccount,
    PointLedger,
    Product,
    Subscription,
)
from app.repository.reporting import (
    SQLAlchemyBillingReportingRepository,
    SQLAlchemyPointsReportingRepository,
)

TODAY = datetime.now(UTC)
YESTERDAY = TODAY - timedelta(days=1)


async def _seed_points(session) -> None:
    session.add_all(
        [
            PointAccount(user_id="u1", purchased_balance=70, monthly_balance=0, frozen=False, version=0),
            PointAccount(user_id="u2", purchased_balance=0, monthly_balance=5, frozen=True, version=0),
        ]
    )
    session.add_all(
        [
            PointLedger(user_id="u1", exec_id="g1", delta=100, balance_type="purchased",
                        kind="grant", status="confirmed", source="register_gift",
                        created_at=TODAY),
            PointLedger(user_id="u1", exec_id="r1", delta=-30, balance_type="purchased",
                    kind="reserve", status="confirmed", source="stage", stage="analysis",
                    created_at=TODAY),
        PointLedger(user_id="u1", exec_id="r2", delta=-20, balance_type="purchased",
                    kind="reserve", status="reserved", source="stage", stage="solve",
                    created_at=TODAY),
        PointLedger(user_id="u2", exec_id="r3", delta=-40, balance_type="monthly",
                    kind="reserve", status="refunded", source="stage", stage="analysis",
                    created_at=YESTERDAY),
        PointLedger(user_id="u2", exec_id="g2", delta=500, balance_type="purchased",
                    kind="grant", status="confirmed", source="register_gift",
                    created_at=YESTERDAY),
        PointLedger(user_id="u2", exec_id="o1", delta=-10, balance_type="purchased",
                    kind="offline_sync", status="confirmed", source="offline",
                    created_at=TODAY),
        ]
    )
    await session.commit()


async def test_points_summary(session_factory) -> None:
    """总览指标：双余额合计/账户数/冻结数/挂起预扣/流水量/24h 离线补报。"""
    async with session_factory() as session:
        await _seed_points(session)
        repo = SQLAlchemyPointsReportingRepository(session)
        summary = await repo.summary()
    assert summary.total_purchased == 70
    assert summary.total_monthly == 5
    assert summary.total_balance == 75
    assert summary.account_count == 2
    assert summary.frozen_count == 1
    assert summary.reserved_points == 20  # 仅 r2 处于 reserved
    assert summary.ledger_count == 6
    assert summary.offline_sync_last_24h == 1


async def test_points_daily_series(session_factory) -> None:
    """日序列分组：今日 100 发放 / 40 消耗（reserve-confirmed 30 + offline_sync 10）；昨日 500 发放 / 40 退还。"""
    async with session_factory() as session:
        await _seed_points(session)
        repo = SQLAlchemyPointsReportingRepository(session)
        rows = await repo.daily_series(TODAY.date() - timedelta(days=3), TODAY.date())
    by_day = {r.stat_date: r for r in rows}
    today = by_day[TODAY.date()]
    assert (today.granted, today.consumed, today.refunded) == (100, 40, 0)
    yesterday = by_day[YESTERDAY.date()]
    assert (yesterday.granted, yesterday.consumed, yesterday.refunded) == (500, 0, 40)
    assert set(by_day) == {TODAY.date(), YESTERDAY.date()}  # 无数据日不产出行


async def test_points_kind_distribution(session_factory) -> None:
    """类型×状态分布：reserve 三态、grant、offline_sync 各一笔。"""
    async with session_factory() as session:
        await _seed_points(session)
        repo = SQLAlchemyPointsReportingRepository(session)
        items = await repo.kind_distribution()
    by_pair = {(i.kind, i.status): i.count for i in items}
    assert by_pair[("reserve", "reserved")] == 1
    assert by_pair[("reserve", "confirmed")] == 1
    assert by_pair[("reserve", "refunded")] == 1
    assert by_pair[("grant", "confirmed")] == 2
    assert by_pair[("offline_sync", "confirmed")] == 1


async def _seed_billing(session) -> None:
    session.add_all(
        [
            Product(id="p-sub", code="sub_monthly", type="subscription",
                    name="月度订阅", price_cents=2900, points=500, duration_days=30, active=True),
            Product(id="p-pack", code="pack_400", type="points_pack",
                    name="积分包 400", price_cents=3980, points=400, duration_days=0, active=True),
        ]
    )
    session.add_all(
        [
            Order(id="o1", user_id="u1", product_id="p-sub", price_cents=2900,
                  channel="mock", status="paid", idempotency_key="k1",
                  expires_at=TODAY + timedelta(minutes=30), paid_at=TODAY,
                  created_at=YESTERDAY),
            Order(id="o2", user_id="u2", product_id="p-pack", price_cents=3980,
                  channel="mock", status="refunded", idempotency_key="k2",
                  expires_at=TODAY + timedelta(minutes=30), paid_at=TODAY,
                  refunded_at=TODAY + timedelta(hours=1)),
            Order(id="o3", user_id="u3", product_id="p-sub", price_cents=2900,
                  channel="mock", status="paid", idempotency_key="k3",
                  expires_at=YESTERDAY + timedelta(minutes=30), paid_at=YESTERDAY),
            Order(id="o4", user_id="u4", product_id="p-sub", price_cents=2900,
                  channel="mock", status="closed", idempotency_key="k4",
                  expires_at=YESTERDAY),
        ]
    )
    session.add_all(
        [
            Subscription(id="s1", user_id="u1", plan="monthly", status="active",
                         start_at=TODAY, end_at=TODAY + timedelta(days=30),
                         created_at=TODAY),
            Subscription(id="s2", user_id="u2", plan="monthly", status="expired",
                         start_at=YESTERDAY - timedelta(days=40),
                         end_at=YESTERDAY - timedelta(days=10),
                         created_at=YESTERDAY),
        ]
    )
    await session.commit()


async def test_billing_summary(session_factory) -> None:
    """总览：GMV=三者合计、退款单列、订单状态计数与订阅计数。"""
    async with session_factory() as session:
        await _seed_billing(session)
        repo = SQLAlchemyBillingReportingRepository(session)
        summary = await repo.summary()
    assert summary.gmv_cents == 2900 + 3980 + 2900
    assert summary.refunded_cents == 3980
    assert summary.paid_order_count == 2
    assert summary.refunded_order_count == 1
    assert summary.closed_order_count == 1
    assert summary.active_subscriptions == 1
    assert summary.expired_subscriptions == 1


async def test_billing_daily_revenue(session_factory) -> None:
    """收入日序列：今日收入 2900 + 退款 3980（退款订单不计收入）；昨日收入 2900。"""
    async with session_factory() as session:
        await _seed_billing(session)
        repo = SQLAlchemyBillingReportingRepository(session)
        rows = await repo.daily_revenue(TODAY.date() - timedelta(days=2), TODAY.date())
    by_day = {r.stat_date: r for r in rows}
    today = by_day[TODAY.date()]
    assert (today.revenue_cents, today.refund_cents) == (2900, 3980)
    yesterday = by_day[YESTERDAY.date()]
    assert (yesterday.revenue_cents, yesterday.refund_cents) == (2900, 0)


async def test_billing_product_sales(session_factory) -> None:
    """商品聚合：月订 2 单 5800、积分包 1 单 3980，按金额降序。"""
    async with session_factory() as session:
        await _seed_billing(session)
        repo = SQLAlchemyBillingReportingRepository(session)
        items = await repo.product_sales()
    assert [i.product_code for i in items] == ["sub_monthly", "pack_400"]
    assert items[0].order_count == 2 and items[0].amount_cents == 5800
    assert items[1].order_count == 1 and items[1].amount_cents == 3980
    assert all(i.product_name for i in items)