"""留痕四类完整性守护（W23 · EN-TRAIL-COMPLETE 第一批：manual_edit + artifact_index）。

10-10 完成度审计发现：`TrailRecorder` 四个记录方法齐、单测齐，但生产链路只有三类有埋点——
`record_manual_edit` **零调用方**，工具产物也不登记 `artifact_index`，而文档记 SP1-6 ✅。
「有 API、没接线」在这个项目里反复出现，所以判据全部走生产入口：RPC `answer_gate` 与
`ToolRegistry.dispatch`。

三条判据：
- 人工门禁决策（pass 与 reject 都算）必须落 `manual_edit`；
- 留痕写失败必须让这次操作失败，且先写留痕再推进状态机（否则留下「放行过却查无记录」的既成事实）；
- 产物只有「工具自己声明了类别 + 文件真在盘上」才登记：不编造 sha256，也不编造类别。
"""

import io
import json
from pathlib import Path

from pydantic import BaseModel

from engine.ipc.events import EventEmitter
from engine.ipc.methods import register_all
from engine.ipc.server import JsonRpcServer
from engine.ipc.state import EngineState
from engine.tools.base import ToolContext, ToolResult, ToolSpec
from engine.tools.registry import ToolRegistry
from engine.trail.recorder import TrailRecorder
from engine.trail.store import EventType, TrailStore

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = REPO_ROOT / "contracts" / "engine-rpc.schema.json"
TASK_ID = "t1"


class _PlotArgs(BaseModel):
    title: str = "图"


class _Call:
    """`ToolCallLike` 的最小实现（与 adapters.openai_compat.ToolCall 同形）。"""

    def __init__(self, name: str, arguments_json: str = "{}", call_id: str = "c1") -> None:
        self._name, self._arguments_json, self._id = name, arguments_json, call_id

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str:
        return self._name

    @property
    def arguments_json(self) -> str:
        return self._arguments_json


def _sink() -> io.StringIO:
    """事件与留痕是两条独立通道：本文件只验留痕，事件写进内存即弃。"""
    return io.StringIO()


def _store(tmp_path: Path) -> TrailStore:
    return TrailStore(str(tmp_path / "audit.db"))


def _recorder_of(tmp_path: Path) -> TrailRecorder:
    return TrailRecorder(_store(tmp_path))


def _details(store: TrailStore, event_type: str) -> list[dict]:
    return [e.detail for e in store.events(TASK_ID) if e.event_type == event_type]


async def _rpc(server: JsonRpcServer, method: str, params: dict) -> dict:
    line = json.dumps({"jsonrpc": "2.0", "id": method, "method": method, "params": params})
    return json.loads(await server.handle_line(line))


async def _gated_server(tmp_path: Path) -> tuple[JsonRpcServer, EngineState]:
    """装配到「门禁待决」：task_create → start_stage（不注入 runner，纯受理语义）。"""
    state = EngineState()
    server = JsonRpcServer(state, EventEmitter(sink=_sink()))
    register_all(server, state, trail=_recorder_of(tmp_path))
    created = await _rpc(
        server, "task_create", {"task_id": TASK_ID, "title": "示例题", "problem_text": "求回归斜率"}
    )
    assert "result" in created, created
    started = await _rpc(server, "start_stage", {"task_id": TASK_ID, "stage": "analysis"})
    assert "result" in started, started
    return server, state


# ----------------------------------------------------------------------
# manual_edit：F-007 第四类留痕此前零生产调用方
# ----------------------------------------------------------------------
async def test_answer_gate_pass_records_manual_edit(tmp_path: Path) -> None:
    server, _ = await _gated_server(tmp_path)
    response = await _rpc(
        server, "answer_gate",
        {
            "task_id": TASK_ID, "gate": "gate_analysis", "decision": "pass",
            "feedback": "斜率口径可接受",
        },
    )
    assert "result" in response, response

    store = _store(tmp_path)
    try:
        details = _details(store, EventType.MANUAL_EDIT)
        assert details, "人工门禁决策没有 manual_edit 留痕 = F-007 四类缺一"
        note = details[0]["note"]
        assert "pass" in note and "gate_analysis" in note
        assert "斜率口径可接受" in note, "人工意见是留痕的一部分，丢了就无法复核谁放行过"
    finally:
        store.close()


async def test_answer_gate_reject_is_recorded_too(tmp_path: Path) -> None:
    """reject 同样是人工决策：只记 pass 会把「人否决过」这件事从审计里抹掉。"""
    server, _ = await _gated_server(tmp_path)
    await _rpc(
        server, "answer_gate",
        {"task_id": TASK_ID, "gate": "gate_analysis", "decision": "reject", "feedback": "缺约束"},
    )
    store = _store(tmp_path)
    try:
        notes = [d["note"] for d in _details(store, EventType.MANUAL_EDIT)]
        assert any("reject" in n and "缺约束" in n for n in notes), notes
    finally:
        store.close()


async def test_manual_edit_failure_blocks_state_advance(tmp_path: Path) -> None:
    """顺序判据：留痕写失败 → 这次操作失败，且门禁状态没被推进。"""

    class _FailingStore(TrailStore):
        def append_event(self, *args, **kwargs):  # noqa: ANN002, ANN003 - 模拟磁盘写失败
            raise OSError("disk full")

    state = EngineState()
    server = JsonRpcServer(state, EventEmitter(sink=_sink()))
    register_all(server, state, trail=TrailRecorder(_FailingStore(str(tmp_path / "audit.db"))))
    await _rpc(server, "task_create", {"task_id": TASK_ID, "title": "t", "problem_text": "p"})
    await _rpc(server, "start_stage", {"task_id": TASK_ID, "stage": "analysis"})
    assert state.orchestrator is not None
    stage_before = state.orchestrator.current_stage

    response = await _rpc(
        server, "answer_gate", {"task_id": TASK_ID, "gate": "gate_analysis", "decision": "pass"}
    )
    assert "error" in response, "留痕写失败却静默成功 = F-007 红线破口"
    assert "disk full" in response["error"]["message"]
    assert state.orchestrator.current_stage == stage_before, (
        "留痕没写成却推进了阶段：审计里会出现「放行过但无记录」的既成事实"
    )


async def test_no_trail_injection_keeps_rpc_working(tmp_path: Path) -> None:
    """未注入 trail（单测/骨架路径）时行为不变：留痕是加性接线，不是新前置条件。"""
    state = EngineState()
    server = JsonRpcServer(state, EventEmitter(sink=_sink()))
    register_all(server, state)
    await _rpc(server, "task_create", {"task_id": TASK_ID, "title": "t", "problem_text": "p"})
    await _rpc(server, "start_stage", {"task_id": TASK_ID, "stage": "analysis"})
    response = await _rpc(
        server, "answer_gate", {"task_id": TASK_ID, "gate": "gate_analysis", "decision": "pass"}
    )
    assert "result" in response, response


# ----------------------------------------------------------------------
# artifact：只在证据真实存在时登记
# ----------------------------------------------------------------------
def _figure_spec(*, produce: bool) -> ToolSpec:
    async def _executor(args: _PlotArgs, ctx: ToolContext) -> ToolResult:
        if produce:
            (ctx.work_dir / "figure.png").write_bytes(b"\x89PNG fake-bytes")
        return ToolResult(ok=True, summary="图表已生成", result_ref="figure.png")

    return ToolSpec(
        name="plot_figure", description="d", args_model=_PlotArgs,
        danger_level="high", executor=_executor, artifact_kind="figure",
    )


async def _dispatch(tmp_path: Path, spec: ToolSpec) -> None:
    registry = ToolRegistry(events=EventEmitter(sink=_sink()), trail=_recorder_of(tmp_path))
    registry.register(spec)
    work_dir = tmp_path / "tasks" / TASK_ID
    work_dir.mkdir(parents=True, exist_ok=True)
    await registry.dispatch(
        _Call(spec.name, '{"title": "x"}'),
        ToolContext(task_id=TASK_ID, stage="solving", work_dir=work_dir),
    )


async def test_produced_figure_registers_artifact_index(tmp_path: Path) -> None:
    await _dispatch(tmp_path, _figure_spec(produce=True))
    store = _store(tmp_path)
    try:
        artifacts = store.artifacts(TASK_ID)
        assert [a["kind"] for a in artifacts] == ["figure"]
        assert artifacts[0]["sha256"] and Path(artifacts[0]["file_path"]).exists()
        assert artifacts[0]["size_bytes"] > 0
        assert _details(store, EventType.ARTIFACT), "artifact_index 与 audit_trail 必须同批落"
        tool_details = _details(store, EventType.TOOL_CALL)
        assert tool_details[0]["artifact_kind"] == "figure", "tool_call 里要能看出登记了什么产物"
        assert "artifact_missing" not in tool_details[0]
    finally:
        store.close()


async def test_claimed_but_absent_figure_is_not_registered(tmp_path: Path) -> None:
    """工具「说它画了图」与「盘上确实有图」必须可区分——不能拿不存在的文件算 sha256。"""
    await _dispatch(tmp_path, _figure_spec(produce=False))
    store = _store(tmp_path)
    try:
        assert store.artifacts(TASK_ID) == []
        assert not _details(store, EventType.ARTIFACT)
        tool_details = _details(store, EventType.TOOL_CALL)
        assert any(d.get("artifact_missing") is True for d in tool_details), tool_details
    finally:
        store.close()


async def test_one_dispatch_writes_exactly_one_tool_call_record(tmp_path: Path) -> None:
    """留痕条数 = 调用次数（审计按条数复核；为产物状态另写一条会把"几次调用"答错）。"""
    await _dispatch(tmp_path, _figure_spec(produce=True))
    store = _store(tmp_path)
    try:
        details = _details(store, EventType.TOOL_CALL)
        assert len(details) == 1, f"一次调用写了几条 tool_call：{len(details)}"
        assert details[0]["artifact_kind"] == "figure"
    finally:
        store.close()


async def test_tool_without_declared_kind_registers_no_artifact(tmp_path: Path) -> None:
    """execute_code 可能产出任意文件，引擎侧不可枚举 → 不硬猜类别（猜错就是编造证据）。"""

    async def _executor(args: _PlotArgs, ctx: ToolContext) -> ToolResult:
        (ctx.work_dir / "mystery.bin").write_bytes(b"x")
        return ToolResult(ok=True, summary="done", result_ref="mystery.bin")

    await _dispatch(
        tmp_path,
        ToolSpec(
            name="execute_code", description="d", args_model=_PlotArgs,
            danger_level="high", executor=_executor,
        ),
    )
    store = _store(tmp_path)
    try:
        assert store.artifacts(TASK_ID) == []
        assert not any(d.get("artifact_missing") for d in _details(store, EventType.TOOL_CALL))
    finally:
        store.close()


# ----------------------------------------------------------------------
# 类别单源：扩展名映射 ⇔ 默认注册表声明 ⇔ 契约 enum
# ----------------------------------------------------------------------
def test_kind_for_file_never_invents_a_category() -> None:
    from engine.trail.recorder import kind_for_file

    assert kind_for_file("figure.png") == "figure"
    assert kind_for_file("result.CSV") == "data"  # 大小写不敏感
    assert kind_for_file("mystery.bin") is None, "认不出就返回 None：兜底成 data 就是编造证据"


def test_declared_kinds_are_within_contract_enum() -> None:
    from engine.tools import build_default_registry
    from engine.trail.recorder import _ARTIFACT_KIND_BY_SUFFIX

    schema = json.loads(CONTRACT.read_text(encoding="utf-8"))
    allowed = set(schema["events"]["artifact.ready"]["properties"]["artifact"]["enum"])
    assert set(_ARTIFACT_KIND_BY_SUFFIX.values()) <= allowed, "扩展名映射造出了契约外的类别"

    class _NoSandbox:
        async def execute(self, code, env, work_dir, *args, **kwargs):  # noqa: ANN001
            raise AssertionError("本用例只装配注册表，不执行代码")

    registry = build_default_registry(_NoSandbox())
    declared = {
        spec.artifact_kind for spec in registry.specs() if spec.artifact_kind is not None
    }
    assert declared == {"figure"}, f"默认注册表里声明产物的工具变了：{declared}"
    assert declared <= allowed
