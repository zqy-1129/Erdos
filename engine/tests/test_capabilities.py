"""EN-CAP（W7）能力探测与缓存测试。

覆盖（《任务执行手册》W7 验收）：
- 四类端点 fixture 矩阵（OpenAI/DeepSeek/vLLM/dashscope-compat）高阶能力默认 + /models 实测叠加；
- 探测失败（404/401/超时/网络）只记 models_endpoint=False，**不得判定 Key 无效**；
- 缓存：TTL 24h 命中/过期、损坏文件重建（EC-D5）、force 强制刷新；
- 降级路由：tool_mode —— tools=False → stage_level。
"""

import json
import time

import httpx
import pytest

from engine.adapters.capabilities import (
    FIXTURE_MATRIX,
    CapabilityCache,
    ProviderCapabilities,
    capability_key,
    fixture_capabilities,
    probe_capabilities,
    probe_models_endpoint,
)


# ----------------------------------------------------------------------
# fixture 矩阵
# ----------------------------------------------------------------------
def test_fixture_matrix_covers_four_endpoint_families() -> None:
    """四类 OpenAI 兼容端点家族的离线工程默认矩阵就位（待 W14 实测校准）。"""
    for provider in ("openai", "deepseek", "vllm", "dashscope-compat"):
        caps = fixture_capabilities(provider)
        assert caps is not None
        assert caps.tools is True
        assert caps.probe_source == "fixture"
        assert caps.probed_at != ""


def test_unknown_provider_returns_none_conservative() -> None:
    """未知厂商 → None（保守降级 stage_level，不凭接口名假定兼容）。"""
    assert fixture_capabilities("unknown-vendor") is None
    assert fixture_capabilities("unknown-vendor") is None or True


def test_tool_mode_degradation_routing() -> None:
    """降级路由：tools=False → stage_level；tools=True → tool_loop。"""
    assert ProviderCapabilities().tool_mode == "stage_level"
    assert ProviderCapabilities(tools=True).tool_mode == "tool_loop"


# ----------------------------------------------------------------------
# /models 运行时探测（不判定 Key 无效红线）
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_probe_models_endpoint_matrix() -> None:
    """200 → True；404/401/超时/连接拒绝 → False（不抛异常、不判 Key 无效）。"""

    def ok_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "test-model"}]})

    def not_found_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "no models route"})

    def unauthorized_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("probe timeout")

    def transport_error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    assert await probe_models_endpoint("https://api.test/v1", transport=httpx.MockTransport(ok_handler))
    assert not await probe_models_endpoint(
        "https://api.test/v1", transport=httpx.MockTransport(not_found_handler)
    )
    assert not await probe_models_endpoint(
        "https://api.test/v1", transport=httpx.MockTransport(unauthorized_handler)
    )
    assert not await probe_models_endpoint(
        "https://api.test/v1", transport=httpx.MockTransport(timeout_handler)
    )
    assert not await probe_models_endpoint(
        "https://api.test/v1", transport=httpx.MockTransport(transport_error_handler)
    )


@pytest.mark.asyncio
async def test_probe_models_sends_auth_header_when_key_present() -> None:
    """已注入 Key 时探测附 Authorization 头；未注入时不附（不触 auth_header 异常）。"""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json={"data": []})

    transport = httpx.MockTransport(handler)
    assert await probe_models_endpoint("https://api.test/v1", transport=transport)
    assert seen["auth"] == ""

    from engine.adapters.key_store import KeyStore

    keys = KeyStore()
    keys.inject("sk-probe-secret")
    assert await probe_models_endpoint("https://api.test/v1", keys=keys, transport=transport)
    assert seen["auth"].startswith("Bearer ")


# ----------------------------------------------------------------------
# probe_capabilities 组合 + 缓存
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_probe_capabilities_combines_fixture_and_models_probe() -> None:
    """provider 命中 fixture → 高阶能力取工程默认；models_endpoint 以实测覆盖。"""
    caps = await probe_capabilities(
        "https://api.test/v1",
        "test-model",
        provider="openai",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": []})),
    )
    assert caps.tools is True
    assert caps.models_endpoint is True

    caps_down = await probe_capabilities(
        "https://api.test/v1",
        "test-model",
        provider="openai",
        transport=httpx.MockTransport(lambda request: httpx.Response(500, text="boom")),
    )
    assert caps_down.tools is True  # fixture 高阶能力不受 /models 故障影响
    assert caps_down.models_endpoint is False


@pytest.mark.asyncio
async def test_probe_capabilities_cache_ttl_and_force(tmp_path) -> None:  # noqa: ANN001
    """缓存命中（TTL 24h 内）→ probe_source=cache；force=True 强制重探。"""
    cache = CapabilityCache(tmp_path)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"data": []}))

    first = await probe_capabilities(
        "https://api.test/v1", "m1", provider="openai", transport=transport, cache=cache
    )
    assert first.probe_source == "fixture"

    second = await probe_capabilities(
        "https://api.test/v1", "m1", provider="openai", transport=transport, cache=cache
    )
    assert second.probe_source == "cache"

    forced = await probe_capabilities(
        "https://api.test/v1", "m1", provider="openai", transport=transport, cache=cache, force=True
    )
    assert forced.probe_source == "fixture"


def test_cache_expired_entry_ignored(tmp_path) -> None:  # noqa: ANN001
    """TTL 过期（人工改 stored_at）→ 未命中重探。"""
    cache = CapabilityCache(tmp_path)
    key = capability_key("https://api.test/v1", "m1")
    caps = ProviderCapabilities(tools=True, probe_source="fixture", probed_at="2026-01-01T00:00:00+00:00")
    cache.store(key, caps)
    # 篡改 stored_at 为 25h 前
    raw = json.loads((tmp_path / "capabilities.json").read_text(encoding="utf-8"))
    raw[key]["stored_at"] = time.time() - 25 * 3600
    (tmp_path / "capabilities.json").write_text(json.dumps(raw), encoding="utf-8")
    assert cache.load(key) is None


def test_cache_corrupt_file_rebuilds(tmp_path) -> None:  # noqa: ANN001
    """缓存文件损坏（EC-D5）→ load 返回 None、store 重建，不抛异常。"""
    cache = CapabilityCache(tmp_path)
    (tmp_path / "capabilities.json").write_text("{not-json", encoding="utf-8")
    assert cache.load(capability_key("https://x/v1", "m")) is None
    cache.store(capability_key("https://x/v1", "m"), ProviderCapabilities(models_endpoint=True))
    assert cache.load(capability_key("https://x/v1", "m")) is not None


def test_fixture_matrix_entries_are_immutable_snapshots() -> None:
    """fixture 返回副本（修改返回值不污染全局矩阵）。"""
    caps = fixture_capabilities("openai")
    assert caps is not None
    caps.tools = False
    assert FIXTURE_MATRIX["openai"].tools is True
