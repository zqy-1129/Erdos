"""检查点状态字面量的跨面守护（P0 数据正确性）。

缺陷形状：恢复定位时把"未识别的状态值"当作"门禁已通过"直接推进下一阶段。这类错误不抛异常、
不丢数据，只是让一个从未过门禁的阶段被跳过并落成 done —— 与本项目反复出现的
"看起来跑通了但语义不同"同形。且客户端 `main/local-history.ts` 的默认方向相反
（非 done 即未完成），同一份库两侧读出相反结论。

这里锁四件事：
1. 字面量合法集只有一个来源（存储层）；
2. 写入侧拒绝第三个值；
3. 读取侧（编排恢复 + 回归驱动）遇未知值一律拒绝推进，保留现场；
4. 跨端不出现第二份字面量定义（客户端只比对 "done"，语义与引擎一致：未知=未过门禁）。
"""

import ast
import sqlite3
from pathlib import Path

import pytest

from engine.checkpoint.store import (
    CHECKPOINT_STATUSES,
    SQLiteCheckpointStore,
    gate_passed,
)
from engine.orchestrator.graph import (
    CHECKPOINT_EXECUTED,
    CHECKPOINT_GATE_PASSED,
    StageOrchestrator,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ----------------------------------------------------------------------
# 单源与判定函数
# ----------------------------------------------------------------------
def test_literals_have_one_owner() -> None:
    assert CHECKPOINT_EXECUTED == "stage_done"
    assert CHECKPOINT_GATE_PASSED == "done"
    assert set(CHECKPOINT_STATUSES) == {CHECKPOINT_EXECUTED, CHECKPOINT_GATE_PASSED}


@pytest.mark.parametrize(
    ("status", "expected"),
    [(CHECKPOINT_GATE_PASSED, True), (CHECKPOINT_EXECUTED, False)],
)
def test_gate_passed_maps_the_two_states(status: str, expected: bool) -> None:
    assert gate_passed(status) is expected


@pytest.mark.parametrize("status", ["", "DONE", "done ", "passed", None, "gate_passed"])
def test_gate_passed_rejects_unknown(status: object) -> None:
    """未知值必须抛错。返回 True 会跳阶段，返回 False 会重挂门禁——两者都不如报错。"""
    with pytest.raises(ValueError, match="字面量未知"):
        gate_passed(status)  # type: ignore[arg-type]


def test_write_rejects_third_status(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(str(tmp_path / "ckpt.db"))
    with pytest.raises(ValueError, match="非法"):
        store.save_stage("t1", "analysis", "passed", 0, {})
    # 拒绝写入不留半行
    assert store.completed_stages("t1") == []
    store.close()


# ----------------------------------------------------------------------
# 恢复定位：未知值不推进
# ----------------------------------------------------------------------
def _inject_unknown_status(db: str, task_id: str, stage: str, status: str) -> None:
    """绕过存储层直接写一个非法状态值（模拟老库/旁路工具/人为损坏）。

    updated_at 取"晚于正常写入"的值：`completed_stages` 按 updated_at 排序，恢复定位看的是
    **最后落库那一条**，所以必须让它排在最后，否则测的是排序而不是状态判定。
    """
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT OR REPLACE INTO checkpoints (task_id, stage, status, step, data, updated_at)"
        " VALUES (?, ?, ?, 0, '{}', '2999-01-01T00:00:00+00:00')",
        (task_id, stage, status),
    )
    conn.commit()
    conn.close()


def test_restore_refuses_to_advance_on_unknown_status(tmp_path: Path) -> None:
    db = str(tmp_path / "ckpt.db")
    store = SQLiteCheckpointStore(db)
    store.save_stage("t1", "analysis", CHECKPOINT_GATE_PASSED, 0, {"insights": ["x"]})
    store.close()
    _inject_unknown_status(db, "t1", "modeling", "weird_state")

    with pytest.raises(ValueError, match="字面量未知"):
        StageOrchestrator.restore("t1", SQLiteCheckpointStore(db))


async def test_restore_still_resumes_normally_for_the_two_states(tmp_path: Path) -> None:
    """反向确认：守护没把正常恢复路径一起打死。"""
    db = str(tmp_path / "ckpt.db")
    store = SQLiteCheckpointStore(db)

    async def runner(task_id: str, stage: str) -> dict:
        return {"stage": stage, "insights": ["决策变量与目标"], "usage": {"prompt_tokens": 1}}

    orch = StageOrchestrator("t1", checkpoint=store, runner=runner)
    await orch.run_current_stage()  # analysis 执行完，门禁未决（stage_done）
    restored = StageOrchestrator.restore("t1", store, runner=runner)
    assert restored.current_stage == "analysis"  # 重挂门禁

    await restored.run_current_stage()
    await restored.answer_gate("pass")
    assert restored.current_stage == "modeling"
    second = StageOrchestrator.restore("t1", store, runner=runner)
    assert second.current_stage == "modeling"  # 已过门禁 → 推进
    store.close()


async def test_rpc_start_stage_reports_internal_error_not_invalid_params(tmp_path: Path) -> None:
    """跨端口径：本地数据问题不能被报成"你的参数错了"（会诱导调用方改参数重试）。"""
    import io
    import json

    from engine.ipc.events import EventEmitter
    from engine.ipc.methods import INTERNAL_ERROR, register_all
    from engine.ipc.rpc import INVALID_PARAMS
    from engine.ipc.server import JsonRpcServer
    from engine.ipc.state import EngineState

    db = str(tmp_path / "ckpt.db")
    store = SQLiteCheckpointStore(db)
    store.save_stage("t1", "analysis", CHECKPOINT_GATE_PASSED, 0, {"insights": ["x"]})
    _inject_unknown_status(db, "t1", "modeling", "corrupted")

    state = EngineState()
    server = JsonRpcServer(state, EventEmitter(sink=io.StringIO()))
    register_all(server, state, checkpoint=store)
    raw = await server.handle_line(
        json.dumps({"jsonrpc": "2.0", "id": "r1", "method": "start_stage",
                    "params": {"task_id": "t1", "stage": "solving"}})
    )
    payload = json.loads(raw)
    assert payload["error"]["code"] == INTERNAL_ERROR
    assert payload["error"]["code"] != INVALID_PARAMS
    assert "保留现场" in payload["error"]["message"]
    store.close()


def test_rpc_flow_uses_the_shared_predicate_not_its_own_copy() -> None:
    """回归驱动器曾自带一份 `== CHECKPOINT_EXECUTED` 判断——两处各写一遍就会各错一遍。"""
    source = (REPO_ROOT / "engine" / "regression" / "rpc_flow.py").read_text(encoding="utf-8")
    assert "gate_passed(" in source
    assert '== CHECKPOINT_EXECUTED' not in source
    assert '"stage_done"' not in source
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "engine.checkpoint.store"
        for alias in node.names
    }
    assert "gate_passed" in imported


def test_client_side_treats_unknown_as_not_passed() -> None:
    """跨端口径：客户端把非 "done" 一律视为未完成，与引擎"未知即拒绝推进"同向。"""
    source = (REPO_ROOT / "client" / "main" / "local-history.ts").read_text(encoding="utf-8")
    assert 'const CHECKPOINT_GATE_PASSED = "done"' in source
    assert "!== CHECKPOINT_GATE_PASSED" in source
    assert "stage_done" in source  # 注释里登记了两态含义，读库的人不会只看 done
