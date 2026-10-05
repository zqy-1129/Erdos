"""厂商能力探测与缓存（EN-CAP W7）。

设计（《引擎开发详细方案》§4）：
- ProviderCapabilities：能力矩阵（tools/structured_output/vision/models 端点等）；
- 运行时探测仅做 `GET {base_url}/models`（2s 超时）——任何失败（404/401/超时）都只记
  models_endpoint=False，**不得判定 Key 无效**（开发文档 §8.2 红线）；
- tools 等高阶能力的判定来源：离线 fixture 矩阵（工程默认，待 W14 provider.test 实测
  校准）+ 运行时缓存；带工具冒烟会产生模型费用，不在后台探测中执行；
- 缓存：capabilities.json，按 sha256(base_url + model) 键，TTL 24h，可强制刷新；
  文件损坏时重建空缓存（EC-D5，不中断任务）；
- 消费：编排层按 `capabilities.tool_mode` 决定求解内循环或阶段级执行（W11）。
"""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx


class KeyStoreLike(Protocol):
    """KeyStore 最小端口（has_key / auth_header），供探测附授权头。"""

    @property
    def has_key(self) -> bool: ...

    def auth_header(self) -> str: ...

MODELS_PROBE_TIMEOUT = 2.0
CACHE_TTL_SECONDS = 24 * 3600
CACHE_FILENAME = "capabilities.json"


@dataclass(slots=True)
class ProviderCapabilities:
    """一次探测得到的能力矩阵（缺省为最保守值：高阶能力全 False）。"""

    chat: bool = True
    stream: bool = True
    tools: bool = False
    tool_choice: bool = False
    structured_output: bool = False
    vision: bool = False
    models_endpoint: bool = False
    context_window: int | None = None
    probed_at: str = ""
    probe_source: str = "default"  # default | probe | fixture | cache
    probe_cost_tokens: int = 0  # 探测消耗的 token（当前仅 /models 不产生 token，预留）

    @property
    def tool_mode(self) -> str:
        """编排层消费：tools 可用 → 求解内循环；否则阶段级执行（W11 路由）。"""
        return "tool_loop" if self.tools else "stage_level"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "ProviderCapabilities":
        return cls(**json.loads(raw))


def capability_key(base_url: str, model: str) -> str:
    """缓存键：sha256(base_url + model) 前 16 位（避免明文存 URL/模型名）。"""
    return hashlib.sha256(f"{base_url}::{model}".encode()).hexdigest()[:16]


class CapabilityCache:
    """capabilities.json 缓存：TTL 24h；损坏/越权内容按未命中处理（EC-D5）。"""

    def __init__(self, root: Path, ttl_seconds: int = CACHE_TTL_SECONDS) -> None:
        self._path = root / CACHE_FILENAME
        self._ttl = ttl_seconds

    def load(self, key: str) -> ProviderCapabilities | None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        entry = raw.get(key) if isinstance(raw, dict) else None
        if not isinstance(entry, dict):
            return None
        try:
            age = datetime.now(UTC).timestamp() - float(entry.get("stored_at", 0))
        except (TypeError, ValueError):
            return None
        if age < 0 or age > self._ttl:
            return None
        try:
            return ProviderCapabilities.from_json(entry["capabilities"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def store(self, key: str, caps: ProviderCapabilities) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raw = {}
        except (OSError, json.JSONDecodeError):
            raw = {}
        raw[key] = {"stored_at": datetime.now(UTC).timestamp(), "capabilities": caps.to_json()}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass  # 缓存写失败不阻塞任务（探测结果即时返回，下次重探）


async def probe_models_endpoint(
    base_url: str,
    *,
    keys: KeyStoreLike | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """探测 /models 端点可用性；失败一律 False，绝不判定 Key 无效。"""
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=httpx.Timeout(MODELS_PROBE_TIMEOUT)
        ) as client:
            headers = {}
            if keys is not None and keys.has_key:
                headers["Authorization"] = keys.auth_header()
            resp = await client.get(f"{base_url.rstrip('/')}/models", headers=headers)
            return resp.status_code == 200
    except (httpx.HTTPError, OSError):
        return False


# 离线 fixture 矩阵：OpenAI 兼容端点的高阶能力工程默认（保守登记，probe_source=fixture；
# 上线前以 W14 provider.test 对真实端点实测校准，不准凭接口名假定兼容——开发文档 §8.2）
FIXTURE_MATRIX: dict[str, "ProviderCapabilities"] = {
    "openai": ProviderCapabilities(
        tools=True, tool_choice=True, structured_output=True, models_endpoint=True,
        probe_source="fixture",
    ),
    "deepseek": ProviderCapabilities(
        tools=True, tool_choice=True, structured_output=True, models_endpoint=True,
        probe_source="fixture",
    ),
    "vllm": ProviderCapabilities(
        tools=True, tool_choice=True, models_endpoint=True, probe_source="fixture"
    ),
    "dashscope-compat": ProviderCapabilities(
        tools=True, tool_choice=True, models_endpoint=True, probe_source="fixture"
    ),
}


def fixture_capabilities(provider: str) -> ProviderCapabilities | None:
    """按 provider 名查离线矩阵；未知厂商返回 None（保守降级为 stage_level）。"""
    caps = FIXTURE_MATRIX.get(provider.lower())
    if caps is None:
        return None
    return ProviderCapabilities(**{**asdict(caps), "probed_at": datetime.now(UTC).isoformat()})


async def probe_capabilities(
    base_url: str,
    model: str,
    *,
    provider: str = "",
    keys: KeyStoreLike | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    cache: CapabilityCache | None = None,
    force: bool = False,
) -> ProviderCapabilities:
    """探测入口：缓存优先 → fixture 矩阵叠加 → /models 运行时探测。

    provider 命中 fixture 矩阵时，高阶能力取工程默认；/models 结果只补充
    models_endpoint 字段（以实测为准）。force=True 忽略缓存强制重探。
    """
    key = capability_key(base_url, model)
    if cache is not None and not force:
        cached = cache.load(key)
        if cached is not None:
            cached.probe_source = "cache"  # 本次数据的 provenance：来自缓存
            return cached

    caps = fixture_capabilities(provider) if provider else None
    if caps is None:
        caps = ProviderCapabilities(probe_source="probe")
    caps.models_endpoint = await probe_models_endpoint(base_url, keys=keys, transport=transport)
    caps.probed_at = datetime.now(UTC).isoformat()

    if cache is not None:
        cache.store(key, caps)
    return caps
