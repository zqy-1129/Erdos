"""检查点状态字面量的跨面守护（P0 数据正确性）。

缺陷形状：恢复定位时把"未识别的状态值"当作"门禁已通过"直接推进下一阶段。这类错误不抛异常、
不丢数据，只是让一个从未过门禁的阶段被跳过并落成 done —— 与本项目反复出现的
"看起来跑通了但语义不同"同形。且客户端 `main/local-history.ts` 的默认方向相反
（非 done 即未完成），同一份库两侧读出相反结论。

这里锁五件事：
1. 字面量合法集只有一个来源（存储层）；
2. 写入侧拒绝第三个值；
3. 读取侧（编排恢复 + 回归驱动）遇未知值一律拒绝推进，保留现场；
4. 跨端不出现第二份字面量定义（客户端只比对 "done"，语义与引擎一致：未知=未过门禁）；
5. 读取侧两个消费方对**同一份库**给出**同一个恢复点**（"记录→阶段"的判定只有一份实现）。
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
    resume_stage,
)
from engine.regression.rpc_flow import resolve_resume_stage

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

    注入的阶段必须是业务上**更靠后**的那个（modeling > analysis）：恢复定位按阶段序取最靠后的
    记录（`resume_stage`），排在后面的行才参与判定——写成更早的阶段就根本不会被读到，
    测的是排序而不是状态判定。updated_at 同时给到最新，覆盖"时间序与阶段序一致"的情形。
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


def test_rpc_flow_uses_the_shared_judgement_not_its_own_copy() -> None:
    """回归驱动器不得自己实现"取哪条记录 + 门禁是否已过"。

    这里原来锁的是"驱动器别复制一份 `== CHECKPOINT_EXECUTED`"，但定位规则本身仍是两份实现：
    编排器改成按阶段序取值后，驱动还按最后落库取值——同一份库两种恢复点，
    于是驱动发的 start_stage 被引擎的阶段顺序校验否决（20 题演练里的随机红灯）。
    所以本批把"记录→阶段"整段判定收成一份（`orchestrator.graph.resume_stage`），守护也跟着升级。
    """
    source = (REPO_ROOT / "engine" / "regression" / "rpc_flow.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    def _imported_from(module: str) -> set[str]:
        return {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == module
            for alias in node.names
        }

    assert "resume_stage" in _imported_from("engine.orchestrator.graph")
    # 判定所需的原料一个都不许出现在驱动里（出现了就说明又写了一遍）
    assert "gate_passed" not in _imported_from("engine.checkpoint.store")
    for forbidden in ("gate_passed(", "completed[-1]", "== CHECKPOINT_EXECUTED", '"stage_done"'):
        assert forbidden not in source, f"驱动器又写了一遍恢复判定：{forbidden}"


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        # 业务上更靠后的阶段先落库，更早的阶段后被回写（时间序尾 = analysis）：
        # 按时间取尾会退回 analysis 重跑，把已过门禁的建模进度吃掉
        ((("modeling", "done"), ("analysis", "stage_done")), "solving"),
        # 更深：solving 门禁未决 + analysis 晚写且已过门禁。按时间取尾得到 "modeling"，
        # 那是**跳过一个从未过门禁的阶段**——比多跑一遍更危险的方向
        ((("solving", "stage_done"), ("analysis", "done")), "solving"),
        # 正向对照：时间序与阶段序同向（正常推进路径），两种实现都给出同一答案
        ((("analysis", "done"), ("modeling", "done")), "solving"),
        # 最后阶段已过门禁 → 停在 writing（min(idx+1, len-1) 的钳制语义）
        ((("writing", "done"),), "writing"),
    ],
)
def test_orchestrator_and_rpc_driver_resolve_the_same_stage(
    tmp_path: Path, rows: tuple, expected: str
) -> None:
    """同一份 checkpoints.db，编排器与回归驱动必须定位到同一个阶段（判定只有一份实现）。

    这两侧读的是同一个库：驱动以为该跑 X、引擎以为该跑 Y 时，`start_stage` 的阶段顺序校验
    当场互相否决，表现为 kill/恢复演练里的随机红灯。判据本身与 `completed_stages` 的
    返回顺序无关（按 STAGES 阶段序取值），所以这里不需要控制写入时间戳。
    """
    home = tmp_path / "home"
    home.mkdir()
    store = SQLiteCheckpointStore(str(home / "checkpoints.db"))
    for stage, status in rows:
        store.save_stage("t-both", stage, status, 0, {"stage": stage})

    completed = store.completed_stages("t-both")
    assert resume_stage(completed) == expected
    assert StageOrchestrator.restore("t-both", store, runner=None).current_stage == expected
    assert resolve_resume_stage(home, "t-both") == expected
    store.close()


def test_resolve_resume_stage_defaults_to_analysis_without_history(tmp_path: Path) -> None:
    """无库 / 有库无记录都从 analysis 起——默认值也只有一份（`resume_stage` 的空输入分支）。"""
    empty_home = tmp_path / "no-db"
    empty_home.mkdir()
    assert resolve_resume_stage(empty_home, "t-none") == "analysis"

    home = tmp_path / "empty-db"
    home.mkdir()
    store = SQLiteCheckpointStore(str(home / "checkpoints.db"))
    store.close()
    assert resolve_resume_stage(home, "t-none") == "analysis"
    assert resume_stage([]) == "analysis"


def test_client_side_treats_unknown_as_not_passed() -> None:
    """跨端口径：客户端把非 "done" 一律视为未完成，与引擎"未知即拒绝推进"同向。"""
    source = (REPO_ROOT / "client" / "main" / "local-history.ts").read_text(encoding="utf-8")
    assert 'const CHECKPOINT_GATE_PASSED = "done"' in source
    assert "!== CHECKPOINT_GATE_PASSED" in source
    assert "stage_done" in source  # 注释里登记了两态含义，读库的人不会只看 done
