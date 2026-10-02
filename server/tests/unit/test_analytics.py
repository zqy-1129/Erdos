"""行为分析口径正确性测试（SP4-2）：构造已知漏斗数据集回放，看板数值与手算一致。"""

from datetime import UTC, datetime, timedelta

from app.domain.analytics.ports import conversion_rate
from app.domain.analytics.service import AnalyticsService
from app.repository.analytics import SQLAlchemyAnalyticsSource
from app.repository.models import Account, TelemetryEventRecord
from app.repository.uow import UnitOfWork


def _seed_telemetry(session) -> None:
    """构造已知数据集：
    - 3 个用户（u1/u2/u3）
    - u1/u2 跑过 analysis 阶段（首题）
    - u1 支付成功 2 次（复购），u2 支付成功 1 次
    - 阶段成功率：analysis 3 start / 2 success
    """
    now = datetime.now(UTC)
    # 遥测事件
    session.add_all([
        # u1：首题 + 复购（2 次支付）
        TelemetryEventRecord(event_name="stage_start", distinct_id="u1", props={"stage": "analysis"}, event_ts=now),
        TelemetryEventRecord(event_name="stage_success", distinct_id="u1", props={"stage": "analysis"}, event_ts=now),
        TelemetryEventRecord(event_name="pay_success", distinct_id="u1", props={}, event_ts=now),
        TelemetryEventRecord(event_name="pay_success", distinct_id="u1", props={}, event_ts=now + timedelta(days=1)),
        # u2：首题 + 单次支付
        TelemetryEventRecord(event_name="stage_start", distinct_id="u2", props={"stage": "analysis"}, event_ts=now),
        TelemetryEventRecord(event_name="stage_success", distinct_id="u2", props={"stage": "analysis"}, event_ts=now),
        TelemetryEventRecord(event_name="pay_success", distinct_id="u2", props={}, event_ts=now),
        # u3：仅启动 analysis 未成功
        TelemetryEventRecord(event_name="stage_start", distinct_id="u3", props={"stage": "analysis"}, event_ts=now),
    ])
    # 注册用户
    session.add_all([
        Account(email="u1@x.com", password_hash="h", status="active", role="user"),
        Account(email="u2@x.com", password_hash="h", status="active", role="user"),
        Account(email="u3@x.com", password_hash="h", status="active", role="user"),
    ])


def test_conversion_rate_basic() -> None:
    """转化率基础：分子/分母，分母为 0 返回 0。"""
    assert conversion_rate(2, 3) == 0.6667
    assert conversion_rate(0, 5) == 0.0
    assert conversion_rate(5, 0) == 0.0


async def test_funnel_counts_match_hand_calc(session_factory) -> None:
    """漏斗四步计数与手算一致。"""
    async with UnitOfWork(session_factory) as uow:
        _seed_telemetry(uow.session)
        await uow.session.flush()
        funnel = await AnalyticsService(SQLAlchemyAnalyticsSource(uow.session)).funnel()
        counts = {s["name"]: s["count"] for s in funnel["steps"]}
        # 手算：注册 3、首题 3（stage_start distinct）、付费 2（pay distinct）、复购 1（u1 支付 2 次）
        assert counts["注册"] == 3
        assert counts["首题"] == 3
        assert counts["付费"] == 2
        assert counts["复购"] == 1


async def test_stage_conversion_match_hand_calc(session_factory) -> None:
    """阶段成功率：analysis 3 start / 2 success = 66.67%。"""
    async with UnitOfWork(session_factory) as uow:
        _seed_telemetry(uow.session)
        await uow.session.flush()
        source = SQLAlchemyAnalyticsSource(uow.session)
        conversion = await source.stage_conversion()
        start, success = conversion["analysis"]
        assert start == 3
        assert success == 2
        assert conversion_rate(success, start) == 0.6667


async def test_pay_conversion_match_hand_calc(session_factory) -> None:
    """付费转化率：order_created / pay_success。"""
    async with UnitOfWork(session_factory) as uow:
        _seed_telemetry(uow.session)
        # 补下单事件
        uow.session.add(TelemetryEventRecord(event_name="order_created", distinct_id="u1", props={}, event_ts=datetime.now(UTC)))
        await uow.session.flush()
        source = SQLAlchemyAnalyticsSource(uow.session)
        orders, paid = await source.pay_conversion()
        assert orders == 1
        assert paid == 3  # 3 次 pay_success（u1 两次 + u2 一次）
        assert conversion_rate(paid, orders) == 3.0


async def test_dashboard_metrics_complete(session_factory) -> None:
    """看板 6 块指标完整聚合。"""
    async with UnitOfWork(session_factory) as uow:
        _seed_telemetry(uow.session)
        await uow.session.flush()
        m = await AnalyticsService(SQLAlchemyAnalyticsSource(uow.session)).dashboard(datetime.now(UTC) - timedelta(days=1))
        assert m.dau == 3  # 3 个 distinct user
        assert m.pay_conversion_rate == 0.0  # 无下单，转化率为 0
        assert m.stage_success_rates["analysis"] == 0.6667
        assert m.points_consumed == 0  # 无积分消耗
