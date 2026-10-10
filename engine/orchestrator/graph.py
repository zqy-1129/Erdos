"""四阶段编排器（SP1-2）—— LangGraph 1.x 状态机实现。

执行计划要求（SP1-2）：
- LangGraph 四阶段状态机（分析→建模→求解→报告），阶段顺序硬编码禁止跳级；
- 每阶段后接门禁节点：interrupt 挂起，answer_gate 以 Command(resume) 注入人工干预；
- Checkpoint 落 SQLite（checkpoint.SQLiteCheckpointStore，与《数据模型设计》checkpoint_data 语义对齐）；
- 落库前对数据做敏感键脱敏：检查点不落 Key 明文。

图结构：START →(按 stage_index 路由)→ stage 节点 → gate 节点（interrupt）
        → finish → END；pass 使 stage_index+1、reject 清除当前阶段结果（前序不受影响）。
对外 API 与 SP1-1/SP1-2 既有调用方完全兼容（run_current_stage / answer_gate / restore / state）。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from engine.gates.hard_checks import check_stage
from engine.trail.redact import redact_sensitive  # 检查点脱敏（密钥域红线，公共实现）

# 四阶段固定顺序（禁止跳级）
STAGES = ("analysis", "modeling", "solving", "writing")


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    GATE_WAITING = "gate_waiting"  # 等待门禁评审
    DONE = "done"


# 检查点阶段状态两态（SP1-7 断点恢复门禁语义）：
#   EXECUTED = 阶段执行完成落库（门禁未决）；GATE_PASSED = 门禁通过（升级落库）。
# 恢复时区分二者：门禁未决阶段恢复后重挂门禁（阶段不重放），未经门禁不推进。
CHECKPOINT_EXECUTED = "stage_done"
CHECKPOINT_GATE_PASSED = StageStatus.DONE.value


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
# LangGraph 状态与节点 schema（每节点产出入 schema 化）
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
    """门禁决策载荷（interrupt resume 注入）。"""

    decision: str


# 阶段执行器注入点（SP1-3/SP1-4/SP1-5 真实阶段逻辑经此接入）
StageRunner = Callable[[str, str], Awaitable[dict]]


async def _default_stage_runner(task_id: str, stage: str) -> dict:
    """默认阶段执行：骨架空产出（真实逻辑在 SP1-4/SP1-5 注入）。"""
    return {}


def build_orchestrator_graph(runner: StageRunner):
    """构建 LangGraph 四阶段图：START 路由 + 4 阶段节点 + 4 门禁节点 + finish。"""
    builder = StateGraph(GraphState)

    def make_stage_node(stage: str):
        async def node(state: StageNodeInput) -> StageNodeOutput:
            index = int(state.get("stage_index", 0))
            if STAGES[index] != stage:
                raise RuntimeError(f"阶段顺序约束：期望 {STAGES[index]}，实际 {stage}")
            results = dict(state.get("results") or {})
            if stage in results:
                # 恢复重挂门禁路径：阶段产物已在检查点（门禁未决崩溃后恢复），
                # 跳过执行不重放副作用（DEC-005），图继续推进到门禁 interrupt。
                return {"results": results}
            data = await runner(str(state.get("task_id", "")), stage)
            results[stage] = {"status": StageStatus.DONE.value, "step": 0, "data": data}
            return {"results": results}

        return node

    def make_gate_node(stage: str):
        async def node(state: GraphState) -> dict:
            # 挂起等待门禁评审；Command(resume) 注入决策后自此处继续
            payload = interrupt({"stage": stage, "message": "等待门禁评审"})
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

    for stage in STAGES:
        builder.add_node(stage, make_stage_node(stage))
    for stage in STAGES:
        gate_name = f"gate_{stage}"  # LangGraph 节点名不允许 ':' 等保留字符
        builder.add_node(gate_name, make_gate_node(stage))
        builder.add_edge(stage, gate_name)
        builder.add_edge(gate_name, "finish")
    builder.add_node("finish", finish_node)
    builder.add_edge("finish", END)
    builder.add_conditional_edges(START, start_route, {stage: stage for stage in STAGES})
    return builder


class StageOrchestrator:
    """LangGraph 四阶段编排器（API 与 SP1-1 骨架完全兼容）。

    每阶段流程：run_current_stage → 阶段节点执行 → 门禁 interrupt 挂起
              → answer_gate(pass) 推动 stage_index（不自动执行下一阶段）
              → answer_gate(reject) 清除当前阶段结果重跑（前序保留）。
    崩溃恢复：restore() 从 SQLite 检查点重建（图状态以恢复结果 seed）。
    """

    def __init__(
        self,
        task_id: str,
        checkpoint: CheckpointStore | None = None,
        runner: StageRunner | None = None,
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
        self._graph = build_orchestrator_graph(self._runner).compile(
            checkpointer=InMemorySaver()
        )
        self._config: dict[str, Any] = {"configurable": {"thread_id": task_id}}

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
        """暴露编译后的 LangGraph 图（测试结构化断言用）。"""
        return self._graph

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
        result = await self._graph.ainvoke(invoke_input, self._config)
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
        """门禁评审结果注入（人工干预）：Command(resume) 恢复挂起的门禁节点。

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
        result = await self._graph.ainvoke(
            Command(resume={"decision": decision, "feedback": feedback}), self._config
        )
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
    ) -> "StageOrchestrator":
        """从检查点恢复编排器：已落库阶段全部还原，按门禁状态定位当前阶段。

        - 最后落库记录为「门禁通过」→ 当前阶段 = 其下一个（不重算已完成阶段）；
        - 最后落库记录为「执行完成（门禁未决）」→ 当前阶段 = 该阶段本身：
          run_current_stage 经阶段节点跳过守卫重挂门禁（不重放副作用，DEC-005）。
        runner 必须随恢复注入（生产路径与 SP1-7 断点恢复演练共用）：
        恢复的编排器还要执行剩余阶段，丢 runner 会静默退化为骨架空产出。
        """
        orchestrator = cls(task_id, checkpoint, runner=runner)
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
        if completed:
            last = completed[-1]
            if last.status == CHECKPOINT_EXECUTED:
                # 门禁未决崩溃：恢复到该阶段本身，重挂门禁
                orchestrator._state.current_stage = last.stage
            else:
                idx = STAGES.index(last.stage)
                orchestrator._state.current_stage = STAGES[min(idx + 1, len(STAGES) - 1)]
        orchestrator._seed = {
            "task_id": task_id,
            "stage_index": orchestrator._state.current_index,
            "results": seed_results,
        }
        # _seeded 保持 False：首次 run_current_stage 经 _ensure_seeded 注入种子，
        # 图从恢复定位的阶段继续（新实例的 InMemorySaver 为空，不能靠 checkpointer 续状态）。
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