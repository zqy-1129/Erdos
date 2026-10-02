"""验收运行器（SP1-7）：逐题跑四阶段 + 门禁判定 + 成功率统计 + 失败归因。

判定标准（对齐 SP1-7 提示词）：
- 论文草稿产出即成功（四阶段跑完）；
- 门禁通过为硬条件（任一阶段门禁失败即该题失败）。
"""

from dataclasses import dataclass, field

from engine.gates.evaluator import Evaluator, GateRunner
from engine.gates.schema import Rubric
from engine.orchestrator.graph import STAGES, StageOrchestrator


# 失败归因模块（分层定位）
class FailureModule:
    ORCHESTRATOR = "orchestrator"  # 编排器（阶段推进/顺序）
    GATE = "gate"  # 门禁（评审不通过）
    SANDBOX = "sandbox"  # 沙箱（执行失败）
    ADAPTER = "adapter"  # 适配器（模型调用失败）


@dataclass(frozen=True, slots=True)
class ProblemResult:
    """单题验收结果。"""

    business_id: str
    category: str
    passed: bool
    failure_module: str | None
    stages_passed: int


@dataclass(slots=True)
class AcceptanceReport:
    """验收报告。"""

    total: int = 0
    passed: int = 0
    results: list[ProblemResult] = field(default_factory=list)
    failure_by_module: dict[str, int] = field(default_factory=dict)
    failure_by_stage: dict[str, int] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        return round(self.passed / self.total, 4) if self.total else 0.0

    def decision(self, threshold: float = 0.85) -> str:
        """决策门结论：成功率 ≥ 阈值锁 MVP，否则触发风险响应。"""
        return "锁 MVP" if self.success_rate >= threshold else "触发风险 R1 响应"


class AcceptanceRunner:
    """逐题跑四阶段的验收运行器。"""

    def __init__(self, evaluator: Evaluator, rubrics: dict[str, Rubric]) -> None:
        self._evaluator = evaluator
        self._rubrics = rubrics

    async def run_all(self, problems) -> AcceptanceReport:
        """逐题跑四阶段，统计成功率与失败归因。"""
        report = AcceptanceReport(total=len(problems))
        for problem in problems:
            result = await self._run_one(problem)
            report.results.append(result)
            if result.passed:
                report.passed += 1
            else:
                mod = result.failure_module or FailureModule.ORCHESTRATOR
                report.failure_by_module[mod] = report.failure_by_module.get(mod, 0) + 1
        return report

    async def _run_one(self, problem) -> ProblemResult:
        """跑单题四阶段；失败归因到模块。"""
        orchestrator = StageOrchestrator(problem.business_id)
        try:
            for stage in STAGES:
                # 1. 执行阶段（骨架：产出空 data）
                await orchestrator.run_current_stage()
                # 2. 门禁评审（硬条件）
                rubric = self._rubrics[stage]
                gate = GateRunner(self._evaluator)
                gate_result = await gate.run(stage, {}, rubric)
                if not gate_result.passed:
                    # 门禁失败：记录失败阶段
                    return ProblemResult(
                        business_id=problem.business_id, category=problem.category,
                        passed=False, failure_module=FailureModule.GATE,
                        stages_passed=STAGES.index(stage),
                    )
                # 3. 门禁通过，进入下一阶段
                await orchestrator.answer_gate("pass")
            # 四阶段全通过：论文草稿产出
            return ProblemResult(
                business_id=problem.business_id, category=problem.category,
                passed=True, failure_module=None, stages_passed=4,
            )
        except Exception as exc:  # noqa: BLE001 - 分层归因
            # 失败归因：编排/沙箱/适配器异常
            return ProblemResult(
                business_id=problem.business_id, category=problem.category,
                passed=False, failure_module=_classify_exception(exc),
                stages_passed=0,
            )


def _classify_exception(exc: Exception) -> str:
    """异常分类到模块（编排/沙箱/适配器）。"""
    name = type(exc).__name__
    if "Sandbox" in name or "Timeout" in name:
        return FailureModule.SANDBOX
    if "Adapter" in name or "Network" in name or "Connection" in name or "Http" in name:
        return FailureModule.ADAPTER
    return FailureModule.ORCHESTRATOR
