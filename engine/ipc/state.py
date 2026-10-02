"""引擎状态机（SP1-1 通信骨架）：idle → running → paused → stopped。

仅维护通信所需的最小状态，不实现任何阶段业务逻辑（SP1-1 红线）。
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EngineStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


VALID_STAGES = ("analysis", "modeling", "solving", "writing")


@dataclass(frozen=True, slots=True)
class TaskState:
    """当前任务快照。"""

    task_id: str
    stage: str
    status: str


@dataclass(slots=True)
class EngineState:
    """引擎运行时状态（单任务，通信骨架 + SP1-2 编排器引用）。"""

    status: str = EngineStatus.IDLE.value
    task: TaskState | None = None
    gate_answers: dict[str, str] = field(default_factory=dict)
    orchestrator: Any = None  # StageOrchestrator | None（SP1-2 注入，避免循环依赖）

    def start_stage(self, task_id: str, stage: str) -> TaskState:
        if stage not in VALID_STAGES:
            raise ValueError(f"非法阶段：{stage}")
        self.status = EngineStatus.RUNNING.value
        self.task = TaskState(task_id=task_id, stage=stage, status=StageStatus.RUNNING.value)
        return self.task

    def pause(self, task_id: str) -> None:
        self._require_task(task_id)
        assert self.task is not None
        self.status = EngineStatus.PAUSED.value
        self.task = TaskState(task_id=self.task.task_id, stage=self.task.stage, status=StageStatus.PAUSED.value)

    def resume(self, task_id: str) -> None:
        self._require_task(task_id)
        assert self.task is not None
        self.status = EngineStatus.RUNNING.value
        self.task = TaskState(task_id=self.task.task_id, stage=self.task.stage, status=StageStatus.RUNNING.value)

    def cancel(self, task_id: str) -> None:
        self._require_task(task_id)
        assert self.task is not None
        self.task = TaskState(task_id=self.task.task_id, stage=self.task.stage, status=StageStatus.CANCELLED.value)
        self.status = EngineStatus.IDLE.value

    def answer_gate(self, task_id: str, gate: str, decision: str) -> None:
        self._require_task(task_id)
        self.gate_answers[gate] = decision

    def snapshot(self) -> dict:
        """get_status 返回结构（含编排器快照，若已注入）。"""
        result = {
            "engine": self.status,
            "task": (
                {"task_id": self.task.task_id, "stage": self.task.stage, "status": self.task.status}
                if self.task
                else None
            ),
        }
        if self.orchestrator is not None:
            result["orchestrator"] = self.orchestrator.state.snapshot()
        return result

    def _require_task(self, task_id: str) -> None:
        if self.task is None or self.task.task_id != task_id:
            raise ValueError("任务不存在或 task_id 不匹配")
