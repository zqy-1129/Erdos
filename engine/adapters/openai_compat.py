"""OpenAI 兼容多厂商适配器（SP1-5 纯文本 chat + W8 工具调用协议）。

红线（SP1-5 提示词）：
- OpenAI 兼容协议（/chat/completions）；
- 超时：连接 10s / 读 60s；
- 错误分类（401/429/网络/余额）返回可读文案；
- 出站仅厂商域名（base_url 由配置固定，不向非厂商域名请求）；
- Key 不出现在日志/事件中（仅 auth 头，脱敏）。

W8（EN-TOOL）扩展：
- ChatMessage 携带 tool_calls（assistant 发起）与 tool_call_id（tool 角色回注）；
- chat() 可传 tools/tool_choice，payload 按 OpenAI function calling 规范编码；
- SSE 增量聚合：delta.tool_calls 按 index 分桶，arguments 分片顺序拼接、name 迟到
  覆盖、finish_reason=="tool_calls" 收口；缺 id 的分桶丢弃（EC-T1，交上层回注重述）；
- usage 收口：流式请求带 stream_options.include_usage，末块无 usage 时按已聚合文本
  估算并置 estimated=True（禁止报 0）。
"""

import json
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from engine.adapters.errors import (
    AdapterError,
    ErrorKind,
    classify_error_body,
    readable_message,
)
from engine.adapters.key_store import KeyStore

CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 60.0
MAX_RETRIES = 2  # 断流重试次数


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """厂商/BaseURL/模型名组合配置。"""

    provider: str  # deepseek / openai / kimi / claude
    base_url: str  # 如 https://api.deepseek.com/v1
    model: str  # 如 deepseek-chat


@dataclass(frozen=True, slots=True)
class ToolCall:
    """一次工具调用请求（assistant 消息携带，由 Tool Registry 分发）。"""

    id: str
    name: str
    arguments_json: str  # 原样保存（分片拼接结果），由 Registry 校验解析


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """一条对话消息（W8：扩展工具调用字段）。"""

    role: str  # system / user / assistant / tool
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] | None = None  # assistant 发起工具调用时
    tool_call_id: str | None = None  # role="tool" 回注执行结果时必带


@dataclass(slots=True)
class Usage:
    """单次调用用量与成本。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_cents: float = 0.0  # 预计成本（分）
    estimated: bool = False  # W8：流式末块无 usage 时按文本估算（禁止报 0）


@dataclass(slots=True)
class ChatResult:
    """一次 chat 调用结果。"""

    content: str
    usage: Usage
    streamed: bool = False
    tool_calls: list[ToolCall] | None = None  # 非空表示模型请求执行工具
    finish_reason: str | None = None  # stop / tool_calls / length / ...


@dataclass(slots=True)
class UsageAccumulator:
    """跨调用用量累计（落 trail，SP1-6 接口）。"""

    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cost_cents: float = 0.0
    calls: int = 0

    def add(self, usage: Usage) -> None:
        self.total_prompt_tokens += usage.prompt_tokens
        self.total_completion_tokens += usage.completion_tokens
        self.total_cost_cents += usage.cost_cents
        self.calls += 1


class OpenAIChatAdapter:
    """OpenAI 兼容 chat 客户端（多厂商统一协议）。"""

    def __init__(self, config: ModelConfig, keys: KeyStore, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._config = config
        self._keys = keys
        self._transport = transport  # 测试注入 mock transport
        self._usage_accum = UsageAccumulator()

    @property
    def usage_total(self) -> UsageAccumulator:
        return self._usage_accum

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT),
        )

    async def chat(
        self,
        messages: list[ChatMessage],
        stream: bool = False,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> ChatResult:
        """非流式/流式 chat 调用，带错误分类与断流重试；tools 非空时启用工具协议。

        W15：stream=True 且提供 on_delta 时，每个内容增量经回调实时上报（token 级
        流式；允许丢帧，断流重试可能造成增量重复——UI 以最终聚合 content 为准）。
        """
        payload: dict = {
            "model": self._config.model,
            "messages": [_encode_message(m) for m in messages],
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
        if stream:
            return await self._chat_stream(payload, on_delta=on_delta)
        return await self._chat_once(payload)

    async def _chat_once(self, payload: dict) -> ChatResult:
        """非流式单次调用（带重试）。"""
        last_error: AdapterError | None = None
        for _ in range(MAX_RETRIES + 1):
            try:
                async with self._client() as client:
                    resp = await client.post(
                        f"{self._config.base_url}/chat/completions",
                        headers={"Authorization": self._keys.auth_header(), "Content-Type": "application/json"},
                        json=payload,
                    )
                if resp.status_code != 200:
                    kind = classify_error_body(resp.status_code, resp.text)
                    raise AdapterError(kind, readable_message(kind))
                data = resp.json()
                choice = data["choices"][0]
                message = choice.get("message", {})
                content = message.get("content") or ""
                usage = _parse_usage(data.get("usage"))
                self._usage_accum.add(usage)
                return ChatResult(
                    content=content,
                    usage=usage,
                    streamed=False,
                    tool_calls=[_parse_tool_call(tc) for tc in message.get("tool_calls") or []],
                    finish_reason=choice.get("finish_reason"),
                )
            except httpx.TimeoutException:
                last_error = AdapterError(ErrorKind.NETWORK, readable_message(ErrorKind.NETWORK))
            except httpx.TransportError:
                last_error = AdapterError(ErrorKind.NETWORK, readable_message(ErrorKind.NETWORK))
            except AdapterError as exc:
                last_error = exc
        raise last_error or AdapterError(ErrorKind.UNKNOWN, readable_message(ErrorKind.UNKNOWN))

    async def _chat_stream(self, payload: dict, on_delta: Callable[[str], None] | None = None) -> ChatResult:
        """SSE 流式调用（断流重试 2 次）；W8：delta.tool_calls 分桶聚合 + usage 收口。"""
        last_error: AdapterError | None = None
        for _ in range(MAX_RETRIES + 1):
            try:
                parts: list[str] = []
                buckets: dict[int, dict] = {}  # index → {id, name, args: [分片]}
                finish_reason: str | None = None
                usage: Usage | None = None
                async with self._client() as client, client.stream(
                    "POST",
                    f"{self._config.base_url}/chat/completions",
                    headers={"Authorization": self._keys.auth_header(), "Content-Type": "application/json"},
                    json=payload,
                ) as resp:
                    if resp.status_code != 200:
                        await resp.aread()
                        kind = classify_error_body(resp.status_code, resp.text)
                        raise AdapterError(kind, readable_message(kind))
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("usage"):
                            usage = _parse_usage(chunk["usage"])  # include_usage 末块
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                        delta = choice.get("delta", {})
                        if delta.get("content"):
                            parts.append(delta["content"])
                            if on_delta is not None:
                                on_delta(delta["content"])  # W15：token 级增量上报
                        for tc in delta.get("tool_calls") or []:
                            index = int(tc.get("index", 0))
                            bucket = buckets.setdefault(index, {"id": "", "name": "", "args": []})
                            if tc.get("id"):
                                bucket["id"] = tc["id"]
                            function = tc.get("function") or {}
                            if function.get("name"):
                                bucket["name"] = function["name"]  # name 迟到 → 覆盖
                            if function.get("arguments"):
                                bucket["args"].append(function["arguments"])
                content = "".join(parts)
                # EC-T1：缺 id 的分桶丢弃（交上层回注模型重述），其余按 index 序还原
                tool_calls = [
                    ToolCall(id=b["id"], name=b["name"], arguments_json="".join(b["args"]))
                    for _, b in sorted(buckets.items())
                    if b["id"]
                ]
                if usage is None or (usage.prompt_tokens == 0 and usage.completion_tokens == 0):
                    # 末块无 usage：按已聚合文本估算（禁止报 0）
                    usage = Usage(
                        prompt_tokens=max(1, len(content) // 3),
                        completion_tokens=max(1, len(content) // 3),
                        total_tokens=max(1, len(content) // 3) * 2,
                        estimated=True,
                    )
                self._usage_accum.add(usage)
                return ChatResult(
                    content=content,
                    usage=usage,
                    streamed=True,
                    tool_calls=tool_calls,
                    finish_reason=finish_reason,
                )
            except httpx.TimeoutException:
                last_error = AdapterError(ErrorKind.NETWORK, readable_message(ErrorKind.NETWORK))
            except httpx.TransportError:
                last_error = AdapterError(ErrorKind.NETWORK, readable_message(ErrorKind.NETWORK))
            except AdapterError as exc:
                last_error = exc
        raise last_error or AdapterError(ErrorKind.UNKNOWN, readable_message(ErrorKind.UNKNOWN))


def _encode_message(m: ChatMessage) -> dict:
    """ChatMessage → OpenAI 协议消息体（W8：tool_calls / tool 角色回注）。"""
    encoded: dict = {"role": m.role}
    if m.content is not None:
        encoded["content"] = m.content
    if m.tool_calls:
        encoded["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments_json},
            }
            for tc in m.tool_calls
        ]
    if m.tool_call_id is not None:
        encoded["tool_call_id"] = m.tool_call_id
    return encoded


def _parse_tool_call(raw: dict) -> ToolCall:
    """非流式响应中的 tool_call → ToolCall（缺字段按空串容错，由 Registry 兜底）。"""
    function = raw.get("function") or {}
    return ToolCall(
        id=str(raw.get("id", "")),
        name=str(function.get("name", "")),
        arguments_json=str(function.get("arguments", "")),
    )


def _parse_usage(raw: dict | None) -> Usage:
    """解析 usage（含成本估算，按 tokens 粗略计价）。"""
    if not raw:
        return Usage()
    prompt = int(raw.get("prompt_tokens", 0))
    completion = int(raw.get("completion_tokens", 0))
    total = int(raw.get("total_tokens", prompt + completion))
    # 预计成本：按通用定价粗略估算（元/百万 token），可配置化
    cost_cents = round((prompt * 2.0 + completion * 8.0) / 1_000_000 * 100, 4)
    return Usage(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=total, cost_cents=cost_cents
    )
