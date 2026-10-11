"""跨实现持久化兼容（回滚可行性的机器证明）。

为什么单独测这一条：灰度期两个后端会写同一张 `checkpoints` 表，而"任意阶段可回滚"
只有在**老引擎能读新引擎写的库、新引擎也能读老引擎写的库**时才成立——表结构没变
不等于恢复语义没变（两态 status、恢复定位、门禁未决不重放都是行为）。这条不成立，
回滚预案就是空的。

用真实 SQLite 而不是内存替身：替身会跟着实现一起被写错，真库才反映落盘事实。
桩产出必须过 SP1-3 硬检查（论文四章节 + paper_path 真实存在 + 64 位十六进制哈希），
否则测的其实是"违规驳回"路径——这个坑在 `test_orchestrator_parity.py` 里踩过一次。
"""

import hashlib
from pathlib import Path
from typing import Any

import pytest

from engine.checkpoint.store import SQLiteCheckpointStore
from engine.orchestrator.backend import IMPL_LANGGRAPH, IMPL_NATIVE
from engine.orchestrator.graph import STAGES, StageOrchestrator

IMPLS = (IMPL_LANGGRAPH, IMPL_NATIVE)
TASK_ID = "compat"

_PAPER = (
    "# 桩论文\n\n## 摘要\n\n方法与结果。\n\n## 一、问题重述\n\n题面。\n\n"
    "## 二、模型假设与建模\n\n假设。\n\n## 三、求解与结果\n\nslope=1.9850\n\n## 四、结论\n\n结论。\n"
)


def _contract_output(stage: str, work_dir: Path) -> dict[str, Any]:
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
    paper_path = work_dir / "paper.md"
    paper_path.parent.mkdir(parents=True, exist_ok=True)
    paper_path.write_text(_PAPER, encoding="utf-8")
    return {
        "stage": stage,
        "paper_md": _PAPER,
        "paper_sha256": hashlib.sha256(_PAPER.encode("utf-8")).hexdigest(),
        "paper_path": str(paper_path),
        "usage": {"prompt_tokens": 5},
    }


def _make_runner(work_dir: Path) -> tuple[Any, list[str]]:
    calls: list[str] = []

    async def run(task_id: str, stage: str) -> dict:
        calls.append(stage)
        return _contract_output(stage, work_dir)

    return run, calls


async def _run_and_pass(orch: StageOrchestrator, times: int) -> list[str]:
    actions: list[str] = []
    for _ in range(times):
        await orch.run_current_stage()
        actions.append((await orch.answer_gate("pass"))["action"])
    return actions


# ----------------------------------------------------------------------
@pytest.mark.parametrize("writer", IMPLS)
@pytest.mark.parametrize("reader", IMPLS)
@pytest.mark.parametrize("done_stages", [0, 1, 2, 3])
async def test_restore_locates_identically_across_implementations(
    tmp_path: Path, writer: str, reader: str, done_stages: int
) -> None:
    """writer 落库 → 换连接 → reader 恢复：定位、已审阶段集合、续跑收尾都必须一致。"""
    db = tmp_path / "ckpt.db"
    store = SQLiteCheckpointStore(str(db))
    write_runner, _ = _make_runner(tmp_path)
    orch = StageOrchestrator(TASK_ID, checkpoint=store, runner=write_runner, impl=writer)
    await _run_and_pass(orch, done_stages)
    store.close()

    read_store = SQLiteCheckpointStore(str(db))
    read_runner, read_calls = _make_runner(tmp_path)
    restored = StageOrchestrator.restore(TASK_ID, read_store, runner=read_runner, impl=reader)

    assert restored.current_stage == STAGES[done_stages]
    assert sorted(restored.state.stages) == sorted(STAGES[:done_stages])

    actions = await _run_and_pass(restored, len(STAGES) - done_stages)
    assert actions[-1] == "complete"
    assert read_calls == list(STAGES[done_stages:])  # 已完成阶段不重算
    records = read_store.completed_stages(TASK_ID)
    assert [r.stage for r in records] == list(STAGES)
    assert all(r.status == "done" for r in records)
    read_store.close()


@pytest.mark.parametrize("writer", IMPLS)
@pytest.mark.parametrize("reader", IMPLS)
async def test_gate_pending_checkpoint_rehangs_gate_in_other_impl(
    tmp_path: Path, writer: str, reader: str
) -> None:
    """门禁未决（stage_done）崩溃点：另一套后端恢复后必须重挂门禁、不重放阶段副作用。"""
    db = tmp_path / "pending.db"
    store = SQLiteCheckpointStore(str(db))
    write_runner, _ = _make_runner(tmp_path)
    orch = StageOrchestrator(TASK_ID, checkpoint=store, runner=write_runner, impl=writer)
    await orch.run_current_stage()  # analysis 完成，未答门禁
    store.close()

    read_store = SQLiteCheckpointStore(str(db))
    read_runner, read_calls = _make_runner(tmp_path)
    restored = StageOrchestrator.restore(TASK_ID, read_store, runner=read_runner, impl=reader)
    assert restored.current_stage == "analysis"

    await restored.run_current_stage()
    assert read_calls == []  # 阶段未重放（DEC-005）
    state = restored.graph().get_state({"configurable": {"thread_id": TASK_ID}})
    assert state.next == ("gate_analysis",)  # 门禁重挂，而不是静默推进
    assert (await restored.answer_gate("pass"))["action"] == "next_stage"
    read_store.close()


@pytest.mark.parametrize("impl", IMPLS)
async def test_checkpoint_payload_matches_reference_shape(tmp_path: Path, impl: str) -> None:
    """两后端落库的载荷必须等于同一份参照值（不是"两边互相一致就行"）。

    互相一致但都偏离《数据模型设计》的 checkpoint_data 语义，回滚到任意历史版本都会读错。
    """
    db = tmp_path / f"rows-{impl}.db"
    store = SQLiteCheckpointStore(str(db))
    runner, _ = _make_runner(tmp_path)
    orch = StageOrchestrator(TASK_ID, checkpoint=store, runner=runner, impl=impl)
    await _run_and_pass(orch, 2)

    rows = {r.stage: (r.status, r.step, r.data) for r in store.completed_stages(TASK_ID)}
    assert sorted(rows) == ["analysis", "modeling"]
    assert rows["analysis"][0] == "done" and rows["analysis"][1] == 0
    assert rows["analysis"][2]["insights"] == ["决策变量与目标", "约束条件"]
    assert rows["modeling"][2]["variables"] == ["slope"]
    store.close()


@pytest.mark.parametrize("impl", IMPLS)
async def test_sensitive_keys_never_reach_the_checkpoint_table(tmp_path: Path, impl: str) -> None:
    """密钥域红线与实现无关：脱敏发生在写库前，两套后端都必须已经剥掉敏感键。"""
    db = tmp_path / f"redact-{impl}.db"
    store = SQLiteCheckpointStore(str(db))

    async def leaky_runner(task_id: str, stage: str) -> dict:
        return {
            "stage": stage,
            "insights": ["决策变量与目标"],
            "api_key": "sk-plain",
            "notes": {"authorization": "Bearer eyJ-sig", "keep": 1},
        }

    orch = StageOrchestrator(TASK_ID, checkpoint=store, runner=leaky_runner, impl=impl)
    await orch.run_current_stage()
    record = store.load_stage(TASK_ID, "analysis")
    assert record is not None
    assert record.data == {"stage": "analysis", "insights": ["决策变量与目标"], "notes": {"keep": 1}}
    assert "sk-plain" not in str(record.data)
    store.close()
