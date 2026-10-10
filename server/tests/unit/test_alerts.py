"""告警分级与 SLO 燃尽测试（SP4-3）：分级 / 路由 / 去重 / 燃尽。"""

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.alerts.severity import (
    Alert,
    AlertRouter,
    Channel,
    Severity,
    SilenceManager,
    classify_availability,
    classify_error_or_latency,
    classify_reconcile_diff,
)
from app.domain.alerts.slo import (
    burn_ratio,
    elapsed_ratio,
    monthly_downtime_budget,
    remaining_budget,
)


# ----------------------------------------------------------------------
# 告警分级
# ----------------------------------------------------------------------
def test_classify_availability_p0() -> None:
    """可用性 < 99.5% → P0。"""
    assert classify_availability(0.99) == Severity.P0
    assert classify_availability(0.998) is None  # 达标无告警


def test_classify_error_or_latency_p1() -> None:
    """错误率 > 1% 或 P99 > 500ms → P1。"""
    assert classify_error_or_latency(0.02, 100) == Severity.P1  # 错误率超
    assert classify_error_or_latency(0.005, 600) == Severity.P1  # P99 超
    assert classify_error_or_latency(0.005, 100) is None  # 达标无告警


def test_classify_reconcile_diff_p2() -> None:
    """对账差异非零 → P2。"""
    assert classify_reconcile_diff(1) == Severity.P2
    assert classify_reconcile_diff(0) is None


# ----------------------------------------------------------------------
# 值班路由 + 静默去重
# ----------------------------------------------------------------------
def test_route_by_severity() -> None:
    """按级别路由到对应通道。"""
    router = AlertRouter()
    now = datetime.now(UTC)
    assert router.route(Alert(Severity.P0, "uptime", "宕机", now), now) == Channel.FEISHU
    assert router.route(Alert(Severity.P1, "error_rate", "错误率高", now), now) == Channel.EMAIL
    assert router.route(Alert(Severity.P2, "reconcile", "对账差异", now), now) == Channel.MESSAGE


def test_silence_dedup_same_key() -> None:
    """静默去重：同级别同指标在窗口内只发一次。"""
    router = AlertRouter(SilenceManager(silence_window_seconds=300))
    now = datetime.now(UTC)
    alert = Alert(Severity.P0, "uptime", "宕机", now)
    assert router.route(alert, now) == Channel.FEISHU  # 第一次发送
    # 30 秒后重复（窗口内）：去重
    assert router.route(alert, now + timedelta(seconds=30)) is None
    # 310 秒后（窗口外）：重新发送
    assert router.route(alert, now + timedelta(seconds=310)) == Channel.FEISHU


def test_silence_different_metric_not_deduped() -> None:
    """不同指标不去重。"""
    router = AlertRouter(SilenceManager(silence_window_seconds=300))
    now = datetime.now(UTC)
    router.route(Alert(Severity.P0, "uptime", "宕机", now), now)
    assert router.route(Alert(Severity.P1, "error_rate", "错误率高", now), now) == Channel.EMAIL


def test_router_accounting_is_bounded() -> None:
    """路由账本是有界计数，不是历史列表：AlertRouter 活在 7×24 的长驻进程里。"""
    router = AlertRouter(SilenceManager(silence_window_seconds=0))
    now = datetime.now(UTC)
    for index in range(200):
        router.route(Alert(Severity.P0, f"metric-{index}", "宕机", now), now)
    assert router.routed_counts == {
        Channel.FEISHU: 200,
        Channel.EMAIL: 0,
        Channel.MESSAGE: 0,
    }


def test_channel_for_does_not_consume_silence() -> None:
    """纯映射不去重：恢复通知与自带状态机的调用方按级别直接取通道。"""
    router = AlertRouter(SilenceManager(silence_window_seconds=300))
    now = datetime.now(UTC)
    alert = Alert(Severity.P0, "uptime", "宕机", now)
    assert router.route(alert, now) == Channel.FEISHU
    assert router.route(alert, now) is None, "窗口内已去重"
    assert AlertRouter.channel_for(Severity.P0) == Channel.FEISHU


# ----------------------------------------------------------------------
# SLO 燃尽
# ----------------------------------------------------------------------
def test_monthly_downtime_budget() -> None:
    """月度宕机预算：30 天 * 0.5% = 12960 秒（3.6 小时）。"""
    budget = monthly_downtime_budget(0.995, 30)
    assert budget == pytest.approx(30 * 24 * 3600 * 0.005)


def test_burn_ratio_and_remaining() -> None:
    """燃尽比例与剩余预算。"""
    budget = monthly_downtime_budget(0.995, 30)
    half = budget / 2
    assert burn_ratio(half) == 0.5
    assert remaining_budget(half) == budget - half


def test_burn_ratio_over_budget() -> None:
    """超预算：燃尽比例 > 1，剩余为 0。"""
    budget = monthly_downtime_budget(0.995, 30)
    assert burn_ratio(budget * 2) == 2.0
    assert remaining_budget(budget * 2) == 0.0


def test_elapsed_ratio() -> None:
    """本月已过时间占比。"""
    month_start = datetime(2026, 10, 1, tzinfo=UTC)
    half_month = month_start + timedelta(days=15)
    assert elapsed_ratio(half_month, month_start) == 0.5
