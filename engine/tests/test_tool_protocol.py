"""EN-TOOL（W8）工具调用协议测试：消息编码 / 非流式解析 / SSE 分片聚合 / usage 收口。

覆盖（《任务执行手册》W8 验收）：
- SSE fixture ≥6 例：arguments 跨 3+ 分片、name 迟到覆盖、缺 id 分桶丢弃（EC-T1）、
  内容与工具混合输出、无 usage 末块估算（estimated=True，禁报 0）、[DONE]/坏行容错；
- 非流式 tool_calls 解析 + finish_reason；
- payload 编码：tools/tool_choice 注入、tool 角色消息带 tool_call_id；
- 400+tool 报文 → TOOL_UNSUPPORTED 分类。
全部走 httpx.MockTransport，无真实模型调用。
"""

import json

import httpx
import pytest

from engine.adapters.errors import AdapterError, ErrorKind, classify_error_body
from engine.adapters.key_store import KeyStore
from engine.adapters.openai_compat import ChatMessage, ModelConfig, OpenAIChatAdapter, ToolCall


def _adapter(handler) -> OpenAIChatAdapter:
    keys = KeyStore()
    keys.inject("sk-test-secret")
    return OpenAIChatAdapter(
        ModelConfig("test", "https://api.test/v1", "test-model"),
        keys,
        transport=httpx.MockTransport(handler),
    )


def _sse(chunks: list[dict]) -> httpx.Response:
    """把增量列表编成 SSE 响应（data: 行 + [DONE]）。"""
    body = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "execute_code",
            "description": "在沙箱执行 Python 代码",
            "parameters": {"type": "object", "properties": {"code": {"type": "string"}}},
        },
    }
]


# ----------------------------------------------------------------------
# payload 编码
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_payload_includes_tools_and_tool_choice() -> None:
    """tools/tool_choice 注入请求体；tool 角色消息带 tool_call_id。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    messages = [
        ChatMessage(role="assistant", content=None, tool_calls=(
            ToolCall(id="call_1", name="execute_code", arguments_json='{"code":"print(1)"}'),
        )),
        ChatMessage(role="tool", content="slope=2.5", tool_call_id="call_1"),
    ]
    await _adapter(handler).chat(messages, tools=TOOLS, tool_choice="auto")
    payload = captured["payload"]
    assert payload["tools"] == TOOLS
    assert payload["tool_choice"] == "auto"
    assert payload["messages"][0]["tool_calls"][0]["function"]["name"] == "execute_code"
    assert payload["messages"][1]["role"] == "tool"
    assert payload["messages"][1]["tool_call_id"] == "call_1"


@pytest.mark.asyncio
async def test_payload_omits_tools_when_not_provided() -> None:
    """未传 tools 时 payload 不含 tools 键（向后兼容纯文本调用）。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    await _adapter(handler).chat([ChatMessage(role="user", content="hi")])
    assert "tools" not in captured["payload"]


# ----------------------------------------------------------------------
# 非流式解析
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_non_stream_tool_calls_parsed() -> None:
    """非流式响应的 message.tool_calls 解析为 ToolCall 列表 + finish_reason。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{
                    "message": {
                        "content": None,
                        "tool_calls": [{
                            "id": "call_9",
                            "type": "function",
                            "function": {"name": "execute_code", "arguments": '{"code":"1+1"}'},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    result = await _adapter(handler).chat([ChatMessage(role="user", content="solve")], tools=TOOLS)
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls is not None and len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "call_9"
    assert result.tool_calls[0].arguments_json == '{"code":"1+1"}'
    assert result.usage.estimated is False


# ----------------------------------------------------------------------
# SSE 分片聚合（EC-T1 边界矩阵）
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sse_arguments_split_across_chunks() -> None:
    """arguments 跨 3 分片按 index 顺序拼接；name 在首块。"""
    chunks: list[dict] = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_a", "function": {"name": "execute_code", "arguments": '{"co'}}
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": 'de": "pr'}}
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": 'int(1)"}'}}
        ]}, "finish_reason": "tool_calls"}]},
    ]

    result = await _adapter(lambda r: _sse(chunks)).chat(
        [ChatMessage(role="user", content="x")], stream=True, tools=TOOLS
    )
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls is not None and len(result.tool_calls) == 1
    assert result.tool_calls[0].arguments_json == '{"code": "print(1)"}'


@pytest.mark.asyncio
async def test_sse_name_arrives_late_overrides() -> None:
    """name 迟到（第二块才到）→ 覆盖空名。"""
    chunks: list[dict] = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_b"}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"name": "plot_figure", "arguments": "{}"}}
        ]}}]},
    ]
    result = await _adapter(lambda r: _sse(chunks)).chat(
        [ChatMessage(role="user", content="x")], stream=True, tools=TOOLS
    )
    assert result.tool_calls is not None and result.tool_calls[0].name == "plot_figure"


@pytest.mark.asyncio
async def test_sse_missing_id_bucket_dropped() -> None:
    """EC-T1：缺 id 的分桶丢弃（上层回注重述），不产生半残 ToolCall。"""
    chunks: list[dict] = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"name": "execute_code", "arguments": "{}"}}
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 1, "id": "call_ok", "function": {"name": "plot_figure", "arguments": "{}"}}
        ]}}]},
    ]
    result = await _adapter(lambda r: _sse(chunks)).chat(
        [ChatMessage(role="user", content="x")], stream=True, tools=TOOLS
    )
    assert result.tool_calls is not None and len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "call_ok"


@pytest.mark.asyncio
async def test_sse_mixed_content_and_tool_calls() -> None:
    """内容与工具调用混合输出：content 聚合不受影响。"""
    chunks: list[dict] = [
        {"choices": [{"delta": {"content": "先说明思路。"}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_c", "function": {"name": "execute_code", "arguments": "{}"}}
        ]}}]},
    ]
    result = await _adapter(lambda r: _sse(chunks)).chat(
        [ChatMessage(role="user", content="x")], stream=True, tools=TOOLS
    )
    assert result.content == "先说明思路。"
    assert result.tool_calls is not None and result.tool_calls[0].id == "call_c"


@pytest.mark.asyncio
async def test_sse_usage_missing_estimated_not_zero() -> None:
    """末块无 usage → 按文本估算且 estimated=True（禁报 0）。"""
    chunks: list[dict] = [{"choices": [{"delta": {"content": "hello world"}, "finish_reason": "stop"}]}]
    result = await _adapter(lambda r: _sse(chunks)).chat(
        [ChatMessage(role="user", content="x")], stream=True
    )
    assert result.usage.estimated is True
    assert result.usage.prompt_tokens > 0


@pytest.mark.asyncio
async def test_sse_usage_final_chunk_captured() -> None:
    """include_usage 末块（空 choices + usage）→ 真实 usage，estimated=False。"""
    chunks: list[dict] = [
        {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}},
    ]
    result = await _adapter(lambda r: _sse(chunks)).chat(
        [ChatMessage(role="user", content="x")], stream=True
    )
    assert result.usage.estimated is False
    assert result.usage.total_tokens == 10


@pytest.mark.asyncio
async def test_sse_bad_line_and_done_tolerated() -> None:
    """坏行/空行/[DONE] 容错（EC-N4：不崩、跳过）。"""
    body = (
        'data: {broken json\n\n'
        '\n'
        'data: {"choices": [{"delta": {"content": "ok"}}]}\n\n'
        'data: [DONE]\n\n'
    )
    resp = httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    result = await _adapter(lambda r: resp).chat([ChatMessage(role="user", content="x")], stream=True)
    assert result.content == "ok"


# ----------------------------------------------------------------------
# 错误分类（W8 扩展）
# ----------------------------------------------------------------------
def test_classify_error_body_tool_unsupported() -> None:
    """400 且报文含 tool → TOOL_UNSUPPORTED；其余走既有分类。"""
    assert classify_error_body(400, '{"error": "tools not supported"}') == ErrorKind.TOOL_UNSUPPORTED
    assert classify_error_body(400, '{"error": "bad request"}') == ErrorKind.UNKNOWN
    assert classify_error_body(401, "unauthorized") == ErrorKind.AUTH


@pytest.mark.asyncio
async def test_stream_400_tool_unsupported_raises() -> None:
    """流式 400+tool 报文 → AdapterError(TOOL_UNSUPPORTED) 可读文案。"""
    resp = httpx.Response(400, text='{"error": {"message": "tools is not supported"}}')
    with pytest.raises(AdapterError) as ei:
        await _adapter(lambda r: resp).chat(
            [ChatMessage(role="user", content="x")], stream=True, tools=TOOLS
        )
    assert ei.value.kind == ErrorKind.TOOL_UNSUPPORTED
