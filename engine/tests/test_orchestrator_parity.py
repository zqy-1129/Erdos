"""双轨校验 T1：同一场景在 LangGraph 后端与自研内核后端上各跑一遍，逐序列比对。

为什么这么测（不是重复劳动）：本项目反复出现的缺陷形状是"行为差异不报错、只是语义不同"
（沙箱相对 work_dir 导致代码从未执行却一路"成功"；硬检查接进编排链后 RPC 驱动空转到超时）。
自研内核的最大风险同形——它可能"跑通了"，但推进语义与框架不同。所以比对必须机器判定、
进门禁，并且**自身可被测坏**（末组用例专门测这点）。

比对的六个序列（不一致就打印首个分歧点）：
1. 动作序列   —— run_current_stage / answer_gate 每步返回值；
2. 状态快照   —— 每步后的 `state.snapshot()`；
3. 挂起视图   —— 每步后的 `graph().get_state().next` 与 tasks 数；
4. 检查点写序 —— save_stage/delete_stage 全部入参（落库是恢复与审计的依据）；
5. 副作用序列 —— runner 被调用的阶段（重放/重跑判定）；
6. 异常       —— 类型 + 消息（节点体两套后端共用，消息必须逐字相同）。

runner 与沙箱都是替身，不产生真实模型调用（DEC-024 口径）。
"""

import asyncio
import copy
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from engine.adapters.openai_compat import ChatMessage, ChatResult, ToolCall, Usage
from engine.checkpoint.store import CheckpointRecord
from engine.orchestrator.backend import IMPL_LANGGRAPH, IMPL_NATIVE
from engine.orchestrator.graph import STAGES, StageOrchestrator
from engine.orchestrator.operations import OperationLog
from engine.orchestrator.solve_loop import SolveLoop
from engine.sandbox.base import ExecutionResult
from engine.tools import build_default_registry

IMPLS = (IMPL_LANGGRAPH, IMPL_NATIVE)
TASK_ID = "parity"


# ----------------------------------------------------------------------
# 观测替身
# ----------------------------------------------------------------------
class RecordingStore:
    """内存检查点存储 + 写序列记录（`CheckpointStore` 端口的测试实现）。

    `completed_stages` 按**最近一次写入**排序，与 SQLite 版 `ORDER BY updated_at` 同口径
    （门禁通过时整行被覆盖 → updated_at 前进 → 该行排到最后）。这条顺序直接决定恢复定位，
    替身要是写错了，双轨比对就会拿一个错基准去比两个实现对不对。
    """

    def __init__(self) -> None:
        self.writes: list[tuple] = []
        self._rows: dict[tuple[str, str], tuple[int, CheckpointRecord]] = {}
        self._seq = 0

    def save_stage(self, task_id: str, stage: str, status: str, step: int, data: dict) -> None:
        self._seq += 1
        self.writes.append(("save", self._seq, task_id, stage, status, step, copy.deepcopy(data)))
        self._rows[(task_id, stage)] = (
            self._seq,
            CheckpointRecord(
                task_id=task_id, stage=stage, status=status, step=step, data=copy.deepcopy(data)
            ),
        )

    def delete_stage(self, task_id: str, stage: str) -> None:
        self._seq += 1
        self.writes.append(("delete", self._seq, task_id, stage))
        self._rows.pop((task_id, stage), None)

    def completed_stages(self, task_id: str) -> list[CheckpointRecord]:
        ordered = sorted(self._rows.values(), key=lambda item: item[0])
        return [record for _seq, record in ordered if record.task_id == task_id]


def _contract_output(stage: str, work_dir: Path) -> dict:
    """各阶段契约形状的桩产出（必须过 SP1-3 硬检查；字段形状同 StagePipeline）。

    writing 的 `paper_path` 会被硬检查做 `Path.exists()` 校验、`paper_md` 要含四个章节标记，
    所以这里必须真的落一个文件——否则"四阶段全通过"场景会静默退化成硬检查驳回场景，
    两条轨迹仍然一致（假绿灯），比对就白做了。
    """
    if stage == "analysis":
        return {
            "stage": stage,
            "insights": ["决策变量与目标", "约束条件"],
            "usage": {"prompt_tokens": 5},
        }
    if stage == "modeling":
        return {
            "stage": stage,
            "assumptions": "变量独立",
            "modeling_detail": "min f(x)=Σwᵢ·xᵢ",
            "variables": ["slope"],
            "usage": {"prompt_tokens": 5},
        }
    if stage == "solving":
        return {
            "stage": stage,
            "exit_code": 0,
            "stdout": "slope=1.9850",
            "timed_out": False,
            "artifacts": [],
            "usage": {"prompt_tokens": 5},
        }
    paper = (
        "# 桩论文\n\n## 摘要\n\n方法与结果。\n\n## 一、问题重述\n\n题面。\n\n"
        "## 二、模型假设与建模\n\n假设。\n\n## 三、求解与结果\n\nslope=1.9850\n\n## 四、结论\n\n结论。\n"
    )
    paper_path = work_dir / "paper.md"
    paper_path.parent.mkdir(parents=True, exist_ok=True)
    paper_path.write_text(paper, encoding="utf-8")
    return {
        "stage": stage,
        "paper_md": paper,
        "paper_sha256": hashlib.sha256(paper.encode("utf-8")).hexdigest(),
        "paper_path": str(paper_path),
        "usage": {"prompt_tokens": 5},
    }


class ScriptedRunner:
    """阶段执行器替身：按阶段返回产出（可按场景覆盖首轮），记录调用序列。"""

    def __init__(self, work_dir: Path, overrides: dict[str, list[dict]] | None = None) -> None:
        self._work_dir = work_dir
        self._overrides = copy.deepcopy(overrides or {})
        self.calls: list[str] = []

    async def run(self, task_id: str, stage: str) -> dict:
        self.calls.append(stage)
        queue = self._overrides.get(stage)
        if queue:
            return copy.deepcopy(queue.pop(0))
        return copy.deepcopy(_contract_output(stage, self._work_dir))


@dataclass(slots=True)
class Trace:
    actions: list[Any] = field(default_factory=list)
    snapshots: list[dict] = field(default_factory=list)
    suspensions: list[tuple] = field(default_factory=list)
    writes: list[tuple] = field(default_factory=list)
    node_calls: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    runner: "ScriptedRunner | None" = field(default=None, compare=False)


def _record(trace: Trace, orch: StageOrchestrator, store: RecordingStore) -> None:
    trace.snapshots.append(orch.state.snapshot())
    state = orch.graph().get_state({"configurable": {"thread_id": orch.state.task_id}})
    trace.suspensions.append((tuple(state.next), len(state.tasks)))
    assert trace.runner is not None
    trace.node_calls = list(trace.runner.calls)
    trace.writes = list(store.writes)


async def _step(trace: Trace, awaitable: Awaitable[Any], *, label: str | None = None) -> Any:
    """执行一步；返回值与异常都记进轨迹（异常本身就是被比对的对象）。"""
    try:
        result = await awaitable
    except Exception as exc:  # noqa: BLE001 - 异常类型与消息都要比对
        trace.errors.append(f"{type(exc).__name__}: {exc}")
        return None
    trace.actions.append({"label": label} if label else result)
    return result


# ----------------------------------------------------------------------
# 场景：操作序列完全相同，只有后端不同
# ----------------------------------------------------------------------
async def _scenario_four_stages(
    orch: StageOrchestrator, store: RecordingStore, trace: Trace
) -> None:
    for _ in STAGES:
        await _step(trace, orch.run_current_stage())
        _record(trace, orch, store)
        await _step(trace, orch.answer_gate("pass"))
        _record(trace, orch, store)


async def _scenario_reject_then_pass(
    orch: StageOrchestrator, store: RecordingStore, trace: Trace
) -> None:
    await _step(trace, orch.run_current_stage())
    _record(trace, orch, store)
    await _step(trace, orch.answer_gate("reject", feedback="假设与数据脱节"))
    _record(trace, orch, store)
    await _step(trace, orch.run_current_stage())
    _record(trace, orch, store)
    await _step(trace, orch.answer_gate("pass"))
    _record(trace, orch, store)


async def _scenario_hard_check_violation(
    orch: StageOrchestrator, store: RecordingStore, trace: Trace
) -> None:
    """首轮 analysis 违规（insights 全空白）→ 自动 reject 且不落检查点；次轮合规 → 挂人工门禁。"""
    data = await _step(trace, orch.run_current_stage())
    trace.actions.append({"returned": data, "hard_reject": orch.last_hard_reject})
    _record(trace, orch, store)
    await _step(trace, orch.run_current_stage())
    _record(trace, orch, store)
    await _step(trace, orch.answer_gate("pass"))
    _record(trace, orch, store)


async def _scenario_gate_pending_crash_restore(
    orch: StageOrchestrator, store: RecordingStore, trace: Trace, impl: str
) -> None:
    """门禁未决窗口"崩溃"：换新编排器从同一 store 恢复，必须重挂门禁且不重放阶段副作用。"""
    await _step(trace, orch.run_current_stage())
    _record(trace, orch, store)
    assert trace.runner is not None
    restored = StageOrchestrator.restore(TASK_ID, store, runner=trace.runner.run, impl=impl)
    trace.actions.append({"restored_stage": restored.current_stage})
    _record(trace, restored, store)
    await _step(trace, restored.run_current_stage())
    _record(trace, restored, store)
    await _step(trace, restored.answer_gate("pass"))
    _record(trace, restored, store)


async def _scenario_skeleton_mode(
    orch: StageOrchestrator, store: RecordingStore, trace: Trace
) -> None:
    """骨架模式（默认 runner）：无挂起门禁时 answer_gate 走 SP1-1 兼容的直接驱动分支。"""
    await _step(trace, orch.run_current_stage())
    _record(trace, orch, store)
    await _step(trace, orch.answer_gate("reject"))
    _record(trace, orch, store)


async def _scenario_illegal_decision(
    orch: StageOrchestrator, store: RecordingStore, trace: Trace
) -> None:
    await _step(trace, orch.run_current_stage())
    _record(trace, orch, store)
    await _step(trace, orch.answer_gate("maybe"))
    _record(trace, orch, store)


async def _scenario_stale_pass(
    orch: StageOrchestrator, store: RecordingStore, trace: Trace
) -> None:
    """真实模式下阶段未执行就 pass → EC-U4 防御必须抛错（防重复 pass 把未执行阶段标通过）。"""
    await _step(trace, orch.answer_gate("pass"))
    _record(trace, orch, store)
    await _step(trace, orch.run_current_stage())
    _record(trace, orch, store)


SCENARIOS: dict[str, Callable[..., Awaitable[None]]] = {
    "four_stages_pass": _scenario_four_stages,
    "reject_then_pass": _scenario_reject_then_pass,
    "hard_check_violation": _scenario_hard_check_violation,
    "gate_pending_crash_restore": _scenario_gate_pending_crash_restore,
    "skeleton_mode": _scenario_skeleton_mode,
    "illegal_decision": _scenario_illegal_decision,
    "stale_pass_rejected": _scenario_stale_pass,
}
def _violating_first_round(work_dir: Path) -> dict:
    return {
        "analysis": [
            {"stage": "analysis", "insights": ["  "], "usage": {"prompt_tokens": 1}},
            _contract_output("analysis", work_dir),
        ]
    }


async def _run_scenario(scenario: str, impl: str, work_dir: Path) -> Trace:
    runner = ScriptedRunner(
        work_dir,
        _violating_first_round(work_dir) if scenario == "hard_check_violation" else None,
    )
    trace = Trace(runner=runner)
    store = RecordingStore()
    orch = StageOrchestrator(
        TASK_ID,
        checkpoint=store,
        runner=None if scenario == "skeleton_mode" else runner.run,
        impl=impl,
    )
    if scenario == "gate_pending_crash_restore":
        await SCENARIOS[scenario](orch, store, trace, impl)
    else:
        await SCENARIOS[scenario](orch, store, trace)
    return trace


def _first_divergence(a: Trace, b: Trace) -> str:
    for name in ("actions", "snapshots", "suspensions", "writes", "node_calls", "errors"):
        left, right = getattr(a, name), getattr(b, name)
        if left == right:
            continue
        for index, (x, y) in enumerate(zip(left, right, strict=False)):
            if x != y:
                return f"{name}[{index}]: langgraph={x!r} != native={y!r}"
        return f"{name}: 条数不同 langgraph={len(left)} native={len(right)}"
    return "无分歧"


# ----------------------------------------------------------------------
# 主图双轨
# ----------------------------------------------------------------------
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
async def test_stage_orchestrator_parity(scenario: str, tmp_path: Path) -> None:
    # 两后端共用同一个 work_dir：桩产出里的 paper_path 是被比对字段之一，
    # 各给一个目录会让"路径字符串不同"伪装成实现分歧。
    left = await _run_scenario(scenario, IMPL_LANGGRAPH, tmp_path)
    right = await _run_scenario(scenario, IMPL_NATIVE, tmp_path)

    message = _first_divergence(left, right)
    assert right == left, f"{scenario}: {message}"
    # 反"两边都空跑"：每个场景都必须真的产生过动作或异常
    assert left.actions or left.errors, f"{scenario}: 场景没有产生任何可观测结果"


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
async def test_parity_scenarios_really_write_checkpoints(scenario: str, tmp_path: Path) -> None:
    """防"比对两个空轨迹"式假绿灯：每个场景都必须真的落过库。"""
    trace = await _run_scenario(scenario, IMPL_NATIVE, tmp_path / IMPL_NATIVE)
    assert [row[0] for row in trace.writes], (scenario, trace.writes)


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
async def test_parity_traces_are_not_trivially_empty(scenario: str, tmp_path: Path) -> None:
    trace = await _run_scenario(scenario, IMPL_LANGGRAPH, tmp_path / "empty")
    assert len(trace.snapshots) >= 2, scenario
    assert len(trace.suspensions) == len(trace.snapshots), scenario


async def test_four_stages_pass_scenario_covers_the_all_pass_path(tmp_path: Path) -> None:
    """正向对照：全通过场景必须真的"全通过"。

    这条是被真实事故逼出来的——夹具最初把论文文本写少了章节、paper_path 指向不存在的文件，
    于是硬检查在 writing 上驳回，"四阶段全通过"静默变成了"违规自动驳回"场景；
    两套后端仍然一致，双轨比对全绿却什么都没覆盖。轨迹一致不等于覆盖成立，所以这里
    直接断言场景语义：零异常、八步动作、四阶段都落过两态检查点。
    """
    trace = await _run_scenario("four_stages_pass", IMPL_LANGGRAPH, tmp_path)

    assert trace.errors == [], trace.errors
    assert len(trace.actions) == 2 * len(STAGES)
    assert [a["action"] for a in trace.actions[1::2]] == [
        "next_stage", "next_stage", "next_stage", "complete"
    ]
    assert trace.node_calls == list(STAGES)
    assert [(row[3], row[4]) for row in trace.writes] == [
        (stage, status)
        for stage in STAGES
        for status in ("stage_done", "done")
    ]
    assert trace.suspensions[-1] == ((), 0)  # 最后一步走到 END，无残留挂起


# ----------------------------------------------------------------------
# 守护可测坏：证明这套比对真的会红
# ----------------------------------------------------------------------
def test_comparator_detects_action_divergence() -> None:
    a = Trace(actions=[{"action": "next_stage", "stage": "modeling"}])
    b = Trace(actions=[{"action": "next_stage", "stage": "solving"}])
    assert "actions[0]" in _first_divergence(a, b)


def test_comparator_detects_missing_checkpoint_write() -> None:
    a = Trace(writes=[("save", 1, TASK_ID, "analysis", "stage_done", 0, {})])
    b = Trace()
    assert "条数不同" in _first_divergence(a, b)


def test_comparator_detects_suspension_change() -> None:
    a = Trace(suspensions=[(("gate_analysis",), 1)])
    b = Trace(suspensions=[((), 0)])
    assert "suspensions[0]" in _first_divergence(a, b)


def test_comparator_reports_error_message_drift() -> None:
    a = Trace(errors=["ValueError: decision 必须为 pass 或 reject"])
    b = Trace(errors=["ValueError: decision 非法"])
    assert "errors[0]" in _first_divergence(a, b)


def test_recording_store_orders_by_latest_write() -> None:
    """替身自身也要被测：门禁通过升级后该行必须排到最后（与 SQLite ORDER BY updated_at 同义）。"""
    store = RecordingStore()
    store.save_stage(TASK_ID, "analysis", "stage_done", 0, {})
    store.save_stage(TASK_ID, "modeling", "stage_done", 0, {})
    store.save_stage(TASK_ID, "analysis", "done", 0, {})
    assert [r.stage for r in store.completed_stages(TASK_ID)] == ["modeling", "analysis"]


# ----------------------------------------------------------------------
# 图自省双轨（S9）
# ----------------------------------------------------------------------
@pytest.mark.parametrize("impl", IMPLS)
async def test_graph_introspection_shape(impl: str) -> None:
    orch = StageOrchestrator("t-int", impl=impl)
    nodes = {name for name in orch.graph().get_graph().nodes if not name.startswith("__")}
    assert nodes == {*STAGES, *[f"gate_{s}" for s in STAGES], "finish"}
    await orch.run_current_stage()
    state = orch.graph().get_state({"configurable": {"thread_id": "t-int"}})
    assert state.next == ("gate_analysis",)
    assert len(state.tasks) == 1


@pytest.mark.parametrize("impl", IMPLS)
async def test_stage_order_guard_message_is_identical(impl: str) -> None:
    orch = StageOrchestrator("t-jump", impl=impl)
    with pytest.raises(RuntimeError, match="非法阶段索引：99"):
        await orch.graph().ainvoke(
            {"task_id": "t-jump", "stage_index": 99, "results": {}},
            {"configurable": {"thread_id": "t-jump-bad"}},
        )


# ----------------------------------------------------------------------
# 内循环双轨（S10/S11）
# ----------------------------------------------------------------------
class ScriptedLLM:
    def __init__(self, results: list[ChatResult]) -> None:
        self._queue = list(results)
        self.observed: list[list[str]] = []

    async def __call__(
        self,
        messages: list[ChatMessage],
        tools: list[dict],
        on_delta: Callable[[str], None] | None = None,
    ) -> ChatResult:
        self.observed.append([m.role for m in messages])
        return self._queue.pop(0)


class CountingSandbox:
    def __init__(self, results: list[ExecutionResult] | None = None) -> None:
        self.codes: list[str] = []
        self._results = list(results or [])

    async def execute(self, code: str, files: dict, work_dir: Path) -> ExecutionResult:
        self.codes.append(code)
        if self._results:
            return self._results.pop(0)
        return ExecutionResult(exit_code=0, stdout="ok", stderr="", artifacts=[])


def _usage() -> Usage:
    return Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5)


def _final(text: str) -> ChatResult:
    return ChatResult(content=text, usage=_usage(), finish_reason="stop")


def _tool(call_id: str, code: str) -> ChatResult:
    return ChatResult(
        content="",
        usage=_usage(),
        finish_reason="tool_calls",
        tool_calls=[
            ToolCall(id=call_id, name="execute_code", arguments_json=f'{{"code": "{code}"}}')
        ],
    )


FINAL_OK = '{"results": [{"name": "slope", "value": 1.985}]}'
FINAL_NOT_JSON = "这不是 JSON"
FINAL_NAN = '{"results": [{"name": "x", "value": NaN}]}'
FAILED_RUN = ExecutionResult(exit_code=1, stdout="", stderr="NameError: boom", artifacts=[])


def _loop_trace(root: Path, scripts: list[ChatResult], runs: list[ExecutionResult], impl: str) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    sandbox = CountingSandbox(copy.deepcopy(runs))
    llm = ScriptedLLM(copy.deepcopy(scripts))
    loop = SolveLoop(
        llm=llm,
        registry=build_default_registry(sandbox),
        operations=OperationLog(str(root / f"ops-{impl}.db")),
        task_id="t-loop",
        work_root=root / impl,
        impl=impl,
    )
    outcome = asyncio.run(loop.run("求解"))
    return {"outcome": outcome, "roles": llm.observed, "codes": sandbox.codes}


LOOP_CASES: dict[str, tuple[list[ChatResult], list[ExecutionResult]]] = {
    "converge_after_tool": ([_tool("c1", "print(1)"), _final(FINAL_OK)], []),
    "repair_then_converge": (
        [_tool("c1", "bad"), _tool("c2", "good"), _final(FINAL_OK)],
        [FAILED_RUN],
    ),
    "invalid_json_repair": ([_final(FINAL_NOT_JSON), _final(FINAL_OK)], []),
    "nan_rejected": ([_final(FINAL_NAN), _final(FINAL_OK)], []),
    "budget_exhausted": (
        [_tool(f"c{i}", "boom") for i in range(6)],
        [copy.deepcopy(FAILED_RUN) for _ in range(6)],
    ),
}


@pytest.mark.parametrize("case", sorted(LOOP_CASES))
def test_solve_loop_parity(tmp_path: Path, case: str) -> None:
    scripts, runs = LOOP_CASES[case]
    left = _loop_trace(tmp_path / "langgraph", scripts, runs, IMPL_LANGGRAPH)
    right = _loop_trace(tmp_path / "native", scripts, runs, IMPL_NATIVE)

    assert right["outcome"] == left["outcome"], f"{case}: {left['outcome']} != {right['outcome']}"
    assert right["roles"] == left["roles"], f"{case}: 送给模型的消息角色序列不同"
    assert right["codes"] == left["codes"], f"{case}: 沙箱执行序列不同"
