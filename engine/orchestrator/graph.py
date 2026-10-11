"""四阶段编排器（SP1-2）。

执行计划要求（SP1-2）：
- 四阶段状态机（分析→建模→求解→报告），阶段顺序硬编码禁止跳级；
- 每阶段后接门禁节点：挂起等待，answer_gate 注入人工干预后自挂起点继续；
- Checkpoint 落 SQLite（checkpoint.SQLiteCheckpointStore，与《数据模型设计》checkpoint_data 语义对齐）；
- 落库前对数据做敏感键脱敏：检查点不落 Key 明文。

图结构：START →(按 stage_index 路由)→ stage 节点 → gate 节点（挂起）
        → finish → END；pass 使 stage_index+1、reject 清除当前阶段结果（前序不受影响）。
对外 API 与 SP1-1/SP1-2 既有调用方完全兼容（run_current_stage / answer_gate / restore / state）。

图**怎么装配**由 `backend.py` 按 `ERDOS_ORCH_IMPL` 选择（LangGraph 或自研内核）；
本文件只描述拓扑与语义，两套后端共用同一份节点体——阶段顺序、门禁决策、恢复定位
只有一份实现，双轨比对因此只比框架层。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, TypedDict

from engine.checkpoint.store import (
    CHECKPOINT_STATUS_EXECUTED,
    CHECKPOINT_STATUS_GATE_PASSED,
    gate_passed,
)
from engine.gates.hard_checks import check_stage
from engine.orchestrator.backend import END, build_stage_graph
from engine.trail.redact import redact_sensitive  # 检查点脱敏（密钥域红线，公共实现）

# 四阶段固定顺序（禁止跳级）
STAGES = ("analysis", "modeling", "solving", "writing")


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    GATE_WAITING = "gate_waiting"  # 等待门禁评审
    DONE = "done"


# 检查点阶段状态两态：字面量与判定单源在存储层（checkpoint/store.py），
# 这里按既有公开名再导出，调用方 import 路径不变。
CHECKPOINT_EXECUTED = CHECKPOINT_STATUS_EXECUTED
CHECKPOINT_GATE_PASSED = CHECKPOINT_STATUS_GATE_PASSED


class CheckpointStore(Protocol):
    """检查点存储端口（SP1-2）：阶段粒度，同步落库。"""

    def save_stage(self, task_id: str, stage: str, status: str, step: int, data: dict) -> None: ...
    def completed_stages(self, task_id: str) -> list: ...
    def delete_stage(self, task_id: str, stage: str) -> None: ...


@dataclass(frozen=True, slots=True)
class StageResult:
    """单个阶段的产出快照。"""

    stage: str
    status: str
    step: int
    data: dict


@dataclass(slots=True)
class OrchestratorState:
    """四阶段编排器运行时状态（对外视图，get_status 与测试消费）。"""

    task_id: str
    current_stage: str = STAGES[0]
    stages: dict[str, StageResult] = field(default_factory=dict)
    gate_decision: str | None = None  # pass / reject（等待门禁时由 answer_gate 注入）

    @property
    def current_index(self) -> int:
        return STAGES.index(self.current_stage)

    def snapshot(self) -> dict:
        """get_status 用的编排状态快照。"""
        return {
            "task_id": self.task_id,
            "current_stage": self.current_stage,
            "stage_index": self.current_index,
            "stages": {name: {"status": r.status, "step": r.step} for name, r in self.stages.items()},
            "gate_decision": self.gate_decision,
        }


# ----------------------------------------------------------------------
# 图状态 schema 与拓扑（后端中性的描述，由 backend.py 装配）
# ----------------------------------------------------------------------

class GraphState(TypedDict, total=False):
    """图状态：results 为 stage -> {status/step/data}（可用 JSON 序列化）。"""

    task_id: str
    stage_index: int
    results: dict[str, dict[str, Any]]
    gate_decision: str
    gate_feedback: str


class StageNodeInput(TypedDict, total=False):
    """阶段节点输入（GraphState 子集）。"""

    task_id: str
    stage_index: int
    results: dict[str, dict[str, Any]]


class StageNodeOutput(TypedDict):
    """阶段节点产出（只更新 results）。"""

    results: dict[str, dict[str, Any]]


class GateDecision(TypedDict):
    """门禁决策载荷（挂起恢复时注入）。"""

    decision: str
    feedback: str


# 通道名从 schema 注解导出：自研内核的声明与 TypedDict 不可能各写一遍再漂移
GRAPH_CHANNELS = tuple(GraphState.__annotations__)

FINISH_NODE = "finish"


def gate_node_name(stage: str) -> str:
    """门禁节点名——`gate_{stage}` 已进 RPC 事件（gate.failed.gate），不可更名。"""
    return f"gate_{stage}"


def _latest_stage(records: list[Any]) -> Any:  # noqa: ANN401 - CheckpointRecord（存储层类型，此处只读字段）
    """取**阶段顺序**最靠后的一条检查点记录，而不是最后写入的那条。

    恢复定位是数据正确性判据（选错就要么重跑已完成阶段、要么跳过未过门禁的阶段），
    所以它只能依赖"分析→建模→求解→报告"这条业务序，不能依赖 `updated_at` 的时间序：
    时间序描述的是"最近写过哪一行"，今天恰好与业务序一致（每阶段各写一次、按业务序推进），
    但那是**写入模式的巧合**而不是不变量——重跑或回退更早阶段的任何通道都会打破它，
    且相同时间戳并列时 SQL 不保证返回顺序。
    未知阶段名一律拒绝——与 `gate_passed()` 对未知状态字面量的处理同一口径。
    """
    indexes = []
    for record in records:
        try:
            indexes.append(STAGES.index(record.stage))
        except ValueError as exc:
            raise ValueError(
                f"检查点含未知阶段 {record.stage!r}（只接受 {STAGES}）；已拒绝据此定位恢复点，保留现场待人工核对"
            ) from exc
    return records[max(range(len(records)), key=indexes.__getitem__)]


def resume_stage(records: list[Any]) -> str:  # noqa: ANN401 - CheckpointRecord（存储层类型，此处只读字段）
    """阶段检查点记录 → 恢复后应处的阶段（空记录 = 全新任务，从 analysis 起）。

    **判定单源**：编排器（`StageOrchestrator.restore`）与回归 RPC 驱动（`rpc_flow`）读同一张
    checkpoints 表，定位必须走这一份实现——各写一遍就会出现同一份库两种恢复点（驱动以为该跑
    solving、编排器以为该重跑 analysis，`start_stage` 当场互相否决）。
    阶段序最靠后的记录：门禁未决 → 该阶段本身（重挂门禁，不重放副作用，DEC-005）；
    已过门禁 → 其下一个阶段（不重算已完成阶段）。
    """
    if not records:
        return STAGES[0]
    last = _latest_stage(records)
    if not gate_passed(last.status):
        # 未知字面量在 gate_passed() 里抛错，不会走到"默认推进"分支：
        # 未过门禁的阶段被跳过是数据错误，比恢复失败更难发现。
        return last.stage
    idx = STAGES.index(last.stage)
    return STAGES[min(idx + 1, len(STAGES) - 1)]


_STATIC_EDGES: dict[str, str] = {
    **{stage: gate_node_name(stage) for stage in STAGES},
    **{gate_node_name(stage): FINISH_NODE for stage in STAGES},
    FINISH_NODE: END,
}
_ENTRY_MAP: dict[str, str] = {stage: stage for stage in STAGES}


# 阶段执行器注入点（SP1-3/SP1-4/SP1-5 真实阶段逻辑经此接入）
StageRunner = Callable[[str, str], Awaitable[dict]]


async def _default_stage_runner(task_id: str, stage: str) -> dict:
    """默认阶段执行：骨架空产出（真实逻辑在 SP1-4/SP1-5 注入）。"""
    return {}


def make_stage_node(stage: str, runner: StageRunner):
    async def node(state: StageNodeInput) -> StageNodeOutput:
        index = int(state.get("stage_index", 0))
        if STAGES[index] != stage:
            raise RuntimeError(f"阶段顺序约束：期望 {STAGES[index]}，实际 {stage}")
        results = dict(state.get("results") or {})
        if stage in results:
            # 恢复重挂门禁路径：阶段产物已在检查点（门禁未决崩溃后恢复），
            # 跳过执行不重放副作用（DEC-005），图继续推进到门禁挂起点。
            return {"results": results}
        data = await runner(str(state.get("task_id", "")), stage)
        results[stage] = {"status": StageStatus.DONE.value, "step": 0, "data": data}
        return {"results": results}

    return node


def make_gate_node(stage: str, suspend: Callable[[dict], Any]):
    async def node(state: GraphState) -> dict:
        # 挂起等待门禁评审；注入决策后自此处继续（suspend 由后端提供，恢复时交出决策）
        payload = suspend({"stage": stage, "message": "等待门禁评审"})
        if isinstance(payload, dict):
            decision = str(payload.get("decision", ""))
        else:
            decision = str(payload)
        index = int(state.get("stage_index", 0))
        if decision == "pass":
            return {
                "stage_index": min(index + 1, len(STAGES) - 1),
                "gate_decision": "pass",
            }
        # reject：清除当前阶段结果（前序阶段不受影响），index 不变 → 重跑
        results = dict(state.get("results") or {})
        results.pop(stage, None)
        return {"results": results, "gate_decision": "reject"}

    return node


def start_route(state: GraphState) -> str:
    index = int(state.get("stage_index", 0))
    if not 0 <= index < len(STAGES):
        raise RuntimeError(f"非法阶段索引：{index}")
    return STAGES[index]


async def finish_node(state: GraphState) -> dict:
    return {}


def build_orchestrator_graph(runner: StageRunner, task_id: str, impl: str | None = None):
    """按当前后端装配四阶段主图，返回 `CompiledGraph`（原返回裸 StateGraph builder 的签名已上移一层）。"""
    return build_stage_graph(
        task_id=task_id,
        schema=GraphState,
        channels=GRAPH_CHANNELS,
        static=_STATIC_EDGES,
        entry_map=_ENTRY_MAP,
        route=start_route,
        make_nodes=lambda suspend: {
            **{stage: make_stage_node(stage, runner) for stage in STAGES},
            **{gate_node_name(stage): make_gate_node(stage, suspend) for stage in STAGES},
            FINISH_NODE: finish_node,
        },
        impl=impl,
    )


class StageOrchestrator:
    """四阶段编排器（API 与 SP1-1 骨架完全兼容）。

    每阶段流程：run_current_stage → 阶段节点执行 → 门禁挂起
              → answer_gate(pass) 推动 stage_index（不自动执行下一阶段）
              → answer_gate(reject) 清除当前阶段结果重跑（前序保留）。
    崩溃恢复：restore() 从 SQLite 检查点重建（图状态以恢复结果 seed）。
    """

    def __init__(
        self,
        task_id: str,
        checkpoint: CheckpointStore | None = None,
        runner: StageRunner | None = None,
        impl: str | None = None,
    ) -> None:
        self._task_id = task_id
        self._checkpoint = checkpoint
        self._runner = runner or _default_stage_runner
        self._state = OrchestratorState(task_id=task_id)
        self._gate_waiting = False
        # SP1-3 硬检查驳回原因（供上层发 gate.failed 事件；None=本次无硬检查失败）
        self.last_hard_reject: str | None = None
        self._seeded = False
        self._seed: dict[str, Any] = {}
        self._backend = build_orchestrator_graph(self._runner, task_id, impl=impl)

    # ------------------------------------------------------------------
    # 对外视图（兼容既有调用方）
    # ------------------------------------------------------------------
    @property
    def state(self) -> OrchestratorState:
        return self._state

    @property
    def current_stage(self) -> str:
        return self._state.current_stage

    def is_final_stage(self) -> bool:
        return self._state.current_index >= len(STAGES) - 1

    def graph(self):
        """暴露编译后的图（测试结构化断言用）；两套后端提供同名同形的自省接口。"""
        return self._backend.raw()

    # ------------------------------------------------------------------
    # 阶段推进
    # ------------------------------------------------------------------
    async def run_current_stage(self) -> dict:
        """执行当前阶段直至门禁挂起；挂起未决时重复调用幂等返回当前产出。

        SP1-3 硬检查（真实模式）：阶段产出先经代码级判定，违规即复用既有人工 reject 语义
        自动驳回——本次门禁已被消费，故 _gate_waiting 必须回落 False（不得谎报"在等人工门禁"），
        下一次 run_current_stage 按 reject 语义重跑当前阶段。骨架模式（默认 runner）不判定，
        与 answer_gate 的骨架/真实模式分野保持一致。
        """
        if self._gate_waiting:
            result = self._state.stages.get(self._state.current_stage)
            return result.data if result else {}
        invoke_input = await self._ensure_seeded()
        result = await self._backend.run(invoke_input)
        self._sync(result)
        current = self._state.stages.get(self._state.current_stage)
        self._gate_waiting = True
        self.last_hard_reject = None
        if current is not None:
            stage = self._state.current_stage
            violations = (
                [] if self._runner is _default_stage_runner else check_stage(stage, current.data)
            )
            if violations:
                reason = ("硬检查不通过：" + "；".join(violations))[:2000]
                self.last_hard_reject = reason
                await self.answer_gate("reject", feedback=reason)
                return current.data
            self._persist_current(current.data)  # 阶段完成即落检查点（脱敏后）
        return current.data if current is not None else {}

    async def answer_gate(self, decision: str, feedback: str = "") -> dict:
        """门禁评审结果注入（人工干预）：把决策交回挂起点，恢复被挂起的门禁节点。

        兼容既有调用方：无挂起门禁时按旧语义直接驱动（幂等记录决策并推进/回退）。
        """
        if decision not in ("pass", "reject"):
            raise ValueError("decision 必须为 pass 或 reject")
        if not self._gate_waiting:
            # 无挂起门禁：
            # - 骨架模式（默认 runner）保持 SP1-1 兼容直接驱动；
            # - 真实模式（注入 runner）下重复/过期应答按 EC-U4 防御：当前阶段
            #   未执行时 pass 不得越门禁推进（防重复 pass 把未执行阶段标记通过）。
            current = self._state.stages.get(self._state.current_stage)
            if self._runner is not _default_stage_runner and decision == "pass" and current is None:
                raise ValueError(
                    f"门禁冲突：{self._state.current_stage} 无挂起门禁且未执行"
                    "（重复/过期应答？请先经 start_stage 执行并等待门禁挂起）"
                )
            self._state.gate_decision = decision
            if decision == "reject":
                self._state.stages.pop(self._state.current_stage, None)
                if self._checkpoint is not None:
                    self._checkpoint.delete_stage(self._task_id, self._state.current_stage)
                return {"action": "retry_stage", "stage": self._state.current_stage}
            self._persist_gate_passed(self._state.current_stage)
            if self._state.current_index >= len(STAGES) - 1:
                return {"action": "complete", "stage": self._state.current_stage}
            self._state.current_stage = STAGES[self._state.current_index + 1]
            self._state.gate_decision = None
            return {"action": "next_stage", "stage": self._state.current_stage}
        gate_stage = self._state.current_stage
        payload: GateDecision = {"decision": decision, "feedback": feedback}
        result = await self._backend.resume(payload)
        self._sync(result)
        self._gate_waiting = False
        if decision == "reject":
            # 重跑当前阶段：清除该阶段检查点（前序阶段保留）
            if self._checkpoint is not None:
                self._checkpoint.delete_stage(self._task_id, gate_stage)
            return {"action": "retry_stage", "stage": gate_stage}
        # 门禁通过：检查点由「执行完成（门禁未决）」升级为「门禁通过」，
        # 崩溃恢复据此区分已审/未审阶段（未经门禁不推进）。
        self._persist_gate_passed(gate_stage)
        if gate_stage == STAGES[-1]:
            # 最后阶段的门禁通过 → 任务完成
            return {"action": "complete", "stage": self._state.current_stage}
        return {"action": "next_stage", "stage": self._state.current_stage}

    # ------------------------------------------------------------------
    # 恢复（从 SQLite 检查点，不重算已完成阶段）
    # ------------------------------------------------------------------
    @classmethod
    def restore(
        cls,
        task_id: str,
        checkpoint: CheckpointStore,
        runner: StageRunner | None = None,
        impl: str | None = None,
    ) -> "StageOrchestrator":
        """从检查点恢复编排器：已落库阶段全部还原，按门禁状态定位当前阶段。

        定位由 `resume_stage()` 单源给出（回归驱动共用）：阶段序最靠后的记录为「门禁通过」
        → 当前阶段 = 其下一个（不重算已完成阶段）；为「执行完成（门禁未决）」→ 当前阶段 =
        该阶段本身，run_current_stage 经阶段节点跳过守卫重挂门禁（不重放副作用，DEC-005）。
        runner 必须随恢复注入（生产路径与 SP1-7 断点恢复演练共用）：
        恢复的编排器还要执行剩余阶段，丢 runner 会静默退化为骨架空产出。
        """
        orchestrator = cls(task_id, checkpoint, runner=runner, impl=impl)
        completed = checkpoint.completed_stages(task_id)
        seed_results: dict[str, dict[str, Any]] = {}
        for record in completed:
            orchestrator._state.stages[record.stage] = StageResult(
                stage=record.stage, status=record.status, step=record.step, data=record.data
            )
            seed_results[record.stage] = {
                "status": record.status,
                "step": record.step,
                "data": record.data,
            }
        orchestrator._state.current_stage = resume_stage(completed)
        orchestrator._seed = {
            "task_id": task_id,
            "stage_index": orchestrator._state.current_index,
            "results": seed_results,
        }
        # _seeded 保持 False：首次 run_current_stage 经 _ensure_seeded 注入种子，
        # 图从恢复定位的阶段继续（新实例的线程态为空，不能靠图自身续状态）。
        orchestrator._seeded = False
        return orchestrator

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    async def _ensure_seeded(self) -> dict:
        if self._seeded:
            return {}
        self._seeded = True
        if self._seed:
            return self._seed
        return {"task_id": self._task_id, "stage_index": self._state.current_index, "results": {}}

    def _sync(self, graph_state: dict) -> None:
        """图状态 → 对外 OrchestratorState 视图。"""
        index = int(graph_state.get("stage_index", self._state.current_index))
        results = graph_state.get("results") or {}
        stages: dict[str, StageResult] = {}
        for stage in STAGES:
            record = results.get(stage)
            if record:
                stages[stage] = StageResult(
                    stage=stage,
                    status=record.get("status", StageStatus.DONE.value),
                    step=int(record.get("step", 0)),
                    data=record.get("data", {}),
                )
        self._state.stages = stages
        self._state.current_stage = STAGES[index]
        self._state.gate_decision = graph_state.get("gate_decision")

    def _persist_current(self, data: dict) -> None:
        """阶段执行完成落检查点（状态=门禁未决；门禁通过时升级，敏感键脱敏不落 Key 明文）。"""
        if self._checkpoint is None:
            return
        safe = redact_sensitive(data)
        self._checkpoint.save_stage(
            self._task_id, self._state.current_stage, CHECKPOINT_EXECUTED, 0, safe
        )

    def _persist_gate_passed(self, stage: str) -> None:
        """门禁通过：该阶段检查点状态升级为「门禁通过」（未经门禁不推进的恢复依据）。"""
        if self._checkpoint is None:
            return
        result = self._state.stages.get(stage)
        if result is None:
            return
        self._checkpoint.save_stage(
            self._task_id, stage, CHECKPOINT_GATE_PASSED, 0, redact_sensitive(result.data)
        )
