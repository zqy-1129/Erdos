"""业务告警出口测试（SR-C）：看板事件 + Webhook 外发 + 静默去重 + 未配置渠道不外发。

这一层存在的理由：三档 SLO 告警分级此前只有纯函数与单测，运行链路零调用方，
"差异必告警不可静默"实际靠人看接口响应。测试要证明的是**真的发出去了**，
而不是返回值里写了 alerted=True。
"""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.core.topics import EVENTS_TOPIC
from app.domain.alerts.ports import BusinessAlert
from app.domain.alerts.severity import Severity, SilenceManager
from app.domain.monitoring.ports import AlertTransition
from app.infra.alert_outlet import BrokerAlertOutlet
from app.infra.alert_webhook import AlertWebhookDispatcher
from app.infra.events import EventBroker
from app.repository.events import SQLAlchemyDashboardEventRepository
from app.repository.uow import UnitOfWork


class RecordingWebhook:
    """外发桩：记录状态转换与档位，不发网络。"""

    def __init__(self) -> None:
        self.transitions: list[AlertTransition] = []
        self.levels: list[object] = []

    def dispatch(self, transition: AlertTransition, level: object = None) -> None:
        self.transitions.append(transition)
        self.levels.append(level)


def _alert(value: float = 3.0, severity: Severity = Severity.P2) -> BusinessAlert:
    return BusinessAlert(
        key="reconcile_diff",
        severity=severity,
        message=f"积分对账差异 {int(value)} 个账户（余额与流水净额不一致）",
        value=value,
    )


async def _payloads(broker: EventBroker) -> list[dict]:
    """读已发布的 EVENTS_TOPIC 载荷：补发缓冲读到空就退出，不等实时事件。"""
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


async def test_emit_publishes_dashboard_event_and_webhook(session_factory) -> None:
    broker, webhook = EventBroker(), RecordingWebhook()
    outlet = BrokerAlertOutlet(session_factory, broker, webhook)
    now = datetime.now(UTC)

    assert await outlet.emit(_alert(), now) is True

    events = await _payloads(broker)
    assert len(events) == 1, events
    payload = events[0]["payload"]
    assert payload["metric"] == "reconcile_diff"
    assert payload["level"] == "P2"
    assert payload["channel"] == "message", "值班载荷要带目标通道，否则分级停在服务端内部"
    assert "3 个账户" in payload["message"], "值班文案要能直接读懂，不是裸指标名"
    assert events[0]["severity"] == "warning"
    # Webhook 载荷复用监测告警口径，且带业务短句而不是 "reconcile_diff 3.00 超过阈值"
    assert len(webhook.transitions) == 1
    assert webhook.levels == [Severity.P2], "档位必须走到外发最后一公里，接收端才能按页呼叫"
    assert "3 个账户" in webhook.transitions[0].message

    # 告警事件独立事务落库（值班可回查，不只在 SSE 里一闪而过）
    async with UnitOfWork(session_factory) as uow:
        items, total = await SQLAlchemyDashboardEventRepository(uow.session).query(
            types={"monitor.alert"}, start=None, end=None, limit=10, offset=0
        )
    assert total == 1, items
    assert items[0].payload["metric"] == "reconcile_diff"


async def test_alert_persist_failure_does_not_break_fanout(session_factory) -> None:
    """落库失败只记日志：告警旁路不能把业务事务带下水滚，也不能因此不发 SSE。"""
    broker, webhook = EventBroker(), RecordingWebhook()
    outlet = BrokerAlertOutlet(_broken_session_factory(), broker, webhook)

    assert await outlet.emit(_alert(), datetime.now(UTC)) is True
    assert len(await _payloads(broker)) == 1
    assert len(webhook.transitions) == 1


def _broken_session_factory():
    class _Broken:
        def __call__(self) -> object:
            raise RuntimeError("数据库不可用")

    return _Broken()


async def test_silence_window_dedupes_same_metric_and_level(session_factory) -> None:
    broker, webhook = EventBroker(), RecordingWebhook()
    outlet = BrokerAlertOutlet(
        session_factory, broker, webhook, SilenceManager(silence_window_seconds=300)
    )
    now = datetime.now(UTC)

    assert await outlet.emit(_alert(), now) is True
    assert await outlet.emit(_alert(), now + timedelta(seconds=120)) is False, "静默窗口内去重"
    assert await outlet.emit(_alert(), now + timedelta(seconds=360)) is True, "窗口过后重新外发"

    assert len(webhook.transitions) == 2
    assert len(await _payloads(broker)) == 2


async def test_p0_maps_to_critical_event_severity(session_factory) -> None:
    outlet = BrokerAlertOutlet(session_factory, (broker := EventBroker()), RecordingWebhook())

    await outlet.emit(_alert(value=1.0, severity=Severity.P0), datetime.now(UTC))

    events = await _payloads(broker)
    assert events[0]["severity"] == "critical"
    assert events[0]["payload"]["level"] == "P0"


async def test_each_tier_carries_its_oncall_channel(session_factory) -> None:
    """P0→飞书、P1→邮件、P2→消息：分级判定要出现在看板载荷里，值班才看得见档与通道。"""
    broker, webhook = EventBroker(), RecordingWebhook()
    outlet = BrokerAlertOutlet(session_factory, broker, webhook)
    now = datetime.now(UTC)

    tiers = [(Severity.P0, "feishu"), (Severity.P1, "email"), (Severity.P2, "message")]
    for index, (severity, _) in enumerate(tiers):
        await outlet.emit(
            BusinessAlert(key=f"tier_{index}", severity=severity, message="分级验收", value=1.0),
            now,
        )

    payloads = [event["payload"] for event in await _payloads(broker)]
    assert [(p["level"], p["channel"]) for p in payloads] == [
        ("P0", "feishu"),
        ("P1", "email"),
        ("P2", "message"),
    ]
    assert webhook.levels == [Severity.P0, Severity.P1, Severity.P2]


async def test_missing_webhook_config_still_publishes_event(session_factory) -> None:
    """未配 ERDOS_ALERT_WEBHOOK_URL：外发关闭但看板事件照发，不因缺凭据静默。"""
    broker = EventBroker()
    dispatcher = AlertWebhookDispatcher.from_settings(
        Settings(env="test", alert_webhook_url="")
    )
    outlet = BrokerAlertOutlet(session_factory, broker, dispatcher)

    assert await outlet.emit(_alert(), datetime.now(UTC)) is True
    assert len(await _payloads(broker)) == 1
    assert not dispatcher.enabled
