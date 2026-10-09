"""W15（EN-STREAM）节流器与适配器增量回调测试。

覆盖（《任务执行手册》W15 验收的可离线部分）：
- 节流器：字符阈值即时发射 / flush 收尾不丢尾 / 间隔阈值（注入假时钟）/ 空缓冲 no-op；
- 适配器 on_delta：内容增量按序回调（流式路径），聚合结果一致。
首 token ≤500ms 的实测需真实端点，列 W15 现场验收（docs/acceptance/），不在离线用例内。
"""

import json

import httpx
import pytest

from engine.adapters.key_store import KeyStore
from engine.adapters.openai_compat import ChatMessage, ModelConfig, OpenAIChatAdapter
from engine.ipc.throttle import DeltaThrottler


def test_throttle_char_threshold_triggers_immediately() -> None:
    entries: list[str] = []
    t = DeltaThrottler(emit=entries.append, char_threshold=10, interval=999)
    t.push("a" * 6)
    assert entries == []  # 未达阈值
    t.push("b" * 4)
    assert entries == ["a" * 6 + "b" * 4]  # 达字符阈值立即发射（合并）


def test_throttle_flush_sends_tail() -> None:
    entries: list[str] = []
    t = DeltaThrottler(emit=entries.append, char_threshold=1000, interval=999)
    t.push("he")
    t.push("llo")
    assert entries == []
    t.flush()
    assert entries == ["hello"]
    t.flush()  # 空缓冲 no-op
    assert entries == ["hello"]


def test_throttle_interval_triggers() -> None:
    now = [100.0]
    entries: list[str] = []
    t = DeltaThrottler(
        emit=entries.append, interval=0.05, char_threshold=1000, clock=lambda: now[0]
    )
    t.push("x")
    assert entries == []  # 间隔未到
    now[0] += 0.06
    t.push("y")
    assert entries == ["xy"]


def test_throttle_empty_flush_noop() -> None:
    entries: list[str] = []
    t = DeltaThrottler(emit=entries.append)
    t.flush()
    assert entries == []


@pytest.mark.asyncio
async def test_adapter_on_delta_receives_content_in_order() -> None:
    """流式路径：内容增量按序逐段回调，聚合 content 与增量一致。"""
    chunks = [
        {"choices": [{"delta": {"content": "先想"}}]},
        {"choices": [{"delta": {"content": "再算"}}]},
        {"choices": [{"delta": {"content": "。"}, "finish_reason": "stop"}]},
    ]
    body = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    resp = httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    keys = KeyStore()
    keys.inject("sk-delta-test")
    adapter = OpenAIChatAdapter(
        ModelConfig("test", "https://api.test/v1", "m"),
        keys, transport=httpx.MockTransport(lambda r: resp),
    )
    seen: list[str] = []
    result = await adapter.chat(
        [ChatMessage(role="user", content="x")], stream=True, on_delta=seen.append
    )
    assert seen == ["先想", "再算", "。"]
    assert result.content == "先想再算。"
