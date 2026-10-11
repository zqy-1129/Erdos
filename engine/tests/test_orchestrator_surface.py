"""对外契约面守护：编排器与求解循环的公开签名、返回形状、RPC 方法集。

替换编排框架时最容易失守的不是"跑不通"，而是**签名静默变了**——调用方按老签名写，
参数被忽略或多出一个必填项，问题要到联调才暴露。所以这一层用字面量把签名钉死，
期望值直接写在测试里（不是从代码反推），改签名必须同时改这里。

同时钉：`engine.orchestrator` 包再导出的公开名、get_status 用的状态快照键集、
RPC 注册的方法集与 `contracts/engine-rpc.schema.json` 一致。
"""

import inspect
import json
import re
from collections.abc import Callable
from pathlib import Path

from engine.orchestrator import (
    STAGES,
    OrchestratorState,
    StageOrchestrator,
    StageResult,
    StageStatus,
)
from engine.orchestrator.graph import CHECKPOINT_EXECUTED, CHECKPOINT_GATE_PASSED, gate_node_name
from engine.orchestrator.solve_loop import SolveLoop

REPO_ROOT = Path(__file__).resolve().parents[2]


def _params(fn: Callable[..., object]) -> list[str]:
    return list(inspect.signature(fn).parameters)


def test_stage_orchestrator_public_surface() -> None:
    assert _params(StageOrchestrator.__init__) == [
        "self", "task_id", "checkpoint", "runner", "impl",
    ]
    assert _params(StageOrchestrator.run_current_stage) == ["self"]
    assert _params(StageOrchestrator.answer_gate) == ["self", "decision", "feedback"]
    assert _params(StageOrchestrator.restore) == ["task_id", "checkpoint", "runner", "impl"]
    assert _params(StageOrchestrator.is_final_stage) == ["self"]
    assert _params(StageOrchestrator.graph) == ["self"]
    assert inspect.iscoroutinefunction(StageOrchestrator.run_current_stage)
    assert inspect.iscoroutinefunction(StageOrchestrator.answer_gate)
    assert isinstance(inspect.getattr_static(StageOrchestrator, "restore"), classmethod)
    # 向后兼容：checkpoint/runner 是既有可选参数，impl 是本轮新增的可选参数——
    # 老调用方的单参/双参构造都不能坏。
    optional = {
        name: p.default
        for name, p in inspect.signature(StageOrchestrator.__init__).parameters.items()
        if p.default is not inspect.Parameter.empty
    }
    assert optional == {"checkpoint": None, "runner": None, "impl": None}
    assert (
        inspect.signature(StageOrchestrator.restore).parameters["impl"].default is None
    )


def test_answer_gate_feedback_default_is_empty_string() -> None:
    signature = inspect.signature(StageOrchestrator.answer_gate)
    assert signature.parameters["feedback"].default == ""
    assert signature.parameters["decision"].default is inspect.Parameter.empty


def test_hard_reject_reason_attribute_exists() -> None:
    """`last_hard_reject` 是 methods.py 判定 gate.failed 的唯一依据，不能改名或私有化。"""
    orch = StageOrchestrator("t-surface")
    assert hasattr(orch, "last_hard_reject")
    assert orch.last_hard_reject is None
    assert orch.current_stage == STAGES[0]
    assert orch.is_final_stage() is False


def test_package_reexports_are_stable() -> None:
    """调用方 import 路径（12 处）依赖这些名字，缺一个就是装配期 ImportError。"""
    assert STAGES == ("analysis", "modeling", "solving", "writing")
    assert StageStatus.DONE.value == "done"
    assert CHECKPOINT_EXECUTED == "stage_done" and CHECKPOINT_GATE_PASSED == "done"
    assert [gate_node_name(s) for s in STAGES] == [
        "gate_analysis", "gate_modeling", "gate_solving", "gate_writing"
    ]
    result = StageResult(stage="analysis", status="done", step=0, data={})
    assert (result.stage, result.status, result.step, result.data) == ("analysis", "done", 0, {})


def test_status_snapshot_keys_for_get_status() -> None:
    """`snapshot()` 的键集经 get_status 出进程边界，客户端与文档都按这份键读。"""
    snap = OrchestratorState(task_id="t1").snapshot()
    assert sorted(snap) == [
        "current_stage", "gate_decision", "stage_index", "stages", "task_id",
    ]
    assert snap["stages"] == {}


def test_solve_loop_surface() -> None:
    assert _params(SolveLoop.__init__) == [
        "self", "llm", "registry", "operations", "task_id", "work_root",
        "attempt", "max_repairs", "max_dispatches", "delta_sink", "impl",
    ]
    assert _params(SolveLoop.run) == ["self", "task_prompt"]
    signature = inspect.signature(SolveLoop.__init__)
    assert signature.parameters["attempt"].default == 1
    assert signature.parameters["max_repairs"].default == 3
    assert signature.parameters["max_dispatches"].default == 8
    # 关键字限定：task_id 及之后必须显式传参（防位置参数误传）
    assert signature.parameters["task_id"].kind is inspect.Parameter.KEYWORD_ONLY


def test_rpc_methods_match_contract() -> None:
    """引擎注册的方法集必须与契约声明一致——多一个少一个都是跨端漂移。"""
    schema = json.loads(
        (REPO_ROOT / "contracts" / "engine-rpc.schema.json").read_text(encoding="utf-8")
    )
    contract_methods = set(schema["methods"])
    source = (REPO_ROOT / "engine" / "ipc" / "methods.py").read_text(encoding="utf-8")
    registered = {line.split('"')[1] for line in source.splitlines() if 'server.register("' in line}
    assert registered == contract_methods, registered ^ contract_methods


def test_rpc_events_match_contract_emission_points() -> None:
    """契约声明的 6 个事件在代码里都要有发射点，且代码不得发射契约外的事件名。

    注意这只证明"有发射语句"，不证明"该发的时候真发了"——`artifact.ready` 只有论文一处
    发射点、figure/data/table/code 是死枚举，属另一类缺口（08 §14.2 G12），不在本判据内。
    """
    schema = json.loads(
        (REPO_ROOT / "contracts" / "engine-rpc.schema.json").read_text(encoding="utf-8")
    )
    contract_events = set(schema["events"])
    found: set[str] = set()
    for path in (REPO_ROOT / "engine").rglob("*.py"):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        found |= set(re.findall(r'\.emit\(\s*"([a-z.]+)"', path.read_text(encoding="utf-8")))
    assert found == contract_events, f"契约与代码发射点差异: {found ^ contract_events}"
