"""四阶段编排器（SP1-2）：分析→建模→求解→报告，固定顺序 + 门禁节点。

对齐 LangGraph 核心概念（节点/边/状态/检查点），采用轻量纯 Python 实现，
避免重依赖版本兼容风险；接口预留 LangGraph 接入点。

红线（SP1-2 提示词）：
- 阶段顺序硬编码禁止跳级；
- 每阶段后接门禁节点（pass 进入下一阶段 / reject 重跑当前阶段）；
- 检查点不落 Key 明文。
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

# 四阶段固定顺序（禁止跳级）
STAGES = ("analysis", "modeling", "solving", "writing")


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    GATE_WAITING = "gate_waiting"  # 等待门禁评审
    DONE = "done"


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
    """四阶段编排器运行时状态（对齐 stage_runs 语义）。"""

    task_id: str
    current_stage: str = STAGES[0]
    stages: dict[str, StageResult] = field(default_factory=dict)
    gate_decision: str | None = None  # pass / reject（等待门禁时由 answer_gate 注入）

    @property
    def current_index(self) -> int:
        return STAGES.index(self.current_stage)

    def advance(self) -> bool:
        """进入下一阶段；已是最后阶段返回 False。"""
        if self.current_index >= len(STAGES) - 1:
            return False
        self.current_stage = STAGES[self.current_index + 1]
        self.gate_decision = None
        return True

    def mark_done(self, step: int, data: dict) -> None:
        """标记当前阶段完成（产出落检查点），进入等待门禁态。"""
        self.stages[self.current_stage] = StageResult(
            stage=self.current_stage, status=StageStatus.DONE.value, step=step, data=data
        )
        self.gate_decision = None

    def snapshot(self) -> dict:
        """get_status 用的编排状态快照。"""
        return {
            "task_id": self.task_id,
            "current_stage": self.current_stage,
            "stage_index": self.current_index,
            "stages": {name: {"status": r.status, "step": r.step} for name, r in self.stages.items()},
            "gate_decision": self.gate_decision,
        }


class StageOrchestrator:
    """四阶段编排器：线性流程 + 门禁控制。

    每阶段流程：RUNNING →（产出）→ GATE_WAITING → answer_gate(pass) → 下一阶段
                                        └→ answer_gate(reject) → 重跑当前阶段
    """

    def __init__(self, task_id: str, checkpoint: CheckpointStore | None = None) -> None:
        self._state = OrchestratorState(task_id=task_id)
        self._checkpoint = checkpoint

    @property
    def state(self) -> OrchestratorState:
        return self._state

    @property
    def current_stage(self) -> str:
        return self._state.current_stage

    def is_final_stage(self) -> bool:
        return self._state.current_index >= len(STAGES) - 1

    # ------------------------------------------------------------------
    # 阶段推进（仅骨架：产出为空 data，业务逻辑在 SP1-3/SP1-4 实现）
    # ------------------------------------------------------------------
    async def run_current_stage(self) -> dict:
        """执行当前阶段（骨架：产出空 data，标记完成进入门禁等待）。

        真实阶段逻辑（模型调用/沙箱）由 SP1-4/SP1-5 注入，此处仅走编排流程。
        """
        data: dict = {}
        self._state.mark_done(step=0, data=data)
        if self._checkpoint is not None:
            self._checkpoint.save_stage(
                self._state.task_id, self._state.current_stage, StageStatus.DONE.value, 0, data
            )
        return data

    async def answer_gate(self, decision: str, feedback: str = "") -> dict:
        """门禁评审结果注入（人工干预）：pass 进入下一阶段，reject 重跑当前阶段。

        reject 时前序阶段不受影响（已完成的阶段结果保留），仅当前阶段重跑。
        """
        if decision not in ("pass", "reject"):
            raise ValueError("decision 必须为 pass 或 reject")
        self._state.gate_decision = decision
        if decision == "reject":
            # 重跑当前阶段：清除该阶段的 done 结果（含检查点），回到 running
            self._state.stages.pop(self._state.current_stage, None)
            if self._checkpoint is not None:
                self._checkpoint.delete_stage(self._state.task_id, self._state.current_stage)
            return {"action": "retry_stage", "stage": self._state.current_stage}
        advanced = self._state.advance()
        if not advanced:
            return {"action": "complete", "stage": self._state.current_stage}
        return {"action": "next_stage", "stage": self._state.current_stage}

    # ------------------------------------------------------------------
    # 恢复（从检查点续跑，不重算已完成阶段）
    # ------------------------------------------------------------------
    @classmethod
    def restore(cls, task_id: str, checkpoint: CheckpointStore) -> "StageOrchestrator":
        """从检查点恢复编排器：已完成阶段全部还原，当前阶段 = 最后完成阶段的下一个。"""
        orchestrator = cls(task_id, checkpoint)
        completed = checkpoint.completed_stages(task_id)
        for record in completed:
            orchestrator._state.stages[record.stage] = StageResult(
                stage=record.stage, status=record.status, step=record.step, data=record.data
            )
        if completed:
            last_stage = completed[-1].stage
            idx = STAGES.index(last_stage)
            if idx < len(STAGES) - 1:
                orchestrator._state.current_stage = STAGES[idx + 1]
            else:
                orchestrator._state.current_stage = STAGES[-1]
        return orchestrator
