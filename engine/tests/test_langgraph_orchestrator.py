"""LangGraph 编排器测试（SP1-2 真实落地验收补充）。

覆盖：图结构（4 阶段 + 4 门禁 + finish）、interrupt 挂起语义、
Command 人工干预（pass/reject）、阶段顺序禁止跳级、runner 注入、
检查点敏感键脱敏（不落 Key 明文）、挂起幂等。
中断-恢复矩阵（SQLite 检查点）见 test_checkpoint_resume.py。
"""

import pytest

from engine.checkpoint.store import SQLiteCheckpointStore
from engine.orchestrator.graph import STAGES, StageOrchestrator, redact_sensitive


async def test_graph_has_four_stage_and_gate_nodes() -> None:
    orch = StageOrchestrator("t-struct")
    nodes = {node for node in orch.graph().get_graph().nodes if node not in ("__start__", "__end__")}
    assert nodes == {*STAGES, *[f"gate_{s}" for s in STAGES], "finish"}


async def test_run_suspends_at_gate_with_interrupt() -> None:
    orch = StageOrchestrator("t-interrupt")
    await orch.run_current_stage()
    graph_state = orch.graph().get_state({"configurable": {"thread_id": "t-interrupt"}})
    assert graph_state.next == ("gate_analysis",)
    assert len(graph_state.tasks) == 1  # 挂起的 interrupt 任务存在（LangGraph 1.x: tasks）
    # 挂起未决时重复 run 幂等（不重跑节点）
    await orch.run_current_stage()
    assert len(orch.state.stages) == 1


async def test_answer_gate_pass_advances_without_running_next() -> None:
    orch = StageOrchestrator("t-pass")
    await orch.run_current_stage()
    action = await orch.answer_gate("pass", feedback="ok")
    assert action == {"action": "next_stage", "stage": "modeling"}
    assert orch.current_stage == "modeling"
    assert len(orch.state.stages) == 1  # 下一阶段未自动执行（与骨架语义一致）


async def test_answer_gate_reject_clears_current_and_reruns() -> None:
    orch = StageOrchestrator("t-reject")
    await orch.run_current_stage()
    action = await orch.answer_gate("reject", feedback="假设与数据脱节")
    assert action == {"action": "retry_stage", "stage": "analysis"}
    assert orch.current_stage == "analysis"
    assert "analysis" not in orch.state.stages  # 被清除待重跑


async def test_fourth_stage_pass_completes() -> None:
    orch = StageOrchestrator("t-complete")
    for _ in STAGES:
        await orch.run_current_stage()
        action = await orch.answer_gate("pass")
    assert action == {"action": "complete", "stage": "writing"}
    assert len(orch.state.stages) == 4


async def test_stage_order_cannot_be_jumped() -> None:
    orch = StageOrchestrator("t-order")
    # 人为把图状态拨到非法位置：路由必须拒绝跳级（index 越界即异常）
    with pytest.raises(RuntimeError):
        await orch.graph().ainvoke(
            {"task_id": "t-order", "stage_index": 99, "results": {}},
            {"configurable": {"thread_id": "t-order-bad"}},
        )


async def test_runner_injected_artifact_flows_into_state() -> None:
    async def runner(task_id: str, stage: str) -> dict:
        return {"artifacts": [f"{stage}-out.md"], "api_key": "sk-plain", "notes": {"api_key": "sk2"}}

    orch = StageOrchestrator("t-runner", runner=runner)
    data = await orch.run_current_stage()
    assert data == {"artifacts": ["analysis-out.md"], "api_key": "sk-plain", "notes": {"api_key": "sk2"}}


async def test_checkpoint_redacts_sensitive_keys(tmp_path) -> None:
    async def runner(task_id: str, stage: str) -> dict:
        return {"result": {"api_key": "sk-xxxx", "safe": "ok", "nested": {"secret_token": "t", "keep": 1}}}

    db = str(tmp_path / "ckpt.db")
    store = SQLiteCheckpointStore(db)
    orch = StageOrchestrator("t-redact", checkpoint=store, runner=runner)
    await orch.run_current_stage()
    record = store.load_stage("t-redact", "analysis")
    assert record is not None
    assert record.data == {"result": {"safe": "ok", "nested": {"keep": 1}}}  # 敏感键被剔除
    assert "sk-xxxx" not in str(record.data)
    store.close()


def test_redact_sensitive_covers_lists_and_scalars() -> None:
    payload = {"a": 1, "api_key": "x", "list": [{"token": "y", "ok": 2}], "s": "keep"}
    assert redact_sensitive(payload) == {"a": 1, "list": [{"ok": 2}], "s": "keep"}
    assert redact_sensitive([1, {"key": "z"}]) == [1, {}]
    assert redact_sensitive("plain") == "plain"