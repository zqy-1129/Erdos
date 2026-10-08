"""W14（CT-V2-2）运行时方法测试：握手协商 / 端点探测 / 事件补发 / seq 缓冲。

覆盖（《任务执行手册》W14 验收）：
- initialize 匹配 → compatible=True，capabilities 含 tool_mode/isolation_mode；
- 版本不匹配 → compatible=False 且后续 start_stage 被 SCHEMA_UNSUPPORTED 拒绝；
- provider.test：/models 200 → ok=True + tool_mode；401 → ok=False（不判 Key 无效文案）；
  Key 不经方法参数传递（用引擎注入的 KeyStore）；
- events.replay：seq > after_seq 增量补发、limit 上限、task_id 过滤、last_seq；
- EventEmitter seq 单调 + 环形缓冲丢弃最旧（EC-N4：replay 缓冲上限）。
"""

import io
import json

import httpx
import pytest

from engine.adapters.key_store import KeyStore
from engine.ipc.events import EventEmitter
from engine.ipc.methods import register_all
from engine.ipc.rpc import SCHEMA_UNSUPPORTED
from engine.ipc.server import JsonRpcServer
from engine.ipc.state import EngineState

RUNTIME_INFO = {
    "protocol_version": 2,
    "engine_version": "0.1.0",
    "tool_mode": "tool_loop",
    "isolation_mode": "docker",
}


def _server(events: EventEmitter | None = None, **kwargs) -> tuple[JsonRpcServer, EngineState]:
    state = EngineState()
    server = JsonRpcServer(state, events or EventEmitter(sink=io.StringIO()))
    kwargs.setdefault("key_store", KeyStore())
    kwargs.setdefault("runtime_info", RUNTIME_INFO)
    register_all(server, state, events=events, **kwargs)
    return server, state


async def _call(server: JsonRpcServer, req_id: str, method: str, params: dict) -> dict:
    line = await server.handle_line(
        json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
    )
    return json.loads(line)


# ----------------------------------------------------------------------
# initialize 握手
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_initialize_compatible_reports_capabilities() -> None:
    server, _ = _server()
    resp = await _call(server, "i1", "initialize", {"client_protocol_version": 2})
    assert resp["result"]["compatible"] is True
    assert resp["result"]["protocol_version"] == 2
    assert resp["result"]["engine_version"] == "0.1.0"
    assert resp["result"]["capabilities"] == {"tool_mode": "tool_loop", "isolation_mode": "docker"}
    # 协商通过后 start_stage 正常受理
    resp = await _call(server, "s1", "start_stage", {"task_id": "t1", "stage": "analysis"})
    assert "error" not in resp


@pytest.mark.asyncio
async def test_initialize_mismatch_rejects_start_stage() -> None:
    """版本不匹配 → compatible=False，后续 start_stage 返回 SCHEMA_UNSUPPORTED（拒发任务）。"""
    server, state = _server()
    resp = await _call(server, "i1", "initialize", {"client_protocol_version": 1})
    assert resp["result"]["compatible"] is False
    assert state.protocol_ok is False

    resp = await _call(server, "s1", "start_stage", {"task_id": "t1", "stage": "analysis"})
    assert resp["error"]["code"] == SCHEMA_UNSUPPORTED

    # 重新以匹配版本握手 → 恢复可发任务
    await _call(server, "i2", "initialize", {"client_protocol_version": 2})
    assert state.protocol_ok is True
    resp = await _call(server, "s2", "start_stage", {"task_id": "t1", "stage": "analysis"})
    assert "error" not in resp


@pytest.mark.asyncio
async def test_initialize_without_client_version_defaults_compatible() -> None:
    """无 client_protocol_version（旧调用方）→ 默认兼容（向后兼容路径）。"""
    server, state = _server()
    resp = await _call(server, "i1", "initialize", {})
    assert resp["result"]["compatible"] is True
    assert state.protocol_ok is True


# ----------------------------------------------------------------------
# provider_test
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_provider_test_ok_and_tool_mode() -> None:
    """/models 200 → ok=True + fixture tool_mode；Key 经注入的 KeyStore 附头（不经方法）。"""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json={"data": []})

    keys = KeyStore()
    keys.inject("sk-provider-secret")
    server, _ = _server(
        probe_transport=httpx.MockTransport(handler), key_store=keys,
        runtime_info={**RUNTIME_INFO, "tool_mode": "stage_level"},
    )
    resp = await _call(server, "p1", "provider_test",
                       {"base_url": "https://api.test/v1", "model": "m1", "provider": "openai"})
    assert resp["result"]["ok"] is True
    assert resp["result"]["models_endpoint"] is True
    assert resp["result"]["tool_mode"] == "tool_loop"  # openai fixture → tool_loop
    assert captured["auth"] == "Bearer sk-provider-secret"


@pytest.mark.asyncio
async def test_provider_test_endpoint_down() -> None:
    """端点不可达/401 → ok=False（探测失败不抛异常、不产生'Key 无效'误判文案）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    server, _ = _server(probe_transport=httpx.MockTransport(handler), key_store=KeyStore())
    resp = await _call(server, "p1", "provider_test",
                       {"base_url": "https://api.test/v1", "model": "m1"})
    assert resp["result"]["ok"] is False
    assert resp["result"]["models_endpoint"] is False
    assert resp["result"]["tool_mode"] == "stage_level"


@pytest.mark.asyncio
async def test_provider_test_missing_params() -> None:
    server, _ = _server()
    resp = await _call(server, "p1", "provider_test", {"base_url": "https://x/v1"})
    assert resp["error"]["code"] == -32602  # INVALID_PARAMS


@pytest.mark.asyncio
async def test_provider_test_persists_probe_to_capabilities_cache(tmp_path) -> None:
    """探测结果必须落 capabilities.json：装配期 tool_mode 复用它的实测值（EN-CAP 消费侧）。"""
    from engine.adapters.capabilities import CapabilityCache, capability_key

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    cache = CapabilityCache(tmp_path)
    keys = KeyStore()
    keys.inject("sk-provider-secret")
    server, _ = _server(probe_transport=httpx.MockTransport(handler), key_store=keys,
                        capability_cache=cache)
    resp = await _call(server, "p1", "provider_test",
                       {"base_url": "https://api.test/v1", "model": "m1", "provider": "deepseek"})
    assert resp["result"]["tool_mode"] == "tool_loop"

    stored = cache.load(capability_key("https://api.test/v1", "m1"))
    assert stored is not None, "provider_test 未把实测能力落盘"
    assert stored.tools is True
    assert stored.models_endpoint is True


# ----------------------------------------------------------------------
# events_replay
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_events_replay_incremental_and_filters() -> None:
    """seq > after_seq 增量补发 + limit 上限 + task_id 过滤 + last_seq。"""
    sink = io.StringIO()
    events = EventEmitter(sink=sink, trace_id="trace-r")
    server, _ = _server(events=events)
    events.emit("stage.progress", task_id="t1", stage="analysis", progress=0.05)
    events.emit("stage.progress", task_id="t1", stage="analysis", progress=1.0)
    events.emit("stage.progress", task_id="t2", stage="analysis", progress=0.05)

    resp = await _call(server, "r1", "events_replay", {"after_seq": 1})
    assert [e["seq"] for e in resp["result"]["events"]] == [2, 3]
    assert resp["result"]["last_seq"] == 3

    resp = await _call(server, "r2", "events_replay", {"after_seq": 0, "limit": 1})
    assert [e["seq"] for e in resp["result"]["events"]] == [1]

    resp = await _call(server, "r3", "events_replay", {"after_seq": 0, "task_id": "t1"})
    assert [e["seq"] for e in resp["result"]["events"]] == [1, 2]


def test_emitter_seq_monotonic_and_ring_buffer() -> None:
    """seq 单调递增；环形缓冲丢弃最旧（replay 只见最近 buffer_size 条，EC-N4）。"""
    events = EventEmitter(sink=io.StringIO(), buffer_size=3)
    for i in range(5):
        events.emit("stage.progress", task_id="t1", stage="analysis", progress=i / 5)
    assert events.last_seq == 5
    found = events.replay(after_seq=0)
    assert [e["seq"] for e in found] == [3, 4, 5]
