"""任务流端口与 FakeLLM 实现（SP1-7 回归专用）。

TaskFlow 是验收运行器与执行链路之间的端口（接口抽象）：运行器只面向
「当前阶段查询 + 阶段执行 + 门禁应答 + 副作用观测」，不关心背后是进程内
编排器还是 RPC 子进程——真实 Key 通道可后续注入同端口的 RPC 驱动实现。

FakeLLMFlow 为进程内确定性实现（离线回归护栏通道，DEC-024：仅作回归护栏，
不计入 ≥85% 判定分母）。checkpoint_path 给定时具备断点恢复能力：构造时检测
已有检查点并 restore 续跑，与生产路径（ipc.methods.start_stage 恢复分支）语义
一致；恢复后的管线经 state_loader 水合跨阶段上下文（DEC-005 副作用不重放）。
"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from engine.checkpoint.store import SQLiteCheckpointStore
from engine.orchestrator.graph import StageOrchestrator
from engine.orchestrator.pipeline import FakeLLM, StagePipeline
from engine.regression.problems import RegressionProblem
from engine.sandbox.base import Sandbox
from engine.sandbox.subprocess_sandbox import SubprocessSandbox

# 与 pipeline._LLM 同型的文本模型端口（依赖注入点，测试可注入故障实现）
ChatLLM = Callable[[list[dict[str, str]], str], Awaitable[dict[str, Any]]]


class TaskFlow(Protocol):
    """回归任务流端口：运行器经此驱动任意执行链路。"""

    def current_stage(self) -> str:
        """当前应执行的阶段（恢复后为检查点定位结果）。"""
        ...

    async def run_stage(self, stage: str) -> dict[str, Any]:
        """执行指定阶段（与 current_stage 不一致时实现应抛阶段顺序错误）。"""
        ...

    async def answer_gate(self, decision: str) -> dict[str, Any]:
        """门禁应答（pass/reject）；返回编排器动作。"""
        ...

    def executed_stages(self) -> tuple[str, ...]:
        """实际执行过的阶段序列（副作用观测；断点恢复断言「不重放」用）。"""
        ...


FlowFactory = Callable[[RegressionProblem], TaskFlow]


class _ExecutionLog:
    """阶段执行副作用记录（sink 形态，接入 StagePipeline）。"""

    def __init__(self) -> None:
        self.stages: list[str] = []

    async def __call__(self, task_id: str, stage: str, data: dict[str, Any]) -> None:
        self.stages.append(stage)


class FakeLLMFlow:
    """进程内 FakeLLM 四阶段任务流（离线确定性；测试/演示专用，非真实模型）。"""

    def __init__(
        self,
        task_id: str,
        title: str,
        problem_text: str,
        *,
        checkpoint_path: Path | str | None = None,
        sandbox: Sandbox | None = None,
        work_root: Path | None = None,
        llm: ChatLLM | None = None,
    ) -> None:
        self._store = None
        if checkpoint_path:
            Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
            self._store = SQLiteCheckpointStore(str(checkpoint_path))
        self._exec_log = _ExecutionLog()
        pipeline = StagePipeline(
            llm=llm or FakeLLM().chat,
            sandbox=sandbox or SubprocessSandbox(timeout=30),
            sink=self._exec_log,
            work_root=work_root,
            task_inputs={task_id: {"title": title, "problem_text": problem_text}},
            state_loader=self._checkpoint_history,
        )
        self._orch = StageOrchestrator(task_id, checkpoint=self._store, runner=pipeline.process)
        if self._store is not None and self._store.completed_stages(task_id):
            self._orch = StageOrchestrator.restore(task_id, self._store, runner=pipeline.process)

    def _checkpoint_history(self, task_id: str) -> dict[str, dict[str, Any]]:
        """检查点 → 跨阶段上下文（恢复后管线水合；无检查点返回空）。"""
        if self._store is None:
            return {}
        return {record.stage: record.data for record in self._store.completed_stages(task_id)}

    def current_stage(self) -> str:
        return self._orch.current_stage

    async def run_stage(self, stage: str) -> dict[str, Any]:
        if stage != self._orch.current_stage:
            raise ValueError(f"阶段顺序约束：当前应执行 {self._orch.current_stage}，收到 {stage}")
        return await self._orch.run_current_stage()

    async def answer_gate(self, decision: str) -> dict[str, Any]:
        return await self._orch.answer_gate(decision)

    def executed_stages(self) -> tuple[str, ...]:
        return tuple(self._exec_log.stages)
