"""BYOK 适配器单元测试（SP1-5）：mock 联调 + 错误分类矩阵 + Key 安全。

验收标准对齐：
- 真实连通：用 httpx.MockTransport 模拟 OpenAI 兼容接口（无真实 Key 依赖）；
- 错误分类：401/网络/余额三类错误提示可读；
- Key 安全：日志零 Key 明文。
"""


import json

import httpx
import pytest

import engine.adapters.openai_compat as openai_compat
from engine.adapters.errors import (
    AdapterError,
    ErrorKind,
    classify_status,
    readable_message,
)
from engine.adapters.key_store import KeyStore
from engine.adapters.openai_compat import ChatMessage, ModelConfig, OpenAIChatAdapter


def _adapter(handler) -> OpenAIChatAdapter:
    keys = KeyStore()
    keys.inject("sk-test-secret")
    transport = httpx.MockTransport(handler)
    return OpenAIChatAdapter(ModelConfig("test", "https://api.test/v1", "test-model"), keys, transport=transport)


# ----------------------------------------------------------------------
# 错误分类
# ----------------------------------------------------------------------
def test_classify_status_matrix() -> None:
    """错误分类矩阵：401/429/402/5xx/其他。"""
    assert classify_status(401) == ErrorKind.AUTH
    assert classify_status(429) == ErrorKind.RATE_LIMIT
    assert classify_status(402) == ErrorKind.INSUFFICIENT_BALANCE
    assert classify_status(500) == ErrorKind.SERVER
    assert classify_status(404) == ErrorKind.UNKNOWN


def test_readable_messages_all_kinds() -> None:
    """三类关键错误文案可读。"""
    assert "Key" in readable_message(ErrorKind.AUTH)
    assert "限流" in readable_message(ErrorKind.RATE_LIMIT)
    assert "网络" in readable_message(ErrorKind.NETWORK)
    assert "余额" in readable_message(ErrorKind.INSUFFICIENT_BALANCE)


# ----------------------------------------------------------------------
# Key 安全
# ----------------------------------------------------------------------
def test_key_store_masked_never_leaks() -> None:
    """Key 脱敏：masked 不返回明文。"""
    store = KeyStore()
    store.inject("sk-secret-abcdef123456")
    masked = store.masked()
    assert "sk-secret-abcdef123456" not in masked
    assert "..." in masked  # 有打码
    assert store.auth_header() == "Bearer sk-secret-abcdef123456"  # 内部用完整 Key


def test_key_store_uninjected_raises() -> None:
    """未注入 Key 时 auth_header 抛错。"""
    store = KeyStore()
    with pytest.raises(ValueError):
        store.auth_header()


# ----------------------------------------------------------------------
# chat 调用（mock transport）
# ----------------------------------------------------------------------
def _chat_response(content: str, usage: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        },
    )


async def test_chat_non_stream_success() -> None:
    """非流式 chat 成功：解析内容 + usage 统计 + 带 Authorization 头。"""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-test-secret"
        return _chat_response("hello")

    adapter = _adapter(handler)
    result = await adapter.chat([ChatMessage("user", "hi")])
    assert result.content == "hello"
    assert result.streamed is False
    assert result.usage.total_tokens == 30
    assert adapter.usage_total.calls == 1


async def test_chat_error_401_raises_auth() -> None:
    """401 错误：抛 AdapterError(AUTH)，文案可读。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid key"})

    adapter = _adapter(handler)
    with pytest.raises(AdapterError) as ei:
        await adapter.chat([ChatMessage("user", "hi")])
    assert ei.value.kind == ErrorKind.AUTH
    assert "Key" in ei.value.message


async def test_chat_error_network_timeout(retry_sleep_recorder) -> None:
    """网络错误：超时分类为 NETWORK；EC-N1 初次+2 次指数退避重试全败。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout")

    adapter = _adapter(handler)
    with pytest.raises(AdapterError) as ei:
        await adapter.chat([ChatMessage("user", "hi")])
    assert ei.value.kind == ErrorKind.NETWORK
    assert retry_sleep_recorder == [0.5, 1.0]  # 指数退避（记录器替代真实等待）


async def test_chat_stream_success() -> None:
    """SSE 流式：逐 chunk 累积，[DONE] 结束。"""
    sse_body = (
        'data: {"choices":[{"delta":{"content":"你"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse_body, headers={"content-type": "text/event-stream"})

    adapter = _adapter(handler)
    result = await adapter.chat([ChatMessage("user", "hi")], stream=True)
    assert result.content == "你好"
    assert result.streamed is True


async def test_chat_stream_asks_vendor_for_usage() -> None:
    """W8 请求侧契约：流式请求带 stream_options.include_usage，末块 usage 优先于估算。

    不发该声明的厂商不会在末块回 usage，引擎只能按字符数估算——用量留痕与厂商账单
    长期对不上，且下游没有任何信号提示"这不是真实计量"。
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        body = (
            'data: {"choices":[{"delta":{"content":"你好"}}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":20,'
            '"total_tokens":30}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    adapter = _adapter(handler)
    result = await adapter.chat([ChatMessage("user", "hi")], stream=True)
    assert seen["stream_options"] == {"include_usage": True}
    assert result.usage.prompt_tokens == 10
    assert result.usage.estimated is False


async def test_chat_stream_without_vendor_usage_estimates() -> None:
    """厂商末块不给 usage：按已聚合文本估算并标 estimated=True（禁止报 0）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        body = 'data: {"choices":[{"delta":{"content":"一二三四五六七八九十"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    adapter = _adapter(handler)
    result = await adapter.chat([ChatMessage("user", "hi")], stream=True)
    assert result.usage.estimated is True
    assert result.usage.prompt_tokens > 0 and result.usage.completion_tokens > 0


def test_usage_accumulator() -> None:
    """usage 累计：只累计可核实的量（token 与调用次数）。"""
    from engine.adapters.openai_compat import Usage, UsageAccumulator

    acc = UsageAccumulator()
    acc.add(Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30))
    acc.add(Usage(prompt_tokens=5, completion_tokens=10, total_tokens=15))
    assert acc.total_prompt_tokens == 15
    assert acc.total_completion_tokens == 30
    assert acc.calls == 2


def test_engine_does_not_fabricate_costs() -> None:
    """G45：引擎没有任何单价来源，就不许给出成本数值。

    两层判据——只查属性会漏掉"换个字段名再塞回来"（`estimated_cost`/`cost_yuan`），
    只查源码又管不住从厂商响应里透传进来的字段：
    ① `Usage` / `UsageAccumulator` 里不得出现任何含 cost 的字段；
    ② 适配器源码里不得再出现"按每百万 token 折算"的乘法（原缺陷的形状）。
    要恢复引擎侧计价必须同时有：真实单价来源、整数分、未登记模型不给数值（F-002）。
    """
    import inspect
    import pathlib
    import re

    from engine.adapters import openai_compat as mod

    for cls in (mod.Usage, mod.UsageAccumulator):
        fields = getattr(cls, "__dataclass_fields__", {})
        assert not [name for name in fields if "cost" in name.lower()], (
            f"{cls.__name__} 又出现了成本字段：{list(fields)}"
        )

    source = pathlib.Path(inspect.getfile(mod)).read_text(encoding="utf-8")
    assert not re.search(r"\*\s*\d+\.\d+[^\n]*/\s*1_?000_?000", source), (
        "适配器里再次出现写死的每百万 token 单价折算"
    )


# ----------------------------------------------------------------------
# EC-N1/N2/N3 重试策略：分类过滤 + 指数退避 + Retry-After
# ----------------------------------------------------------------------


@pytest.fixture
def retry_sleep_recorder(monkeypatch):
    """替换退避等待为记录器（不真睡，捕获退避序列）。"""
    delays: list[float] = []

    async def _sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(openai_compat, "RETRY_SLEEP", _sleep)
    return delays


async def test_auth_error_no_retry(retry_sleep_recorder) -> None:
    """EC-N3：401 不盲重试——仅 1 次调用，零退避等待。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={"error": "invalid key"})

    adapter = _adapter(handler)
    with pytest.raises(AdapterError) as ei:
        await adapter.chat([ChatMessage("user", "hi")])
    assert ei.value.kind == ErrorKind.AUTH
    assert len(calls) == 1
    assert retry_sleep_recorder == []


async def test_network_error_retries_with_exponential_backoff(retry_sleep_recorder) -> None:
    """EC-N1：网络错误按指数退避重试（0.5s→1s），恢复后成功。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("connection reset")
        return _chat_response("ok")

    adapter = _adapter(handler)
    result = await adapter.chat([ChatMessage("user", "hi")])
    assert result.content == "ok"
    assert len(calls) == 2
    assert retry_sleep_recorder == [0.5]


async def test_rate_limit_honors_retry_after(retry_sleep_recorder) -> None:
    """EC-N2：429 遵守 Retry-After 头（替代指数退避）。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "slow down"}, headers={"Retry-After": "2"})
        return _chat_response("ok")

    adapter = _adapter(handler)
    result = await adapter.chat([ChatMessage("user", "hi")])
    assert result.content == "ok"
    assert len(calls) == 2
    assert retry_sleep_recorder == [2.0]


async def test_rate_limit_retry_after_over_cap_fails_fast(retry_sleep_recorder) -> None:
    """EC-N2：Retry-After 超阶段预算上限（30s）→ 不硬刷，立即失败转用户。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, json={"error": "slow down"}, headers={"Retry-After": "120"})

    adapter = _adapter(handler)
    with pytest.raises(AdapterError) as ei:
        await adapter.chat([ChatMessage("user", "hi")])
    assert ei.value.kind == ErrorKind.RATE_LIMIT
    assert len(calls) == 1
    assert retry_sleep_recorder == []


async def test_server_error_retries_exhausted(retry_sleep_recorder) -> None:
    """EC-N1：5xx 可重试，初次+2 次全败后抛 SERVER（退避 0.5s→1s）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    adapter = _adapter(handler)
    with pytest.raises(AdapterError) as ei:
        await adapter.chat([ChatMessage("user", "hi")])
    assert ei.value.kind == ErrorKind.SERVER
    assert retry_sleep_recorder == [0.5, 1.0]


async def test_stream_transport_error_retries(retry_sleep_recorder) -> None:
    """流式断流同样走指数退避重试，恢复后聚合成功。"""
    calls: list[int] = []
    sse_body = 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n' + "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadError("stream broken")
        return httpx.Response(200, content=sse_body, headers={"content-type": "text/event-stream"})

    adapter = _adapter(handler)
    result = await adapter.chat([ChatMessage("user", "hi")], stream=True)
    assert result.content == "ok"
    assert len(calls) == 2
    assert retry_sleep_recorder == [0.5]
