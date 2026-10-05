"""四阶段端到端流水线（SP1-7 系统测试支撑；SP1-3/4/5/6 组件接线）。

- LLM 可注入：真实 OpenAI 兼容适配（SP1-5）或 FakeLLM（离线确定性，测试/演示）；
- solving 阶段经沙箱执行（SP1-4 SubprocessSandbox：超时强杀/环境隔离/路径白名单）；
- 各阶段产出结构化 data，writing 产物落盘并计算 sha256（SP1-6 留痕 artifact 接线）；
- 作为 LangGraph 编排器的 StageRunner 注入（run_current_stage 执行真实逻辑）。
红线：FakeLLM 为确定性假模型，仅用于测试/演示，真实任务必须注入真实适配器。
"""

from collections.abc import Awaitable, Callable
from hashlib import sha256
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

from engine.orchestrator.solve_loop import SolveLoop

# ----------------------------------------------------------------------
# 阶段完成回调端口（留痕接线，SP1-6）
# ----------------------------------------------------------------------
StageSink = Callable[[str, str, dict[str, Any]], Awaitable[None]]


class FakeLLM:
    """离线确定性假模型：按阶段返回模板化推理内容（测试/演示专用，非真实模型）。"""

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return max(1, len(text) // 3)

    async def chat(self, messages: list[dict[str, str]], stage: str) -> dict[str, Any]:
        prompt = " ".join(m.get("content", "") for m in messages)
        if stage == "analysis":
            text = (
                "要点提取：1) 决策变量与目标；2) 数据特征（样本/缺失）；"
                "3) 约束条件（资源/边界）；4) 可验证的量化指标。"
            )
        elif stage == "modeling":
            text = (
                "模型假设：变量独立、线性关系近似成立、数据无显著离群。"
                "目标函数：min f(x)=Σwᵢ·xᵢ；约束：xᵢ≥0 且资源总量受限。"
            )
        elif stage == "writing":
            text = "论文草稿：摘要/问题重述/模型/求解/结果分析/结论 六节齐全。"
        else:
            text = f"（求解阶段由沙箱执行，模型仅复核）：{prompt[:40]}"
        return {
            "content": text,
            "usage": {
                "prompt_tokens": self._estimate_tokens(prompt),
                "completion_tokens": self._estimate_tokens(text),
            },
            "model": "fake-llm/deterministic",
            "stage": stage,
        }


_LLM = Callable[[list[dict[str, str]], str], Awaitable[dict[str, Any]]]


def _default_llm() -> _LLM:
    fake = FakeLLM()
    return fake.chat


_SOLVE_SCRIPT = """\
# 线性拟合求解（确定性示例）：给定数据点求 y=ax+b 的最小二乘解
points = [(0, 2.1), (1, 4.0), (2, 6.2), (3, 7.8), (4, 10.1)]
n = len(points)
sx = sum(p[0] for p in points)
sy = sum(p[1] for p in points)
sxy = sum(p[0] * p[1] for p in points)
sxx = sum(p[0] ** 2 for p in points)
a = (n * sxy - sx * sy) / (n * sxx - sx * sx)
b = (sy - a * sx) / n
print(f"slope={a:.4f}")
print(f"intercept={b:.4f}")
"""


class StagePipeline:
    """四阶段流水线：process(task_id, stage) → 结构化阶段产出。

    作为 `StageOrchestrator(runner=pipeline.process)` 注入，接上 SP1-4 沙箱、
    SP1-5 适配层与 SP1-6 留痕（sink 回调）。
    W11：registry/operations/solve_llm 注入且 tool_mode="tool_loop" 时，solving 走
    模型驱动工具循环；否则保持既有阶段级路径（无工具能力端点降级，EN-CAP 路由）。
    """

    def __init__(
        self,
        llm: _LLM | None = None,
        sandbox: Any | None = None,
        sink: StageSink | None = None,
        work_root: Path | None = None,
        registry: Any | None = None,  # noqa: ANN401 - ToolRegistry | None
        operations: Any | None = None,  # noqa: ANN401 - OperationLog | None
        solve_llm: Any | None = None,  # noqa: ANN401 - SolveLLMPort | None
        tool_mode: str = "stage_level",
        delta_sink: Any | None = None,  # noqa: ANN401 - Callable[[str, str], None] | None（W15）
    ) -> None:
        self._llm = llm or _default_llm()
        self._sandbox = sandbox
        self._sink = sink
        self._work_root = work_root or Path(mkdtemp(prefix="erdos-pipeline-"))
        self._registry = registry
        self._operations = operations
        self._solve_llm = solve_llm
        self._tool_mode = tool_mode
        self._delta_sink = delta_sink

    async def process(self, task_id: str, stage: str) -> dict[str, Any]:
        data: dict[str, Any]
        if stage == "analysis":
            data = await self._analysis(task_id)
        elif stage == "modeling":
            data = await self._modeling(task_id)
        elif stage == "solving":
            data = await self._solving(task_id)
        elif stage == "writing":
            data = await self._writing(task_id)
        else:
            raise RuntimeError(f"未知阶段：{stage}")
        if self._sink is not None:
            await self._sink(task_id, stage, data)
        return data

    # ------------------------------------------------------------------
    async def _analysis(self, task_id: str) -> dict[str, Any]:
        reply = await self._llm([{"role": "user", "content": f"任务 {task_id}：提取要点与约束"}], "analysis")
        return {
            "stage": "analysis",
            "insights": reply["content"].split("；"),
            "question_focused": True,
            "usage": reply["usage"],
        }

    async def _modeling(self, task_id: str) -> dict[str, Any]:
        reply = await self._llm([{"role": "user", "content": f"任务 {task_id}：建立模型"}], "modeling")
        return {
            "stage": "modeling",
            "assumptions": reply["content"].split("。")[0],
            "objective": "最小二乘线性拟合（演示）",
            "variables": ["slope", "intercept"],
            "usage": reply["usage"],
        }

    async def _solving(self, task_id: str) -> dict[str, Any]:
        registry = self._registry
        solve_llm = self._solve_llm
        operations = self._operations
        if registry is not None and solve_llm is not None and operations is not None \
                and self._tool_mode == "tool_loop":
            return await self._solving_tool_loop(task_id, registry, solve_llm, operations)
        # 阶段级路径（tool_mode=stage_level：无工具能力端点降级，行为与 SP1-7 一致）
        if self._sandbox is None:
            raise RuntimeError("solving 需要注入沙箱（SP1-4 SubprocessSandbox）")
        work_dir = self._work_root / task_id
        result = await self._sandbox.execute(_SOLVE_SCRIPT, {}, work_dir)
        reply = await self._llm([{"role": "user", "content": f"任务 {task_id}：复核求解结果"}], "solving")
        return {
            "stage": "solving",
            "exit_code": result.exit_code,
            "stdout": result.stdout.strip(),
            "timed_out": result.timed_out,
            "artifacts": result.artifacts,
            "usage": reply["usage"],
        }

    async def _solving_tool_loop(self, task_id: str, registry: Any, solve_llm: Any, operations: Any) -> dict[str, Any]:
        """W11：模型驱动工具循环（decide → dispatch → finalize，副作用幂等）。"""
        loop = SolveLoop(
            llm=solve_llm,
            registry=registry,
            operations=operations,
            task_id=task_id,
            work_root=self._work_root,
            delta_sink=self._delta_sink,
        )
        outcome = await loop.run(f"任务 {task_id}：完成求解并输出结构化结果")
        usage = outcome.get("usage", {})
        return {
            "stage": "solving",
            "mode": "tool_loop",
            "status": outcome["status"],
            "results": outcome["results"],
            "repair_count": outcome["repair_count"],
            "dispatch_count": outcome["dispatch_count"],
            "limitations": outcome.get("limitations", []),
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
            },
        }

    async def _writing(self, task_id: str) -> dict[str, Any]:
        reply = await self._llm([{"role": "user", "content": f"任务 {task_id}：撰写论文草稿"}], "writing")
        paper = (
            f"# {task_id} 论文草稿\n\n"
            f"- 摘要：基于数据拟合并验证线性模型（演示流程）。\n"
            f"- 模型：y = ax + b，最小二乘求解。\n"
            f"- 结论：{reply['content']}\n"
        )
        # 产物落盘 + sha256（SP1-6 留痕 artifact 支撑材料）
        out_dir = self._work_root / task_id
        out_dir.mkdir(parents=True, exist_ok=True)
        paper_path = out_dir / "paper.md"
        paper_path.write_text(paper, encoding="utf-8")
        digest = sha256(paper.encode("utf-8")).hexdigest()
        return {
            "stage": "writing",
            "paper_md": paper,
            "paper_sha256": digest,
            "paper_path": str(paper_path),
            "usage": reply["usage"],
        }