"""告警评估器测试：状态转换（触发/恢复）与去重语义。"""

from datetime import UTC, datetime

from app.domain.monitoring.ports import AlertThresholds, MonitoringSample
from app.domain.monitoring.service import AlertEvaluator, MonitoringTrendService

THRESHOLDS = AlertThresholds(
    p95_ms=500.0,
    error_rate=0.05,
    qps=100.0,
    cpu_percent=85.0,
    memory_percent=85.0,
    db_query_p95_ms=200.0,
    db_pool_usage=0.8,
)
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _sample(**overrides) -> MonitoringSample:
    base = dict(
        sampled_at=NOW,
        qps=10.0,
        error_rate=0.01,
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


def test_trigger_then_sustain_then_recover() -> None:
    evaluator = AlertEvaluator(THRESHOLDS)
    assert evaluator.evaluate(_sample()) == []
    triggered = evaluator.evaluate(_sample(p95_ms=600.0))
    assert len(triggered) == 1
    assert triggered[0].metric == "p95_ms"
    assert triggered[0].state == "triggered"
    assert "P95 延迟" in triggered[0].message
    # 持续超阈值：不重复告警
    assert evaluator.evaluate(_sample(p95_ms=610.0)) == []
    assert evaluator.active == frozenset({"p95_ms"})
    # 回落：产生恢复事件
    recovered = evaluator.evaluate(_sample(p95_ms=100.0))
    assert len(recovered) == 1
    assert recovered[0].state == "recovered"
    assert evaluator.active == frozenset()


def test_multiple_metrics_state_transitions() -> None:
    evaluator = AlertEvaluator(THRESHOLDS)
    transitions = evaluator.evaluate(_sample(p95_ms=600.0, error_rate=0.1))
    assert {t.metric for t in transitions} == {"p95_ms", "error_rate"}
    # 只剩一个超阈值 -> 另一个恢复
    transitions = evaluator.evaluate(_sample(p95_ms=600.0, error_rate=0.01))
    assert len(transitions) == 1
    assert transitions[0].metric == "error_rate"
    assert transitions[0].state == "recovered"


def test_thresholds_from_settings(settings) -> None:
    settings.alert_p95_ms = 700.0
    thresholds = AlertThresholds.from_settings(settings)
    assert thresholds.p95_ms == 700.0
    assert thresholds.error_rate == settings.alert_error_rate


def test_trend_downsample_avg_and_factor() -> None:
    from app.domain.monitoring.ports import MonitoringPoint

    points = [
        MonitoringPoint(ts=NOW.replace(minute=m), value=float(v))
        for m, v in ((0, 2.0), (2, 4.0), (6, 8.0))
    ]
    service = MonitoringTrendService()
    series = service.build(
        points, "5m", NOW, NOW.replace(minute=9), agg="avg", factor=100.0
    )
    assert series.granularity == "5m"
    assert len(series.points) == 2
    assert series.points[0].value == 300.0  # (2+4)/2 * 100
    assert series.points[1].value == 800.0  # 8 * 100


def test_trend_downsample_max() -> None:
    from app.domain.monitoring.ports import MonitoringPoint

    points = [
        MonitoringPoint(ts=NOW.replace(minute=m), value=float(v))
        for m, v in ((0, 150.0), (3, 90.0), (7, 60.0))
    ]
    service = MonitoringTrendService()
    series = service.build(points, "5m", NOW, NOW.replace(minute=9), agg="max")
    assert [p.value for p in series.points] == [150.0, 60.0]