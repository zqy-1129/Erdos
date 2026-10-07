"""中断-恢复矩阵测试（SP1-2 验收标准）：各阶段崩溃后从最近检查点续跑，无重算。"""

import pytest

from engine.checkpoint.store import SQLiteCheckpointStore
from engine.orchestrator.graph import StageOrchestrator
from engine.orchestrator.pipeline import StagePipeline
from engine.sandbox.subprocess_sandbox import SubprocessSandbox


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


async def test_crash_in_gate_pending_window_rehangs_gate(tmp_path) -> None:
    """门禁未决窗口崩溃：恢复定位到该阶段，重挂门禁且不重放阶段副作用（DEC-005）。

    检查点两态语义（SP1-7）：stage_done=执行完成门禁未决，done=门禁通过；
    未经门禁的阶段恢复后必须重新过门禁，不得静默推进。
    """
    db = str(tmp_path / "ckpt.db")
    store = SQLiteCheckpointStore(db)
    calls: list[str] = []

    async def runner(task_id: str, stage: str) -> dict:
        calls.append(stage)
        return {"stage": stage, "usage": {"prompt_tokens": 5}}

    orch = StageOrchestrator("t1", checkpoint=store, runner=runner)
    await orch.run_current_stage()  # analysis 执行完成，门禁未决（未 answer_gate）

    restored = StageOrchestrator.restore("t1", store, runner=runner)
    assert restored.current_stage == "analysis"  # 定位到门禁未决阶段（而非跳到 modeling）

    data = await restored.run_current_stage()  # 重挂门禁：阶段不重放
    assert calls == ["analysis"]  # 副作用未重放
    assert data.get("stage") == "analysis"  # 返回检查点数据（usage 经严格脱敏剥离）

    action = await restored.answer_gate("pass")
    assert action["action"] == "next_stage"
    await _run_through(restored, 3)  # modeling/solving/writing 正常续跑
    assert calls == ["analysis", "modeling", "solving", "writing"]

    records = store.completed_stages("t1")
    assert all(r.status == "done" for r in records)  # 门禁通过状态全部升级落库
    store.close()


async def test_restore_forwards_runner_to_remaining_stages(tmp_path) -> None:
    """恢复必须保留 runner：否则剩余阶段静默退化为骨架空产出（SP1-7 恢复演练语义）。"""
    db = str(tmp_path / "ckpt.db")
    store = SQLiteCheckpointStore(db)
    calls: list[str] = []

    async def runner(task_id: str, stage: str) -> dict:
        calls.append(stage)
        return {"stage": stage}

    orch = StageOrchestrator("t1", checkpoint=store, runner=runner)
    await _run_through(orch, 2)  # analysis + modeling 完成

    restored = StageOrchestrator.restore("t1", store, runner=runner)
    assert restored.current_stage == "solving"
    await _run_through(restored, 2)  # solving + writing 续跑
    assert calls == ["analysis", "modeling", "solving", "writing"]  # 剩余阶段真实执行
    store.close()


async def test_pipeline_resume_hydrates_history_from_checkpoint(tmp_path) -> None:
    """恢复后管线经 state_loader 水合跨阶段上下文（DEC-005）。

    水合失败时 writing 会输出「分析要点：（无）」；水合成功则论文引用前序
    analysis 的真实产出（FakeLLM 确定性文本含「决策变量」）。
    """
    db = str(tmp_path / "ckpt.db")
    store = SQLiteCheckpointStore(db)
    task_inputs = {"t1": {"title": "恢复演练", "problem_text": "题面文本"}}

    first = StagePipeline(
        sandbox=SubprocessSandbox(timeout=30), work_root=tmp_path / "work",
        task_inputs=task_inputs,
    )
    orch = StageOrchestrator("t1", checkpoint=store, runner=first.process)
    await orch.run_current_stage()  # analysis 完成（落检查点）
    await orch.answer_gate("pass")

    def loader(task_id: str) -> dict:
        return {r.stage: r.data for r in store.completed_stages(task_id)}

    resumed = StagePipeline(
        sandbox=SubprocessSandbox(timeout=30), work_root=tmp_path / "work",
        task_inputs=task_inputs, state_loader=loader,
    )
    restored = StageOrchestrator.restore("t1", store, runner=resumed.process)
    final: dict = {}
    for _ in range(3):  # modeling / solving / writing 续跑
        final = await restored.run_current_stage()
        await restored.answer_gate("pass")

    assert "决策变量" in final["paper_md"]  # 前序分析真实进入论文
    assert "分析要点：（无）" not in final["paper_md"]
    store.close()
