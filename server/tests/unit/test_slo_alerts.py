"""SLO 三档告警测试：可用性 P0 真的能触发，且恢复/再触发不会被静默窗口吞掉。

背景：三档分级（P0 可用性跌破 99.5% / P1 错误率>1% 或延迟超阈 / P2 容量与对账差异）此前
只有纯函数与单测，运行链路里既没有 P0 指标（可用性根本没人算），事件也只带扁平 warning/info。
"""

from datetime import UTC, datetime

from app.core.config import Settings
from app.core.topics import EVENTS_TOPIC
from app.domain.alerts.ports import BusinessAlert
from app.domain.alerts.severity import Severity, severity_for_metric
from app.domain.monitoring.ports import AlertThresholds, MonitoringSample
from app.domain.monitoring.service import AlertEvaluator
from app.infra.alert_outlet import BrokerAlertOutlet
from app.infra.events import EventBroker


def _sample(*, error_rate: float, qps: float, window: float = 60.0) -> MonitoringSample:
    return MonitoringSample(
        sampled_at=datetime(2026, 10, 8, 12, 0, tzinfo=UTC),
        qps=qps,
        error_rate=error_rate,
        p50_ms=100.0,
        p95_ms=200.0,
        cpu_percent=10.0,
        memory_percent=10.0,
        rss_mb=100.0,
        db_query_p95_ms=10.0,
        db_pool_usage=0.1,
        window_seconds=window,
    )


def _evaluator() -> AlertEvaluator:
    settings = Settings(env="test")
    return AlertEvaluator(
        AlertThresholds.from_settings(settings),
        availability_min_requests=settings.monitoring_availability_min_requests,
    )


def test_availability_breach_triggers_p0() -> None:
    """5xx 占比 2% ⇒ 可用性 98% < 99.5%：P0 触发，且可用性是唯一 P0。"""
    transitions = _evaluator().evaluate(_sample(error_rate=0.02, qps=10.0))
    by_metric = {t.metric: t for t in transitions}
    assert "availability" in by_metric, [t.metric for t in transitions]
    assert by_metric["availability"].state == "triggered"
    assert by_metric["availability"].value == 0.98
    assert by_metric["availability"].threshold == 0.995
    assert severity_for_metric("availability") is Severity.P0
    assert severity_for_metric("error_rate") is Severity.P1
    assert severity_for_metric("cpu_percent") is Severity.P2


def test_low_traffic_window_does_not_trigger_availability() -> None:
    """低流量窗口里几个 5xx 就跌破 99.5% 属统计噪声：可用性不误报，错误率规则照常。"""
    transitions = _evaluator().evaluate(_sample(error_rate=0.5, qps=0.5, window=60.0))
    metrics = {t.metric for t in transitions}
    assert "availability" not in metrics, "样本不足不得触发 P0"
    assert "error_rate" in metrics, "错误率规则不受最小样本门影响"


def test_availability_recovers_and_re_triggers() -> None:
    """状态机语义：跌破→触发、恢复→recovered、再跌破→再次触发（值班要看恢复）。"""
    evaluator = _evaluator()
    bad = _sample(error_rate=0.02, qps=10.0)
    good = _sample(error_rate=0.0, qps=10.0)

    assert {t.state for t in evaluator.evaluate(bad)} >= {"triggered"}
    recovered = {t.metric: t.state for t in evaluator.evaluate(good)}
    assert recovered.get("availability") == "recovered"
    again = {t.metric: t.state for t in evaluator.evaluate(bad)}
    assert again.get("availability") == "triggered"
    assert "跌破阈值" in evaluator.active_view()["availability"]


class RecordingWebhook:
    def __init__(self) -> None:
        self.transitions: list[object] = []

    def dispatch(self, transition, level=None) -> None:
        self.transitions.append(transition)


async def _events(broker: EventBroker) -> list[dict]:
    import asyncio
    import contextlib

    out: list[dict] = []
    generator = broker.subscribe({EVENTS_TOPIC}, since=0)
    try:
        while True:
            try:
                event = await asyncio.wait_for(generator.__anext__(), timeout=0.1)
            except (TimeoutError, StopAsyncIteration):
                break
            out.append(dict(event.payload))
    finally:
        with contextlib.suppress(Exception):
            await generator.aclose()
    return out


async def test_sampled_alerts_are_not_silenced(session_factory) -> None:
    """dedupe=False：恢复后立刻再触发必须再次外发（静默窗口会吞掉 P0 的第二次告警）。"""
    outlet = BrokerAlertOutlet(session_factory, EventBroker(), RecordingWebhook())
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    alert = BusinessAlert(
        key="availability", severity=Severity.P0, message="可用性跌破阈值", value=0.98, threshold=0.995
    )

    assert await outlet.emit(alert, now, dedupe=False) is True
    assert await outlet.emit(alert, now, dedupe=False) is True
    assert await outlet.emit(alert, now, dedupe=True) is True
    assert await outlet.emit(alert, now, dedupe=True) is False, "业务差异类仍按静默窗口去重"


async def test_recovery_is_published_as_info(session_factory) -> None:
    """恢复通知：事件按 info 着色但仍落库、仍走渠道（值班需要知道什么时候好的）。"""
    broker = EventBroker()
    webhook = RecordingWebhook()
    outlet = BrokerAlertOutlet(session_factory, broker, webhook)
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)

    await outlet.emit(
        BusinessAlert(
            key="error_rate",
            severity=Severity.P1,
            message="错误率回落阈值内",
            value=0.0,
            threshold=0.01,
            state="recovered",
        ),
        now,
    )

    events = await _events(broker)
    assert events[0]["severity"] == "info"
    assert events[0]["payload"]["state"] == "recovered"
    assert events[0]["payload"]["level"] == "P1"
