"""SLO 月度可用性预算燃尽（SP4-3 收口）。

为什么补这一条：PRD/架构承诺的是**月度** 99.5%，而运行链路里只有采样侧的 60s 瞬时可用性——
`domain/alerts/slo.py` 的预算/燃尽纯函数一直是零调用方的"已实现待验收"。本批把它接成
每小时重算的调度任务，并钉住三件容易做错的事：

1. 无监测数据 ≠ 健康（覆盖率单独告警，不假装算出 100%）；
2. 观测分钟不足（部署首小时）不判燃尽，避免刚启动就 P0；
3. 同档持续违规不重复播报，但**恢复必须播报**（值班要知道什么时候好的）。
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.domain.alerts.ports import BusinessAlert
from app.domain.alerts.severity import SLO_BURN_METRIC, SLO_COVERAGE_METRIC, Severity
from app.domain.alerts.slo import (
    SLO_MIN_OBSERVED_MINUTES,
    coverage_of,
    evaluate_burn,
    monthly_downtime_budget,
)
from app.domain.monitoring.ports import MonitoringSample
from app.infra.scheduler_tasks import SloBurnState, run_slo_burn_task
from app.repository.monitoring import SQLAlchemyMonitoringTrendRepository

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


class RecordingOutlet:
    def __init__(self) -> None:
        self.emitted: list[BusinessAlert] = []

    async def emit(self, alert: BusinessAlert, now: datetime, dedupe: bool = True) -> bool:
        self.emitted.append(alert)
        return True


def _sample(error_rate: float) -> MonitoringSample:
    return MonitoringSample(
        sampled_at=NOW,
        qps=10.0,
        error_rate=error_rate,
        p50_ms=20.0,
        p95_ms=80.0,
        cpu_percent=10.0,
        memory_percent=30.0,
        rss_mb=64.0,
        db_query_p95_ms=30.0,
        db_pool_usage=0.1,
    )


async def _seed_minutes(
    session_factory, minutes: int, error_rate: float, *, end: datetime = NOW
) -> None:
    """写入 [end-minutes, end) 的分钟快照，模拟采样器已落库的窗口。"""
    async with session_factory() as session:
        repo = SQLAlchemyMonitoringTrendRepository(session)
        for index in range(minutes):
            minute = end - timedelta(minutes=minutes - index)
            await repo.upsert_minute(minute, _sample(error_rate))
        await session.commit()


# ----------------------------------------------------------------------
# 纯函数：预算份额与判级
# ----------------------------------------------------------------------
def test_daily_budget_is_one_thirtieth_of_month() -> None:
    """99.5% 月度承诺 = 30 天 3.6h 宕机预算；日窗口份额应正好是 1/30。"""
    month = monthly_downtime_budget(0.995, 30)
    day = monthly_downtime_budget(0.995, 1)
    assert month == pytest.approx(432 * 30)  # 12960s = 3.6h
    assert day == pytest.approx(432.0)


def test_evaluate_burn_tiers() -> None:
    """2 倍速 P0、1 倍速 P1、预算内不判级。"""
    # 864s 不可用（= 14.4 个 error_rate=1.0 的分钟）对 432s 份额 = 2 倍
    fast = evaluate_burn(14.4, 1440)
    assert fast is not None and fast.severity is Severity.P0
    assert fast.burn_ratio == 2.0 and fast.downtime_seconds == 864.0

    at_pace = evaluate_burn(7.2, 1440)  # 432s = 正好用完份额
    assert at_pace is not None and at_pace.severity is Severity.P1
    assert at_pace.burn_ratio == 1.0

    healthy = evaluate_burn(0.72, 1440)  # 43.2s，10% 份额
    assert healthy is not None and healthy.severity is None
    assert healthy.availability == 0.9995


def test_evaluate_burn_needs_enough_observation() -> None:
    """样本不足返回 None：部署首小时不能被判成"违反 SLO"，也不能被当成健康。"""
    assert evaluate_burn(7.2, SLO_MIN_OBSERVED_MINUTES - 1) is None
    assert evaluate_burn(0.0, 0) is None


def test_coverage_of_bounds() -> None:
    assert coverage_of(1440) == 1.0
    assert coverage_of(720) == 0.5
    assert coverage_of(0) == 0.0
    assert coverage_of(99999) == 1.0, "超量观测不溢出 100%"


# ----------------------------------------------------------------------
# 任务接线：真实库 + 告警播报
# ----------------------------------------------------------------------
async def test_task_emits_p0_when_burning_over_page_ratio(session_factory) -> None:
    """整 30 分钟的完全不可用（error_rate=1.0）→ 1800s 不可用 / 432s 份额 → P0。"""
    await _seed_minutes(session_factory, 1440, 0.021)  # 1440*0.021*60=1814s
    outlet = RecordingOutlet()
    result = await run_slo_burn_task(
        session_factory, Settings(env="test"), outlet, NOW, SloBurnState()
    )

    assert "burn:" in result
    assert len(outlet.emitted) == 1
    alert = outlet.emitted[0]
    assert alert.key == SLO_BURN_METRIC
    assert alert.severity is Severity.P0
    assert alert.state == "triggered"
    assert "99.5%" in alert.message and "燃尽" in alert.message, "值班文案要能读懂档位依据"


async def test_task_reports_only_once_per_tier_and_announces_recovery(
    session_factory,
) -> None:
    """同档持续违规不重复播报；回到预算内必须播 recovered；再次违规要重新播。"""
    await _seed_minutes(session_factory, 1440, 0.021)
    outlet, state = RecordingOutlet(), SloBurnState()

    await run_slo_burn_task(session_factory, Settings(env="test"), outlet, NOW, state)
    await run_slo_burn_task(session_factory, Settings(env="test"), outlet, NOW, state)
    assert len(outlet.emitted) == 1, "同档第二次不重复叫醒"

    # 数据变健康：播一次 recovered（级别沿用触发时的 P0）
    healthy_factory = session_factory
    await _seed_minutes(healthy_factory, 1440, 0.0, end=NOW + timedelta(hours=1))
    await run_slo_burn_task(
        healthy_factory, Settings(env="test"), outlet, NOW + timedelta(hours=1), state
    )
    assert len(outlet.emitted) == 2
    assert outlet.emitted[1].state == "recovered"
    assert outlet.emitted[1].severity is Severity.P0
    assert SLO_BURN_METRIC not in state.active


async def test_task_alerts_on_missing_telemetry_not_fake_health(session_factory) -> None:
    """库里只有 10 分钟数据：不判燃尽，但覆盖率 P1 必须响——无数据不等于健康。"""
    await _seed_minutes(session_factory, 10, 0.0)
    outlet = RecordingOutlet()
    result = await run_slo_burn_task(
        session_factory, Settings(env="test"), outlet, NOW, SloBurnState()
    )

    assert "burn:insufficient-data" in result
    assert len(outlet.emitted) == 1
    alert = outlet.emitted[0]
    assert alert.key == SLO_COVERAGE_METRIC
    assert alert.severity is Severity.P1
    assert "覆盖率" in alert.message


async def test_task_window_is_half_open(session_factory) -> None:
    """窗口左闭右开：边界外的分钟不计入，跨小时复检也不会重复计入同一分钟。"""
    await _seed_minutes(session_factory, 1440, 0.021, end=NOW)
    window_start = NOW - timedelta(days=1)
    async with session_factory() as session:
        repo = SQLAlchemyMonitoringTrendRepository(session)
        await repo.upsert_minute(window_start - timedelta(minutes=1), _sample(1.0))
        await repo.upsert_minute(NOW, _sample(1.0))  # 右开：这一分钟属于下一个窗口
        await session.commit()

    async with session_factory() as session:
        observed, error_sum = await SQLAlchemyMonitoringTrendRepository(
            session
        ).window_error_minutes(window_start, NOW)
    assert observed == 1440
    assert error_sum == pytest.approx(1440 * 0.021)

