"""OpenAI 兼容多厂商适配器（SP1-5）：chat/completions 非流式 + SSE 流式。

红线（SP1-5 提示词）：
- OpenAI 兼容协议（/chat/completions）；
- 超时：连接 10s / 读 60s；
- 错误分类（401/429/网络/余额）返回可读文案；
- 出站仅厂商域名（base_url 由配置固定，不向非厂商域名请求）；
- Key 不出现在日志/事件中（仅 auth 头，脱敏）。
"""

import json
from dataclasses import dataclass

import httpx

from engine.adapters.errors import (
    AdapterError,
    ErrorKind,
    classify_status,
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
class ChatMessage:
    """一条对话消息。"""

    role: str  # system / user / assistant
    content: str


@dataclass(slots=True)
class Usage:
    """单次调用用量与成本。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_cents: float = 0.0  # 预计成本（分）


@dataclass(slots=True)
class ChatResult:
    """一次 chat 调用结果。"""

    content: str
    usage: Usage
    streamed: bool = False


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
        self, messages: list[ChatMessage], stream: bool = False
    ) -> ChatResult:
        """非流式/流式 chat 调用，带错误分类与断流重试。"""
        payload = {
            "model": self._config.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": stream,
        }
        if stream:
            return await self._chat_stream(payload)
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
                    kind = classify_status(resp.status_code)
                    raise AdapterError(kind, readable_message(kind))
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                usage = _parse_usage(data.get("usage"))
                self._usage_accum.add(usage)
                return ChatResult(content=content, usage=usage, streamed=False)
            except httpx.TimeoutException:
                last_error = AdapterError(ErrorKind.NETWORK, readable_message(ErrorKind.NETWORK))
            except httpx.TransportError:
                last_error = AdapterError(ErrorKind.NETWORK, readable_message(ErrorKind.NETWORK))
            except AdapterError as exc:
                last_error = exc
        raise last_error or AdapterError(ErrorKind.UNKNOWN, readable_message(ErrorKind.UNKNOWN))

    async def _chat_stream(self, payload: dict) -> ChatResult:
        """SSE 流式调用（断流重试 2 次）。"""
        last_error: AdapterError | None = None
        for _ in range(MAX_RETRIES + 1):
            try:
                parts: list[str] = []
                async with self._client() as client, client.stream(
                    "POST",
                    f"{self._config.base_url}/chat/completions",
                    headers={"Authorization": self._keys.auth_header(), "Content-Type": "application/json"},
                    json=payload,
                ) as resp:
                    if resp.status_code != 200:
                        await resp.aread()
                        kind = classify_status(resp.status_code)
                        raise AdapterError(kind, readable_message(kind))
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            delta = chunk["choices"][0].get("delta", {}).get("content", "")
                            if delta:
                                parts.append(delta)
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue
                content = "".join(parts)
                usage = Usage()
                self._usage_accum.add(usage)
                return ChatResult(content=content, usage=usage, streamed=True)
            except httpx.TimeoutException:
                last_error = AdapterError(ErrorKind.NETWORK, readable_message(ErrorKind.NETWORK))
            except httpx.TransportError:
                last_error = AdapterError(ErrorKind.NETWORK, readable_message(ErrorKind.NETWORK))
            except AdapterError as exc:
                last_error = exc
        raise last_error or AdapterError(ErrorKind.UNKNOWN, readable_message(ErrorKind.UNKNOWN))


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
