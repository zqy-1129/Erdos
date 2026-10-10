"""告警 Webhook 外发测试（看板增强项 #10）。

覆盖：
- 未配置 URL：整条链路关闭（无 HTTP 调用、dispatch 空操作）；
- 投递成功 / 指数退避重试 / 耗尽失败（旁路语义：失败不外抛）；
- payload 形状（severity 对应 triggered/recovered，level 带 SLO 档位供值班分级）。
- sampling._run_once 接线：状态转换既投影看板事件又旁路外发。
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from app.core.config import Settings
from app.domain.alerts.severity import Severity
from app.domain.monitoring.ports import AlertTransition
from app.infra import sampling
from app.infra.alert_webhook import AlertWebhookDispatcher


def transition(state: str, metric: str = "p95_ms") -> AlertTransition:
    return AlertTransition(
        metric=metric,
        state=state,
        value=650.0,
        threshold=500.0,
        occurred_at=datetime(2026, 10, 3, 8, 0, 0, tzinfo=UTC),
    )


class RecordingSend:
    """可编程投递桩：记录调用，前 N 次失败。"""

    def __init__(self, failures: int = 0) -> None:
        self.calls: list[tuple[str, dict[str, Any], float]] = []
        self.failures = failures

    async def __call__(self, url: str, payload: dict[str, Any], timeout: float) -> bool:
        self.calls.append((url, payload, timeout))
        if self.failures > 0:
            self.failures -= 1
            raise OSError("connection refused")
        return True


def make_dispatcher(send: RecordingSend, url: str = "http://hook.example/alert") -> AlertWebhookDispatcher:
    return AlertWebhookDispatcher(
        url,
        timeout_seconds=3.0,
        retries=2,
        backoff_seconds=0.01,
        send_fn=send,
    )


class TestDispatcherBehavior:
    async def test_unconfigured_disabled_no_call(self) -> None:
        send = RecordingSend()
        dispatcher = make_dispatcher(send, url="   ")
        assert dispatcher.enabled is False
        dispatcher.dispatch(transition("triggered"))
        await asyncio_sleep0()
        assert send.calls == []

    async def test_payload_shape(self) -> None:
        send = RecordingSend()
        dispatcher = make_dispatcher(send)
        triggered = transition("triggered")
        assert await dispatcher.send(triggered) is True
        url, payload, timeout = send.calls[0]
        assert url == "http://hook.example/alert"
        assert timeout == 3.0
        assert payload["source"] == "erdos-server"
        assert payload["type"] == "monitor.alert"
        assert payload["severity"] == "warning"
        assert payload["state"] == "triggered"
        assert payload["metric"] == "p95_ms"
        assert payload["value"] == 650.0
        assert payload["threshold"] == 500.0
        assert payload["message"] == transition("triggered").message
        assert payload["occurred_at"] == "2026-10-03T08:00:00+00:00"
        assert payload["level"] is None, "未传档位时显式为 null，而不是缺键让接收端猜"

    async def test_payload_carries_slo_tier_for_oncall_routing(self) -> None:
        """三档分级必须走到外发最后一公里：只有 warning/info 时值班机器人无法按档呼叫。"""
        send = RecordingSend()
        dispatcher = make_dispatcher(send)
        assert await dispatcher.send(transition("triggered"), Severity.P0) is True
        assert send.calls[0][1]["level"] == "P0"
        assert send.calls[0][1]["severity"] == "warning", "旧字段口径保持兼容"

    async def test_recovered_severity_info(self) -> None:
        send = RecordingSend()
        dispatcher = make_dispatcher(send)
        await dispatcher.send(transition("recovered", metric="cpu_percent"))
        assert send.calls[0][1]["severity"] == "info"

    async def test_retry_then_success(self) -> None:
        send = RecordingSend(failures=2)
        dispatcher = make_dispatcher(send)
        assert await dispatcher.send(transition("triggered")) is True
        assert len(send.calls) == 3  # 2 次失败 + 1 次成功（retries=2）

    async def test_exhausted_returns_false_not_raise(self) -> None:
        # 旁路语义：耗尽重试返回 False（不外抛），监测主循环不受渠道故障影响
        send = RecordingSend(failures=99)
        dispatcher = make_dispatcher(send)
        assert await dispatcher.send(transition("triggered")) is False
        assert len(send.calls) == 3  # 1 + retries

    async def test_dispatch_fire_and_forget(self) -> None:
        send = RecordingSend()
        dispatcher = make_dispatcher(send)
        dispatcher.dispatch(transition("triggered"))
        await asyncio_sleep0()
        assert len(send.calls) == 1


async def asyncio_sleep0() -> None:
    import asyncio

    await asyncio.sleep(0)


class FakeCollector:
    def snapshot(self, window_seconds: float) -> Any:
        return SimpleNamespace(
            qps=1.0, error_rate=0.0, p50_ms=10.0, p95_ms=100.0,
            db_query_p95_ms=50.0, window_seconds=window_seconds, paths=(),
        )

    def system_metrics(self) -> tuple[float, float, float]:
        return (10.0, 20.0, 100.0)


class FakeEvaluator:
    def __init__(self, transitions: list[AlertTransition]) -> None:
        self._transitions = transitions
        self.active: set[str] = set()

    def evaluate(self, sample: Any) -> list[AlertTransition]:
        return self._transitions

    def active_view(self) -> dict[str, str]:
        return {}


class FakeBroker:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        self.published.append({**payload, "_topic": topic})


class TestSamplingIntegration:
    async def test_transition_dispatches_webhook_and_keeps_dashboard(
        self, monkeypatch, session_factory
    ) -> None:
        """采样告警经统一出口扇出：状态转换 -> 看板事件 + Webhook（含恢复通知）。

        出口与落库口径本身由 test_alert_outlet.py 覆盖；本用例只证明监测循环不再自带
        第二套告警外发实现。
        """
        from app.core.topics import EVENTS_TOPIC
        from app.infra.alert_outlet import BrokerAlertOutlet
        from app.infra.events import EventBroker

        async def fake_persist(**kwargs: Any) -> None:
            return None

        monkeypatch.setattr(sampling, "_persist", fake_persist)

        send = RecordingSend()
        dispatcher = make_dispatcher(send, url="http://hook.example/alert")
        real_broker = EventBroker()
        outlet = BrokerAlertOutlet(session_factory, real_broker, dispatcher)
        app = SimpleNamespace(
            state=SimpleNamespace(
                engine=SimpleNamespace(pool=None),  # _pool_usage 对无池信息按 0 处理
                session_factory=None,
                monitoring_alerts={},
                alert_outlet=outlet,
                event_broker=real_broker,
                settings=Settings(),
            )
        )
        result = await sampling._run_once(
            app=app,  # type: ignore[arg-type]
            collector=FakeCollector(),  # type: ignore[arg-type]
            broker=broker_topics(EVENTS_TOPIC),  # type: ignore[arg-type]
            evaluator=FakeEvaluator(  # type: ignore[arg-type]
                [transition("triggered"), transition("recovered")]
            ),
            settings=Settings(),
            last_minute=None,
        )
        await asyncio_sleep0()
        # 原有看板事件投影保持（2 条 monitor.alert 进事件总线）
        alert_events = await _alert_events(real_broker)
        assert len(alert_events) == 2
        # 旁路外发同样 2 条（payload 与事件口径一致）
        assert len(send.calls) == 2
        assert send.calls[0][1]["metric"] == "p95_ms"
        assert send.calls[1][1]["severity"] == "info"
        assert result is not None


def broker_topics(_topic: str) -> FakeBroker:
    """占位：监测循环内不再直接投影告警，broker 仅用于快照主题。"""
    return FakeBroker()


async def _alert_events(broker: object) -> list[dict]:
    import asyncio
    import contextlib

    from app.core.topics import EVENTS_TOPIC

    out: list[dict] = []
    generator = broker.subscribe({EVENTS_TOPIC}, since=0)  # type: ignore[attr-defined]
    try:
        while True:
            try:
                event = await asyncio.wait_for(generator.__anext__(), timeout=0.1)
            except (TimeoutError, StopAsyncIteration):
                break
            payload = dict(event.payload)
            if payload.get("type") == "monitor.alert":
                out.append(payload)
    finally:
        with contextlib.suppress(Exception):
            await generator.aclose()
    return out