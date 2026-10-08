"""LangGraph 编排器测试（SP1-2 真实落地验收补充）。

覆盖：图结构（4 阶段 + 4 门禁 + finish）、interrupt 挂起语义、
Command 人工干预（pass/reject）、阶段顺序禁止跳级、runner 注入、
检查点敏感键脱敏（不落 Key 明文）、挂起幂等、SP1-3 硬检查自动裁决。
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
        return {
            "insights": ["决策变量与目标"],
            "result": {"api_key": "sk-xxxx", "safe": "ok", "nested": {"secret_token": "t", "keep": 1}},
        }

    db = str(tmp_path / "ckpt.db")
    store = SQLiteCheckpointStore(db)
    orch = StageOrchestrator("t-redact", checkpoint=store, runner=runner)
    await orch.run_current_stage()
    record = store.load_stage("t-redact", "analysis")
    assert record is not None
    assert record.data == {
        "insights": ["决策变量与目标"],
        "result": {"safe": "ok", "nested": {"keep": 1}},
    }  # 敏感键被剔除
    assert "sk-xxxx" not in str(record.data)
    store.close()


# ----------------------------------------------------------------------
# SP1-3 硬检查裁决：违规自动 reject（不谎报"等待人工门禁"），合规仍挂人工门禁
# ----------------------------------------------------------------------
async def test_hard_check_violation_auto_rejects_and_reruns_stage(tmp_path) -> None:
    """违规产出经 answer_gate("reject") 消费本次门禁：不挂人工门禁、不清 checkpoint 之外语义。"""
    calls: list[str] = []

    async def bad_runner(task_id: str, stage: str) -> dict:
        calls.append(stage)
        return {"stage": stage, "insights": ["  "]}

    db = str(tmp_path / "ckpt.db")
    store = SQLiteCheckpointStore(db)
    orch = StageOrchestrator("t-hard", checkpoint=store, runner=bad_runner)
    data = await orch.run_current_stage()

    assert data["insights"] == ["  "]  # 返回本次产出供上层展示
    assert orch.last_hard_reject is not None
    assert orch.last_hard_reject.startswith("硬检查不通过")
    assert "insights" in orch.last_hard_reject
    assert orch.current_stage == "analysis"  # 不推进
    assert orch.state.gate_decision == "reject"
    assert "analysis" not in orch.state.stages  # 违规结果被清除待重跑
    assert store.load_stage("t-hard", "analysis") is None  # 违规产出不落检查点
    assert orch.graph().get_state({"configurable": {"thread_id": "t-hard"}}).next == ()

    await orch.run_current_stage()  # 再次执行 = 重跑当前阶段（reject 既有语义）
    assert calls == ["analysis", "analysis"]
    store.close()


async def test_hard_check_compliant_output_still_waits_for_human_gate() -> None:
    """合规产出：硬检查放行，门禁挂起语义（interrupt + 幂等返回）不回退。"""
    calls: list[str] = []

    async def good_runner(task_id: str, stage: str) -> dict:
        calls.append(stage)
        return {"stage": stage, "insights": ["决策变量与目标"], "usage": {"prompt_tokens": 5}}

    orch = StageOrchestrator("t-hard-ok", runner=good_runner)
    data = await orch.run_current_stage()
    assert orch.last_hard_reject is None
    assert data["insights"] == ["决策变量与目标"]
    assert orch.state.stages["analysis"].data == data
    graph_state = orch.graph().get_state({"configurable": {"thread_id": "t-hard-ok"}})
    assert graph_state.next == ("gate_analysis",)  # 仍在人工门禁挂起

    await orch.run_current_stage()  # 挂起未决：幂等返回，不重跑阶段
    assert calls == ["analysis"]
    assert (await orch.answer_gate("pass"))["action"] == "next_stage"


async def test_hard_check_cleared_on_next_compliant_run() -> None:
    """先违规后合规：重跑产出合规时 last_hard_reject 归 None 并重新挂起门禁。"""
    outputs: list[dict] = [{"insights": []}, {"insights": ["约束条件"]}]

    async def flaky_runner(task_id: str, stage: str) -> dict:
        return outputs.pop(0)

    orch = StageOrchestrator("t-hard-fix", runner=flaky_runner)
    await orch.run_current_stage()
    assert orch.last_hard_reject is not None
    await orch.run_current_stage()
    assert orch.last_hard_reject is None
    assert "analysis" in orch.state.stages


def test_hard_check_skipped_in_skeleton_mode() -> None:
    """骨架模式（默认 runner）不判硬检查：与 answer_gate 的骨架/真实模式分野一致（SP1-1 兼容）。"""
    assert StageOrchestrator("t-skeleton").last_hard_reject is None


def test_redact_sensitive_covers_lists_and_scalars() -> None:
    payload = {"a": 1, "api_key": "x", "list": [{"token": "y", "ok": 2}], "s": "keep"}
    assert redact_sensitive(payload) == {"a": 1, "list": [{"ok": 2}], "s": "keep"}
    assert redact_sensitive([1, {"key": "z"}]) == [1, {}]
    assert redact_sensitive("plain") == "plain"
    # 密钥域红线扩展：authorization（含 Bearer 头值）同样被剔除
    assert redact_sensitive({"authorization": "Bearer eyJ-sig", "ok": 1, "nested": [{"API_Key": "x"}]}) == {
        "ok": 1,
        "nested": [{}],
    }