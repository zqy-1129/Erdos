"""SSE 事件流游标空窗协商测试（看板收尾项：跨服务重启空窗）。"""

import json

import pytest

from app.api.v1.dashboard import _event_stream, _parse_since
from app.infra.events import EventBroker


async def _collect(generator, limit: int) -> list[tuple[str, str, dict]]:
    """消费事件流生成器，返回 [(id, event, payload), ...]。"""
    out: list[tuple[str, str, dict]] = []
    async for chunk in generator:
        ident = event = ""
        data = {}
        for line in chunk.splitlines():
            if line.startswith("id: "):
                ident = line[4:]
            elif line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        out.append((ident, event, data))
        if len(out) >= limit:
            break
    return out


async def test_stream_emits_reset_on_version_rewind() -> None:
    """版本回卷（服务重启）：游标大于最新版本时先下发 stream.reset。"""
    broker = EventBroker()
    await broker.publish("presence.online_count", {"current_online": 1})
    # 旧客户端游标 42 远大于重启后的最新版本 1
    events = await _collect(_event_stream(broker, since=42), limit=1)
    assert events[0][1] == "stream.reset", "游标失效应先下发复位事件"
    assert events[0][2]["reason"] == "cursor_stale"
    assert events[0][2]["since"] == 42


async def test_stream_emits_reset_on_buffer_overflow() -> None:
    """积压溢出：游标早于缓冲最早版本时下发 stream.reset。"""
    broker = EventBroker(buffer_size=3)
    for i in range(5):
        await broker.publish("presence.online_count", {"current_online": i})
    # 缓冲只保留版本 3/4/5，游标 1 已溢出
    events = await _collect(_event_stream(broker, since=1), limit=1)
    assert events[0][1] == "stream.reset"
    assert events[0][2]["reason"] == "cursor_stale"


async def test_stream_no_reset_for_valid_cursor() -> None:
    """有效游标：直接补发积压，不产生 stream.reset。"""
    broker = EventBroker()
    await broker.publish("presence.online_count", {"current_online": 1})
    await broker.publish("presence.online_count", {"current_online": 2})
    events = await _collect(_event_stream(broker, since=1), limit=1)
    assert events[0][1] == "presence.online_count", "有效游标直接补发积压，不复位"


def test_parse_since_priority() -> None:
    """游标解析：Last-Event-ID 优先于 ?since=，非法值回退 0。"""

    class _H:
        def __init__(self, h, q):
            self.headers = h
            self.query_params = q

    assert _parse_since(_H({"Last-Event-ID": "7"}, {"since": "3"})) == 7  # 头优先
    assert _parse_since(_H({}, {"since": "3"})) == 3
    assert _parse_since(_H({"Last-Event-ID": "abc"}, {})) == 0  # 非法回退
    assert _parse_since(_H({}, {})) == 0


@pytest.mark.parametrize(
    "buffer_size,publishes,since,expect_reset",
    [
        (5, 3, 0, False),   # 从头订阅
        (5, 3, 3, False),   # 恰好最新
        (5, 3, 4, True),    # 版本回卷
        (2, 5, 3, True),    # 积压溢出（缓冲 4/5，游标 3 早于最早）
    ],
)
async def test_reset_needed_matrix(buffer_size, publishes, since, expect_reset) -> None:
    broker = EventBroker(buffer_size=buffer_size)
    for _ in range(publishes):
        await broker.publish("t", {"n": 1})
    assert broker.reset_needed(since) is expect_reset
