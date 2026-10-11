"""后端选择层守护：默认值、显式覆盖、两拓扑等价、非法值。

默认值翻到 native 是 P5 的动作，这条用例把"当前默认是谁"钉成显式断言——
以后有人无意中改回去或改错，这里会红，而不是靠读代码猜。
"""

import pytest

from engine.orchestrator import backend
from engine.orchestrator.backend import (
    IMPL_ENV,
    IMPL_LANGGRAPH,
    IMPL_NATIVE,
    VALID_IMPLS,
    orchestration_impl,
)
from engine.orchestrator.graph import STAGES, StageOrchestrator, gate_node_name


def test_default_implementation_is_native_since_p5(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(IMPL_ENV, raising=False)
    assert orchestration_impl() == IMPL_NATIVE


@pytest.mark.parametrize("impl", VALID_IMPLS)
def test_env_var_overrides_backend(
    monkeypatch: pytest.MonkeyPatch, impl: str
) -> None:
    monkeypatch.setenv(IMPL_ENV, impl)
    assert orchestration_impl() == impl


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """拼错回退值不能静默落到某个默认实现——那会让"我已经回滚了"变成假事实。"""
    monkeypatch.setenv(IMPL_ENV, "LangGraph-ish")
    with pytest.raises(ValueError, match="ERDOS_ORCH_IMPL"):
        orchestration_impl()


def test_blank_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(IMPL_ENV, "   ")
    assert orchestration_impl() == IMPL_NATIVE


@pytest.mark.parametrize("impl", VALID_IMPLS)
async def test_both_backends_expose_the_same_topology(impl: str) -> None:
    """两套后端的节点集必须逐字相同：改了其中一张图而忘了另一张，这里先红。"""
    orch = StageOrchestrator("t-topo", impl=impl)
    nodes = {name for name in orch.graph().get_graph().nodes if not name.startswith("__")}
    assert nodes == {*STAGES, *(gate_node_name(s) for s in STAGES), "finish"}


async def test_backends_agree_on_node_set() -> None:
    def names(impl: str) -> set[str]:
        orch = StageOrchestrator("t-agree", impl=impl)
        return {n for n in orch.graph().get_graph().nodes if not n.startswith("__")}

    assert names(IMPL_LANGGRAPH) == names(IMPL_NATIVE)


@pytest.mark.parametrize("impl", VALID_IMPLS)
async def test_compiled_graph_call_surface_is_uniform(impl: str) -> None:
    """调用面（run/resume/raw）两套后端都得有，且语义一致——后端分派靠的就是这三个方法。"""
    orch = StageOrchestrator("t-surface", impl=impl)
    graph = orch._backend  # noqa: SLF001 - 本用例测的就是这一层的契约
    for attr in ("run", "resume", "raw"):
        assert callable(getattr(graph, attr)), attr

    config = {"configurable": {"thread_id": "t-surface"}}
    await graph.run({"task_id": "t-surface", "stage_index": 0, "results": {}})
    assert graph.raw().get_state(config).next == (gate_node_name(STAGES[0]),)
    await graph.resume({"decision": "pass", "feedback": ""})
    assert graph.raw().get_state(config).next == ()  # 已到 END，无残留挂起


@pytest.mark.parametrize("impl", VALID_IMPLS)
async def test_resume_advances_under_each_backend(impl: str) -> None:
    orch = StageOrchestrator("t-advance", impl=impl)
    await orch.run_current_stage()
    assert (await orch.answer_gate("pass"))["action"] == "next_stage"
    assert orch.current_stage == STAGES[1]


def test_module_exports_both_impl_names() -> None:
    assert backend.IMPL_LANGGRAPH != backend.IMPL_NATIVE
    assert set(VALID_IMPLS) == {"langgraph", "native"}
