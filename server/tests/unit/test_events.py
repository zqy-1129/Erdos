"""版本化事件总线单元测试（看板 FR-5 实时推送核心）。"""

import asyncio
import contextlib

import pytest

from app.infra.events import EventBroker


async def test_publish_versions_monotonic() -> None:
    broker = EventBroker()
    e1 = await broker.publish("t", {"a": 1})
    e2 = await broker.publish("t", {"a": 2})
    assert e1.version == 1 and e2.version == 2
    assert broker.latest_version == 2


async def test_subscribe_receives_backlog_then_live() -> None:
    broker = EventBroker()
    await broker.publish("t", {"n": 1})
    await broker.publish("t", {"n": 2})

    received: list[int] = []
    async for e in broker.subscribe({"t"}, since=1):
        received.append(e.version)
        if len(received) == 1:
            # 收到积压后发布实时事件，验证积压->实时无缝衔接
            await broker.publish("t", {"n": 3})
        if len(received) == 2:
            break

    assert received == [2, 3], "先补缓冲积压（>since），再实时接收"


async def test_subscribe_topic_filter() -> None:
    broker = EventBroker()
    await broker.publish("other", {"n": 1})
    await broker.publish("t", {"n": 2})

    received = []
    async for e in broker.subscribe({"t"}, since=0):
        received.append(e.payload["n"])
        if len(received) == 1:
            break
    assert received == [2]


async def test_subscribe_unsubscribes_on_close() -> None:
    broker = EventBroker()
    generator = broker.subscribe({"t"}, since=0)
    received: list[int] = []

    async def consume() -> None:
        async for e in generator:
            received.append(e.version)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)  # 让订阅任务完成注册
    assert len(broker._subscribers) == 1

    await broker.publish("t", {"n": 1})
    await asyncio.sleep(0.01)
    assert received == [1]

    # 消费任务尚在 await queue.get() 内挂起：先取消任务，
    # 让生成器在取消时执行 finally 自动退订（不可在运行中直接 aclose）
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert len(broker._subscribers) == 0, "生成器关闭后自动退订"


async def test_buffer_overflow_drops_oldest() -> None:
    broker = EventBroker(buffer_size=3)
    for i in range(5):
        await broker.publish("t", {"n": i})
    backlog = broker.versions_after(0)
    assert [e.version for e in backlog] == [3, 4, 5], "溢出后保留最新 buffer_size 条"


async def test_slow_consumer_dropped_oldest_not_blocking() -> None:
    broker = EventBroker()
    queue = asyncio.Queue(maxsize=2)
    broker._subscribers.add(queue)
    for i in range(5):
        await broker.publish("t", {"n": i})
    assert queue.qsize() == 2, "慢消费者队列满时丢弃最旧，发布不阻塞"


def test_invalid_buffer_size_rejected() -> None:
    with pytest.raises(ValueError):
        EventBroker(buffer_size=0)