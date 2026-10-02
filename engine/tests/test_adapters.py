"""BYOK 适配器单元测试（SP1-5）：mock 联调 + 错误分类矩阵 + Key 安全。

验收标准对齐：
- 真实连通：用 httpx.MockTransport 模拟 OpenAI 兼容接口（无真实 Key 依赖）；
- 错误分类：401/网络/余额三类错误提示可读；
- Key 安全：日志零 Key 明文。
"""


import httpx
import pytest

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


async def test_chat_error_network_timeout() -> None:
    """网络错误：超时分类为 NETWORK。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout")

    adapter = _adapter(handler)
    with pytest.raises(AdapterError) as ei:
        await adapter.chat([ChatMessage("user", "hi")])
    assert ei.value.kind == ErrorKind.NETWORK


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


def test_usage_accumulator() -> None:
    """usage 累计：多次调用累计正确。"""
    from engine.adapters.openai_compat import Usage, UsageAccumulator

    acc = UsageAccumulator()
    acc.add(Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30, cost_cents=0.1))
    acc.add(Usage(prompt_tokens=5, completion_tokens=10, total_tokens=15, cost_cents=0.05))
    assert acc.total_prompt_tokens == 15
    assert acc.total_completion_tokens == 30
    assert acc.calls == 2
    assert abs(acc.total_cost_cents - 0.15) < 1e-6
