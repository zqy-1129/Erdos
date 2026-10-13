"""告警分级与 SLO 燃尽测试（SP4-3）：分级 / 路由 / 去重 / 燃尽。"""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.config import Settings
from app.domain.alerts.severity import (
    SLO_AVAILABILITY,
    Alert,
    AlertRouter,
    Channel,
    Severity,
    SilenceManager,
    classify_reconcile_diff,
    severity_for_metric,
)
from app.domain.alerts.slo import (
    burn_ratio,
    elapsed_ratio,
    monthly_downtime_budget,
    remaining_budget,
)
from app.domain.monitoring.ports import AlertThresholds, MonitoringSample
from app.domain.monitoring.service import AlertEvaluator

SEVERITY_SOURCE = Path(__file__).resolve().parents[2] / "app" / "domain" / "alerts" / "severity.py"

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _sample(**overrides) -> MonitoringSample:
    base = dict(
        sampled_at=NOW,
        qps=100.0,
        error_rate=0.0,
        p50_ms=20.0,
        p95_ms=80.0,
        cpu_percent=10.0,
        memory_percent=30.0,
        rss_mb=64.0,
        db_query_p95_ms=30.0,
        db_pool_usage=0.1,
    )
    base.update(overrides)
    return MonitoringSample(**base)


def _evaluator() -> AlertEvaluator:
    """与 `infra/sampling.py` 完全同一套装配：阈值取自配置，目标取自文档承诺。"""
    settings = Settings(env="test")
    return AlertEvaluator(
        AlertThresholds.from_settings(settings),
        availability_target=SLO_AVAILABILITY,
        availability_min_requests=settings.monitoring_availability_min_requests,
    )


# ----------------------------------------------------------------------
# 告警分级
# ----------------------------------------------------------------------
def test_availability_p0_is_produced_by_the_live_path() -> None:
    """可用性跌破 99.5% → P0：这条产品承诺由采样链路给出，不另写一份分类函数。"""
    transitions = _evaluator().evaluate(_sample(error_rate=0.02))
    assert "availability" in {t.metric for t in transitions}
    assert severity_for_metric("availability") is Severity.P0


def test_error_rate_and_latency_p1_use_the_configured_thresholds() -> None:
    """错误率/延迟 → P1，且判据数字来自配置项（改配置值班就跟着改，不会各漂一半）。"""
    settings = Settings(env="test")
    transitions = _evaluator().evaluate(
        _sample(error_rate=0.0, p95_ms=settings.alert_p95_ms + 1)
    )
    fired = {t.metric for t in transitions}
    assert "p95_ms" in fired
    assert severity_for_metric("p95_ms") is Severity.P1
    assert severity_for_metric("error_rate") is Severity.P1


def test_low_traffic_availability_dip_does_not_page() -> None:
    """样本不足时不判可用性跌破（低流量窗口里几个 5xx 是统计噪声，不是 P0）。"""
    transitions = _evaluator().evaluate(_sample(error_rate=0.5, qps=0.2))
    assert "availability" not in {t.metric for t in transitions}


def test_severity_thresholds_have_a_single_source() -> None:
    """分级模块里不许再出现第二份阈值数字。

    `classify_availability` / `classify_error_or_latency` 曾把 99.5%、1%、500ms 又抄了一遍，
    而运行链路用的是 `SLO_AVAILABILITY` + `alert_error_rate`/`alert_p95_ms` 配置项：
    改配置而那份函数照旧，就是"两处各写一遍，改一处漏一处"。数字现在只有两个家——
    SLO 承诺常量与 Settings。

    判据走 AST 而不是文本：注释与 docstring 里写"500ms"是在陈述文档口径，合法；
    只有代码里的数值常量才算第二份阈值。
    """
    tree = ast.parse(SEVERITY_SOURCE.read_text(encoding="utf-8"))
    functions = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    for name in ("classify_availability", "classify_error_or_latency"):
        assert name not in functions, f"{name} 是阈值的第二份副本"

    numbers = [
        float(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ]
    assert numbers.count(0.995) == 1, "99.5% 只允许出现在 SLO_AVAILABILITY 一处"
    assert 0.01 not in numbers, "错误率阈值归 alert_error_rate 配置项"
    assert 500.0 not in numbers, "延迟阈值归 alert_p95_ms 配置项"


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
