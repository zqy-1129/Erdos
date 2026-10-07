"""四阶段端到端流水线（SP1-7 系统测试支撑；SP1-3/4/5/6 组件接线）。

- LLM 可注入：真实 OpenAI 兼容适配（SP1-5）或 FakeLLM（离线确定性，测试/演示）；
- solving 阶段经沙箱执行（SP1-4 SubprocessSandbox：超时强杀/环境隔离/路径白名单）；
- 各阶段产出结构化 data，writing 产物落盘并计算 sha256（SP1-6 留痕 artifact 接线）；
- 作为 LangGraph 编排器的 StageRunner 注入（run_current_stage 执行真实逻辑）。
红线：FakeLLM 为确定性假模型，仅用于测试/演示，真实任务必须注入真实适配器。
"""

import json
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


def _extract_section(text: str, name: str) -> str:
    """从 LLM 输出提取【name】标记的段落；缺失时回退原文（不伪造章节内容）。"""
    marker = f"【{name}】"
    if marker in text:
        section = text.split(marker, 1)[1]
        for other in ("【摘要】", "【结论】"):
            if other in section and other != marker:
                section = section.split(other, 1)[0]
        return section.strip()
    return text.strip()


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
        task_inputs: dict[str, dict[str, str]] | None = None,  # EN-PAPER：state.tasks 引用
        state_loader: Callable[[str], dict[str, dict[str, Any]]] | None = None,  # 断点恢复上下文
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
        self._task_inputs = task_inputs if task_inputs is not None else {}
        # 断点恢复：进程重启后 _history 为空，经 state_loader 从检查点水合
        # （task_id → {stage: data}）；内存已有数据优先（reject 重跑不被旧值污染）。
        self._state_loader = state_loader
        self._history: dict[str, dict[str, Any]] = {}  # 跨阶段产物上下文（题面贯通）

    async def process(self, task_id: str, stage: str) -> dict[str, Any]:
        data: dict[str, Any]
        if self._state_loader is not None:
            for done_stage, record in self._state_loader(task_id).items():
                self._history.setdefault(task_id, {}).setdefault(done_stage, record)
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
        self._history.setdefault(task_id, {})[stage] = data
        if self._sink is not None:
            await self._sink(task_id, stage, data)
        return data

    # ------------------------------------------------------------------
    # EN-PAPER：题面贯通
    # ------------------------------------------------------------------
    def _problem(self, task_id: str) -> str:
        """题面全文（task_create 登记）；未登记为空（无题面模式）。"""
        info = self._task_inputs.get(task_id) or {}
        return str(info.get("problem_text", ""))

    def _title(self, task_id: str) -> str:
        info = self._task_inputs.get(task_id) or {}
        return str(info.get("title", task_id))

    @staticmethod
    def _clip(text: str, limit: int = 12000) -> str:
        return text if len(text) <= limit else text[:limit] + "\n…（题面过长已截断）"

    def _problem_block(self, task_id: str) -> str:
        """题面块（供 prompt 注入）：有题面给全文（截断），无题面给旧式占位。"""
        problem = self._problem(task_id)
        return f"【竞赛题目】\n{self._clip(problem)}" if problem else f"任务 {task_id}"

    # ------------------------------------------------------------------
    async def _analysis(self, task_id: str) -> dict[str, Any]:
        prompt = (
            f"{self._problem_block(task_id)}\n\n"
            "请完成题目分析：1) 提取决策变量、目标与约束；2) 拆分子问题；"
            "3) 指出数据需求与缺失信息。用分号分隔要点。"
        )
        reply = await self._llm([{"role": "user", "content": prompt}], "analysis")
        return {
            "stage": "analysis",
            "insights": reply["content"].split("；"),
            "question_focused": True,
            "model": reply.get("model", "unknown"),
            "usage": reply["usage"],
        }

    async def _modeling(self, task_id: str) -> dict[str, Any]:
        analysis = (self._history.get(task_id, {}).get("analysis") or {}).get("insights")
        analysis_text = "；".join(analysis) if analysis else "（无上游分析）"
        prompt = (
            f"{self._problem_block(task_id)}\n\n"
            f"【上游分析】\n{analysis_text}\n\n"
            "请建立数学模型：给出模型假设、目标函数/方程与变量说明，并说明求解思路。"
        )
        reply = await self._llm([{"role": "user", "content": prompt}], "modeling")
        return {
            "stage": "modeling",
            "assumptions": reply["content"].split("。")[0],
            "objective": reply["content"].split("。")[1] if "。" in reply["content"] else reply["content"],
            "modeling_detail": reply["content"],
            "variables": ["slope", "intercept"],
            "model": reply.get("model", "unknown"),
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
        prompt = (
            f"{self._problem_block(task_id)}\n\n"
            f"【阶段级求解输出】\n{result.stdout.strip()}\n\n请复核求解结果是否合理。"
        )
        reply = await self._llm([{"role": "user", "content": prompt}], "solving")
        return {
            "stage": "solving",
            "exit_code": result.exit_code,
            "stdout": result.stdout.strip(),
            "timed_out": result.timed_out,
            "artifacts": result.artifacts,
            "model": reply.get("model", "unknown"),
            "usage": reply["usage"],
        }

    async def _solving_tool_loop(self, task_id: str, registry: Any, solve_llm: Any, operations: Any) -> dict[str, Any]:
        """W11：模型驱动工具循环（decide → dispatch → finalize，副作用幂等）。"""
        modeling = self._history.get(task_id, {}).get("modeling") or {}
        task_prompt = (
            f"{self._problem_block(task_id)}\n\n"
            f"【建模方案】\n{modeling.get('modeling_detail', '（无上游建模）')}\n\n"
            "请基于以上方案完成求解：用 execute_code 执行计算/绘图，"
            "最终输出结构化 results（与建模方案对应）。"
        )
        loop = SolveLoop(
            llm=solve_llm,
            registry=registry,
            operations=operations,
            task_id=task_id,
            work_root=self._work_root,
            delta_sink=self._delta_sink,
        )
        outcome = await loop.run(task_prompt)
        usage = outcome.get("usage", {})
        return {
            "stage": "solving",
            "mode": "tool_loop",
            "status": outcome["status"],
            "results": outcome["results"],
            "repair_count": outcome["repair_count"],
            "dispatch_count": outcome["dispatch_count"],
            "limitations": outcome.get("limitations", []),
            # 模型名在适配器层（内循环多调用），管线层不可得；留痕以 unknown 登记
            "model": "unknown",
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
            },
        }

    async def _writing(self, task_id: str) -> dict[str, Any]:
        """EN-PAPER：论文组装——题面 + 上游产物结构化成文，LLM 负责摘要与结论。

        引用真实产物数值（不重新计算）；产物 paper.md 落盘 + sha256（SP1-6 留痕）。
        LaTeX/Word 导出属后续增量（开发文档 §8.5），当前交付 Markdown 草稿。
        """
        history = self._history.get(task_id, {})
        analysis = history.get("analysis") or {}
        modeling = history.get("modeling") or {}
        solving = history.get("solving") or {}
        problem = self._problem(task_id) or "（未提供题面）"
        title = self._title(task_id)

        prompt = (
            f"{self._problem_block(task_id)}\n\n"
            f"【分析】\n{'；'.join(analysis.get('insights', []))}\n\n"
            f"【建模】\n{modeling.get('modeling_detail', '')}\n\n"
            f"【求解结果】\n{json.dumps(solving.get('results') or solving.get('stdout') or '', ensure_ascii=False)}\n\n"
            "请为论文撰写以下两部分，用【摘要】与【结论】标记：\n"
            "【摘要】300 字以内，概括问题、方法与主要结果；\n"
            "【结论】总结结果意义、局限与改进方向，不得引用不存在的数值。"
        )
        reply = await self._llm([{"role": "user", "content": prompt}], "writing")
        abstract = _extract_section(reply["content"], "摘要")
        conclusion = _extract_section(reply["content"], "结论")

        results_text = solving.get("stdout") or json.dumps(
            solving.get("results") or [], ensure_ascii=False
        )
        paper = (
            f"# {title}\n\n"
            f"## 摘要\n\n{abstract}\n\n"
            f"## 一、问题重述\n\n{self._clip(problem, 4000)}\n\n"
            f"## 二、模型假设与建模\n\n"
            f"分析要点：{'；'.join(analysis.get('insights', [])) or '（无）'}\n\n"
            f"{modeling.get('modeling_detail', modeling.get('assumptions', '（无）'))}\n\n"
            f"## 三、求解与结果\n\n"
            f"求解方式：{'工具循环' if solving.get('mode') == 'tool_loop' else '阶段级执行'}\n\n"
            f"```\n{results_text}\n```\n\n"
            f"## 四、结论\n\n{conclusion}\n\n"
            f"## 五、结果局限\n\n"
            + "\n".join(f"- {x}" for x in solving.get("limitations", []) or ["无"])
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
            "model": reply.get("model", "unknown"),
            "usage": reply["usage"],
        }