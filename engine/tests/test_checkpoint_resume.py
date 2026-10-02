"""中断-恢复矩阵测试（SP1-2 验收标准）：各阶段崩溃后从最近检查点续跑，无重算。"""

import pytest

from engine.checkpoint.store import SQLiteCheckpointStore
from engine.orchestrator.graph import StageOrchestrator


async def _run_through(orch: StageOrchestrator, n_stages: int) -> None:
    """跑完前 n_stages 个阶段（每个阶段 run + pass 门禁）。"""
    for _ in range(n_stages):
        await orch.run_current_stage()
        await orch.answer_gate("pass")


@pytest.mark.parametrize(
    "crash_after_stages, expected_resume_stage",
    [
        (0, "analysis"),  # 崩溃于 analysis 开始前
        (1, "modeling"),  # analysis 完成后崩溃
        (2, "solving"),  # modeling 完成后崩溃
        (3, "writing"),  # solving 完成后崩溃
    ],
)
async def test_crash_resume_matrix(tmp_path, crash_after_stages, expected_resume_stage) -> None:
    """崩溃-恢复矩阵：跑完 N 阶段后崩溃，恢复应定位到第 N+1 阶段。"""
    db = str(tmp_path / "ckpt.db")
    store = SQLiteCheckpointStore(db)

    orch = StageOrchestrator("t1", checkpoint=store)
    await _run_through(orch, crash_after_stages)

    # 模拟崩溃：丢弃 orch，从检查点恢复
    restored = StageOrchestrator.restore("t1", store)
    assert restored.current_stage == expected_resume_stage
    # 前序阶段已恢复，无重算
    assert len(restored.state.stages) == crash_after_stages
    store.close()


async def test_crash_after_gate_reject_retries_same_stage(tmp_path) -> None:
    """门禁驳回后崩溃：恢复后仍在当前阶段重跑。"""
    db = str(tmp_path / "ckpt.db")
    store = SQLiteCheckpointStore(db)

    orch = StageOrchestrator("t1", checkpoint=store)
    await orch.run_current_stage()  # analysis done
    await orch.answer_gate("reject")  # 驳回，analysis 结果被清除

    # 崩溃后恢复：analysis 结果已清除，仍在 analysis
    restored = StageOrchestrator.restore("t1", store)
    assert restored.current_stage == "analysis"
    assert "analysis" not in restored.state.stages  # 被清除待重跑
    store.close()


async def test_crash_after_final_stage(tmp_path) -> None:
    """最后阶段完成后崩溃：恢复后仍是 writing（最后阶段）。"""
    db = str(tmp_path / "ckpt.db")
    store = SQLiteCheckpointStore(db)

    orch = StageOrchestrator("t1", checkpoint=store)
    await _run_through(orch, 4)  # 跑完四阶段（writing 也完成并 pass）

    restored = StageOrchestrator.restore("t1", store)
    assert restored.current_stage == "writing"
    assert len(restored.state.stages) == 4  # 四阶段全部恢复
    store.close()
