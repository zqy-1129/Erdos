"""四阶段编排器单元测试（SP1-2）：顺序约束 + 门禁 + 断点续跑。"""

import pytest

from engine.checkpoint.store import SQLiteCheckpointStore
from engine.orchestrator.graph import STAGES, StageOrchestrator


async def test_fixed_stage_order() -> None:
    """四阶段固定顺序：分析→建模→求解→报告。"""
    assert STAGES == ("analysis", "modeling", "solving", "writing")
    orch = StageOrchestrator("t1")
    assert orch.current_stage == "analysis"
    await orch.run_current_stage()
    r = await orch.answer_gate("pass")
    assert r["action"] == "next_stage"
    assert orch.current_stage == "modeling"


async def test_gate_required_before_advance() -> None:
    """禁止跳级：未过门禁不能进入下一阶段。"""
    orch = StageOrchestrator("t1")
    await orch.run_current_stage()  # analysis 完成，进入 gate_waiting
    # 不 answer_gate 直接想 advance 是禁止的（编排器状态仍 gate_waiting）
    assert orch.state.gate_decision is None
    # 只有 answer_gate(pass) 才推进
    await orch.answer_gate("pass")
    assert orch.current_stage == "modeling"


async def test_gate_reject_retries_current_stage() -> None:
    """门禁驳回：当前阶段重跑，前序阶段不受影响。"""
    orch = StageOrchestrator("t1")
    await orch.run_current_stage()  # analysis done
    await orch.answer_gate("pass")  # → modeling
    await orch.run_current_stage()  # modeling done
    r = await orch.answer_gate("reject")  # 驳回 modeling
    assert r["action"] == "retry_stage"
    assert orch.current_stage == "modeling"  # 仍在 modeling
    assert "analysis" in orch.state.stages  # 前序 analysis 不受影响
    assert "modeling" not in orch.state.stages  # modeling 结果被清除待重跑


async def test_full_run_completes_after_final_gate() -> None:
    """完整跑完四阶段：最后门禁 pass 后任务完成。"""
    orch = StageOrchestrator("t1")
    for _ in range(4):
        await orch.run_current_stage()
        r = await orch.answer_gate("pass")
    assert r["action"] == "complete"
    assert len(orch.state.stages) == 4


async def test_invalid_gate_decision_rejected() -> None:
    """非法门禁决策抛错。"""
    orch = StageOrchestrator("t1")
    await orch.run_current_stage()
    with pytest.raises(ValueError):
        await orch.answer_gate("bogus")


async def test_checkpoint_restore_from_recent(tmp_path) -> None:
    """断点续跑：从最近检查点恢复，不重算已完成阶段。"""
    db = str(tmp_path / "ckpt.db")
    store = SQLiteCheckpointStore(db)

    # 第一轮：跑完 analysis 和 modeling（存检查点），solving 崩溃前
    orch = StageOrchestrator("t1", checkpoint=store)
    await orch.run_current_stage()
    await orch.answer_gate("pass")  # → modeling
    await orch.run_current_stage()  # modeling done（检查点已存）

    # 模拟崩溃：重启后从检查点恢复
    restored = StageOrchestrator.restore("t1", store)
    # 恢复后应位于 modeling 之后（即下一个门禁等待），前序 analysis 完成
    assert "analysis" in restored.state.stages
    assert "modeling" in restored.state.stages
    assert restored.current_stage == "solving"  # modeling 完成后的下一阶段

    store.close()
