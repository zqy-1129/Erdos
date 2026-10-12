"""线上状态词表与契约的机器比对（G49①，2026-10-12）。

查出来的形状：阶段状态在仓内有**四套写法**——契约 `stageStatus` 六值、`ipc/state.py` 的
`StageStatus`（镜像契约）、`orchestrator/graph.py` 的**同名** `StageStatus`（四值，编排器内部态）、
以及 `ipc/methods.py` 里曾以裸字面量写的六处。此前**没有任何一条守护比过它们**：
`test_contract_alignment.py` 比的是方法名/事件名/事件必填字段/transport，不比方法 result 的字段集。

本文件只兜**引擎侧能自证**的那半条（G49①）：
- 三个契约枚举 ⇔ 引擎常量逐条相等（改一边不改另一边就红）；
- 出口写侧不得再出现裸的 `TaskState(status="…")` / `.status == "…"`（枚举是唯一入口）；
- 两份同名枚举**确实是两套词表**这一事实要写下来，别被下一个人"顺手对齐"。

G49②（`orchestrator`/`protocol`/`last_error`/`action` 四个契约外字段）与 G49③（恢复路径把检查点态
`stage_done` 透传进出口字段）都要 CT/ARCH 与跨端口径，**不在这里偷偷定**；③用一条现状钉桩
（`test_restored_snapshot_currently_leaks_checkpoint_vocabulary`）把事实锁住：真要改它必须显式改这条用例。
"""

import ast
import json
from pathlib import Path

from engine.checkpoint.store import CHECKPOINT_STATUS_EXECUTED, SQLiteCheckpointStore
from engine.ipc.state import EngineStatus, StageStatus
# `OrchestratorStageStatus` 是编排器**内部态**，与上面 `engine.ipc.state.StageStatus`（线上态）
# 同名而不同词表——别名把这一点写在一个名字上，见 `test_two_same_named_enums_are_deliberately_different`。
from engine.orchestrator.graph import STAGES, StageOrchestrator
from engine.orchestrator.graph import StageStatus as OrchestratorStageStatus

REPO = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO / "contracts" / "engine-rpc.schema.json"
METHODS_PATH = REPO / "engine" / "ipc" / "methods.py"


def _definitions() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))["definitions"]


def _contract_enum(name: str) -> list[str]:
    return list(_definitions()[name]["enum"])


# ----------------------------------------------------------------------
# 契约 enum ⇔ 引擎常量（改一边不改另一边就红）
# ----------------------------------------------------------------------
def test_stage_names_are_the_contract_order_not_a_copy() -> None:
    """阶段名不仅值集要相等，**顺序**也要相等——恢复定位靠的就是这个业务序。"""
    assert STAGES == tuple(_contract_enum("stageName"))


def test_engine_status_matches_contract() -> None:
    assert {e.value for e in EngineStatus} == set(_contract_enum("engineStatus"))


def test_wire_stage_status_matches_contract() -> None:
    assert {s.value for s in StageStatus} == set(_contract_enum("stageStatus"))


def test_two_same_named_enums_are_deliberately_different() -> None:
    """`graph.StageStatus` 是编排器内部态，与线上 `ipc.state.StageStatus` **不是同一套词表**。

    这条不是"发现不一致所以要修平"——恰恰相反：内部态有 `gate_waiting`（门禁挂起），线上态没有它，
    把两边"顺手对齐"会让挂起语义在对外接口里无处安放。名字撞车本身是 G49③ 要处理的口径问题，
    这里先把"它们本就该不同"钉住，防止有人把它当成 bug 改掉。
    """
    inner = {s.value for s in OrchestratorStageStatus}
    wire = {s.value for s in StageStatus}
    assert "gate_waiting" in inner and "gate_waiting" not in wire
    assert inner != wire


# ----------------------------------------------------------------------
# 出口写侧的裸字面量守护
# ----------------------------------------------------------------------
def _literal_status_writes(source: str) -> list[str]:
    """找出把阶段状态写成裸字符串的地方（`TaskState(status="…")` 与 `.status == "…"`）。

    刻意**不**管 `{"status": "created"}` 这类响应字典的键：`task_create` 的 `created` 是契约里
    写死的 `const`，与阶段状态词表无关，一并禁掉会造出假缺陷（本仓那条老话：带误报的守护会被关掉）。
    """
    problems: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "TaskState":
            for kw in node.keywords:
                if kw.arg == "status" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    problems.append(f"TaskState(status={kw.value.value!r}) @line {node.lineno}")
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Attribute) and node.left.attr == "status":
            for comp in node.comparators:
                if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                    problems.append(f".status == {comp.value!r} @line {node.lineno}")
    return problems


def test_ipc_methods_writes_stage_status_only_via_the_enum() -> None:
    """`get_status` 的 `task.status` 是契约限定字段（`stageStatus`），取值只能有一个来源。"""
    source = METHODS_PATH.read_text(encoding="utf-8")
    assert _literal_status_writes(source) == [], (
        "engine/ipc/methods.py 里出现了裸的阶段状态字面量——请改用 ipc.state.StageStatus，"
        "否则加一个状态就要在枚举与写侧两处各改一遍，而契约那第三处没人管"
    )


def test_the_literal_guard_actually_catches_a_literal() -> None:
    """正向对照：守护必须能被测坏，否则它只是在给自己发 ✅。"""
    snippet = 'state.task = TaskState(task_id="t", stage="analysis", status="paused")\n'
    found = _literal_status_writes(snippet)
    assert len(found) == 1 and "paused" in found[0], found

    assert len(_literal_status_writes('if task.status == "failed":\n    pass\n')) == 1

    # 响应字典里的 `status` 键不算违规（契约 const "created" 那一类）
    assert _literal_status_writes('return {"task_id": "t", "status": "created"}\n') == []


# ----------------------------------------------------------------------
# G49③ 的现状钉桩：恢复路径把检查点态透传到出口
# ----------------------------------------------------------------------
async def test_restored_snapshot_currently_leaks_checkpoint_vocabulary(tmp_path) -> None:
    """钉住事实，不是认可它：恢复后的 `get_status.orchestrator.stages.*.status` 会发出 `stage_done`。

    `graph.restore()` 把检查点记录的 status **原样**放进 `StageResult`，再由 `OrchestratorState.snapshot()`
    带出门——于是同一栏在运行路径给 `done`、恢复路径给 `stage_done`。`stage_done` 属检查点词表，
    **契约 `stageStatus` 枚举里没有它**；今天客户端把它声明成 `string` 且无人按它分支，所以是 latent。
    真要收敛（加值 / 出口映射 / 移除该字段）都要跨端口径，**改的时候必须显式改这条用例**，
    不许让它跟着实现静默变绿——那正是这个缺口最难发现的样子。
    """
    store = SQLiteCheckpointStore(str(tmp_path / "ckpt.db"))
    orch = StageOrchestrator("t-g49", checkpoint=store)
    await orch.run_current_stage()  # analysis 执行完成、门禁未决 → 落 `stage_done`

    restored = StageOrchestrator.restore("t-g49", store)
    emitted = restored.state.snapshot()["stages"]["analysis"]["status"]

    assert emitted == CHECKPOINT_STATUS_EXECUTED == "stage_done"
    assert emitted not in _contract_enum("stageStatus"), (
        "检查点态竟然进了契约枚举——那 G49③ 的口径已经变了，本用例与对齐报告 §8.27 都要一起改"
    )
    store.close()
