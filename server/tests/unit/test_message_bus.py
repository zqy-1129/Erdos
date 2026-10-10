"""进程内消息总线：扇出与消费者异常的可观测性。

"单个消费者失败不阻断其它消费者"是对的，但**不能连日志和计数都没有**：`publish` 的
docstring 早就写了"记录日志即可"，实现里却一行日志都没有——真接上 MQ 消费者之后，
消费失败就只剩"某个订阅方不再更新"这种查不出原因的现象。

（现状说明：`MessageBus` 已在 `app.state` 装配，但生产链路的看板 SSE/监测事件走的是
`infra/events.py::EventBroker`，本总线尚无生产发布/订阅方；这条修复是为了它被接上时
不至于带着"看起来有兜底"的静默上线。）
"""

import pytest

from app.core.topics import EVENTS_TOPIC, MONITORING_TOPIC
from app.infra.message_bus import MessageBus


async def test_publish_fans_out_to_every_subscriber() -> None:
    bus = MessageBus()
    seen: list[str] = []

    async def first(message) -> None:  # noqa: ANN001
        seen.append(f"a:{message.payload['n']}")

    async def second(message) -> None:  # noqa: ANN001
        seen.append(f"b:{message.payload['n']}")

    await bus.subscribe(EVENTS_TOPIC, first)
    await bus.subscribe(EVENTS_TOPIC, second)
    await bus.publish(EVENTS_TOPIC, {"n": 1})

    assert seen == ["a:1", "b:1"]
    assert bus.delivery_failures == {}


async def test_consumer_failure_is_counted_logged_and_does_not_block_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = MessageBus()
    received: list[int] = []

    async def bad(message) -> None:  # noqa: ANN001
        raise RuntimeError("消费者内部炸了")

    async def good(message) -> None:  # noqa: ANN001
        received.append(message.payload["n"])

    await bus.subscribe(EVENTS_TOPIC, bad)
    await bus.subscribe(EVENTS_TOPIC, good)

    with caplog.at_level("ERROR", logger="erdos.bus"):
        await bus.publish(EVENTS_TOPIC, {"n": 1})
        await bus.publish(EVENTS_TOPIC, {"n": 2})

    assert received == [1, 2], "坏消费者不得阻断后面的消费者"
    assert bus.delivery_failures == {EVENTS_TOPIC: 2}, "失败必须按 topic 累计，可被看板/告警取用"
    assert "消费者异常" in caplog.text
    assert "RuntimeError" in caplog.text, "日志要带堆栈，否则等于没记"


async def test_failures_are_scoped_by_topic() -> None:
    """计数按 topic 分桶：一个主题炸了不该看起来像全局故障。"""
    bus = MessageBus()

    async def bad(message) -> None:  # noqa: ANN001
        raise ValueError("boom")

    await bus.subscribe(EVENTS_TOPIC, bad)
    await bus.publish(EVENTS_TOPIC, {})
    await bus.publish(MONITORING_TOPIC, {})  # 无订阅者，不算失败

    assert bus.delivery_failures == {EVENTS_TOPIC: 1}


async def test_delivery_failures_snapshot_is_a_copy() -> None:
    """对外暴露的是副本：调用方改不动内部账本。"""
    bus = MessageBus()

    async def bad(message) -> None:  # noqa: ANN001
        raise ValueError("boom")

    await bus.subscribe(EVENTS_TOPIC, bad)
    await bus.publish(EVENTS_TOPIC, {})
    snapshot = bus.delivery_failures
    snapshot[EVENTS_TOPIC] = 999

    assert bus.delivery_failures == {EVENTS_TOPIC: 1}
