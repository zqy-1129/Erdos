"""验收运行器（SP1-7）：通道分离统计 + 门禁策略 + 失败归因 + 断点恢复演练。

对齐《SP1-7 MVP 集成验收方案》（2026-10-06）：
- 通道分离（DEC-024）：FakeLLM 通道仅作回归护栏，不计入 ≥85% 判定分母；
  decision() 默认只对真实 Key 通道出 Go/No-Go，无真实通道结果时 BASELINE_ONLY；
- 硬条件：端到端成功率 ≥ 阈值、每类题型至少 1 题成功、失败样本 100% 归因；
- 断点恢复（DEC-005）：kill_after_stage 指定阶段完成后模拟进程 kill，从检查点
  重建任务流续跑，已成功副作用不重放（executed_stages 可断言）；
- 失败归因：FailureModule 四分类（编排/门禁/沙箱/适配器），归因明细入结果。
"""

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from engine.gates.evaluator import Evaluator, GateRunner
from engine.gates.schema import Rubric
from engine.orchestrator.graph import STAGES
from engine.regression.flow import FlowFactory
from engine.regression.problems import RegressionProblem

if TYPE_CHECKING:
    from engine.regression.evidence import EvidenceWriter

CHANNEL_BASELINE = "fakellm"  # 离线回归护栏通道（不计判定分母）
CHANNEL_REAL = "real_key"  # 真实 Key 通道（判定通道）


class FailureModule:
    """失败归因模块（分层定位）。"""

    ORCHESTRATOR = "orchestrator"  # 编排器（阶段推进/顺序）
    GATE = "gate"  # 门禁（评审不通过）
    SANDBOX = "sandbox"  # 沙箱（执行失败/超时）
    ADAPTER = "adapter"  # 适配器（模型调用失败）


@dataclass(frozen=True, slots=True)
class StageRecord:
    """单阶段执行记录（耗时 + 用量）。"""

    stage: str
    duration_ms: float
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True, slots=True)
class GateScore:
    """一次阶段 rubric 评审结论（SP1-7「门禁通过为硬条件」的可举证依据）。"""

    stage: str
    total_score: float
    threshold: float
    passed: bool
    attempts: int  # 评委被叫次数（≤3，超限转人工口径）


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """策略裁决：pass/reject + rubric 分数（自动通过策略无分数）。"""

    decision: str
    score: GateScore | None = None


@dataclass(frozen=True, slots=True)
class ProblemResult:
    """单题验收结果（含逐阶段记录与恢复标记）。"""

    business_id: str
    category: str
    channel: str
    passed: bool
    failure_module: str | None
    failure_detail: str
    stages_passed: int
    stage_records: tuple[StageRecord, ...]
    prompt_tokens: int
    completion_tokens: int
    paper_sha256: str | None
    resumed: bool
    kill_after_stage: str | None
    executed_stages: tuple[str, ...]
    gate_scores: tuple[GateScore, ...] = ()

    @property
    def duration_ms(self) -> float:
        return round(sum(r.duration_ms for r in self.stage_records), 1)


@dataclass(slots=True)
class GateDecision:
    """决策门结论：GO / NO-GO / BASELINE_ONLY（无判定通道结果）。"""

    verdict: str
    reasons: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        detail = "；".join(self.reasons) if self.reasons else "全部硬条件满足"
        return f"{self.verdict}（{detail}）"


class GatePolicy(Protocol):
    """门禁决策策略端口：运行器经此决定 pass/reject（策略可注入）。"""

    async def decide(
        self, problem: RegressionProblem, stage: str, data: dict[str, Any]
    ) -> GateOutcome: ...


class AutoPassGatePolicy:
    """自动通过策略（FakeLLM 基线语义，对齐 run_paper_e2e 的门禁自动通过）。

    无 rubric 分数：基线护栏只验证链路确定性，不计入 SP1-7 判定分母（DEC-024）。
    """

    async def decide(
        self, problem: RegressionProblem, stage: str, data: dict[str, Any]
    ) -> GateOutcome:
        return GateOutcome(decision="pass")


class RubricGatePolicy:
    """SP1-3 门禁评审器驱动：Evaluator + 版本化 Rubric 打分，低于阈值 reject。

    评委必须看到题面：rubric 含「问题理解」类维度，无题面即无从判定，
    故在投喂前把题面并入阶段产物（题面来自回归集，不额外发问）。
    """

    def __init__(self, evaluator: Evaluator, rubrics: dict[str, Rubric]) -> None:
        self._evaluator = evaluator
        self._rubrics = rubrics

    async def decide(
        self, problem: RegressionProblem, stage: str, data: dict[str, Any]
    ) -> GateOutcome:
        rubric = self._rubrics[stage]
        artifact = {
            **data,
            "title": problem.title,
            "problem_text": problem.statement,
        }
        gate = GateRunner(self._evaluator)
        result = await gate.run(stage, artifact, rubric)
        return GateOutcome(
            decision="pass" if result.passed else "reject",
            score=GateScore(
                stage=stage,
                total_score=result.total_score,
                threshold=result.threshold,
                passed=result.passed,
                attempts=gate.retry_count,
            ),
        )


@dataclass(slots=True)
class AcceptanceReport:
    """验收报告：逐题结果 + 通道/题型/归因统计。"""

    results: list[ProblemResult] = field(default_factory=list)

    def scoped(self, channel: str | None = None) -> list[ProblemResult]:
        if channel is None:
            return list(self.results)
        return [r for r in self.results if r.channel == channel]

    def success_rate(self, channel: str | None = None) -> float:
        results = self.scoped(channel)
        if not results:
            return 0.0
        return round(sum(1 for r in results if r.passed) / len(results), 4)

    def by_category(self, channel: str | None = None) -> dict[str, dict[str, int]]:
        stats: dict[str, dict[str, int]] = {}
        for r in self.scoped(channel):
            stat = stats.setdefault(r.category, {"total": 0, "passed": 0})
            stat["total"] += 1
            stat["passed"] += 1 if r.passed else 0
        return stats

    def failure_by_module(self, channel: str | None = None) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.scoped(channel):
            if not r.passed:
                mod = r.failure_module or FailureModule.ORCHESTRATOR
                counts[mod] = counts.get(mod, 0) + 1
        return counts

    def decision(self, threshold: float = 0.85, channel: str = CHANNEL_REAL) -> GateDecision:
        """决策门（SP1-7 §4 Go 条件；默认只对真实 Key 通道判定，DEC-024）。"""
        scoped = self.scoped(channel)
        if not scoped:
            return GateDecision("BASELINE_ONLY", [
                "无真实 Key 通道结果；FakeLLM 基线仅作回归护栏，不计入判定分母（DEC-024）",
            ])
        reasons: list[str] = []
        total = len(scoped)
        passed = sum(1 for r in scoped if r.passed)
        rate = passed / total
        if rate < threshold:
            reasons.append(f"端到端成功率 {rate:.2%} < {threshold:.0%}")
        for category, stat in self.by_category(channel).items():
            if stat["passed"] == 0:
                reasons.append(f"题型 {category} 无成功样本（防单题型偏科）")
        unattributed = [r.business_id for r in scoped if not r.passed and not r.failure_module]
        if unattributed:
            reasons.append(f"存在未归因失败样本：{', '.join(unattributed)}")
        return GateDecision("GO" if not reasons else "NO-GO", reasons)


class AcceptanceRunner:
    """逐题驱动任务流的验收运行器（通道标签 + 门禁策略 + 恢复演练可注入）。"""

    def __init__(
        self,
        flow_factory: FlowFactory,
        gate_policy: GatePolicy | None = None,
        channel: str = CHANNEL_BASELINE,
    ) -> None:
        self._flow_factory = flow_factory
        self._policy: GatePolicy = gate_policy or AutoPassGatePolicy()
        self._channel = channel

    async def run_all(
        self,
        problems: tuple[RegressionProblem, ...] | list[RegressionProblem],
        *,
        kill_plan: dict[str, str] | None = None,
        evidence: "EvidenceWriter | None" = None,
    ) -> AcceptanceReport:
        """逐题跑四阶段；kill_plan 指定 business_id → kill 发生阶段（断点恢复演练）。"""
        plan = kill_plan or {}
        report = AcceptanceReport()
        for problem in problems:
            result = await self.run_one(problem, kill_after_stage=plan.get(problem.business_id))
            report.results.append(result)
            if evidence is not None:
                evidence.write_result(problem, result)
        return report

    async def run_one(
        self, problem: RegressionProblem, *, kill_after_stage: str | None = None
    ) -> ProblemResult:
        """跑单题四阶段；kill_after_stage 阶段完成后模拟进程 kill 并从检查点续跑。"""
        flow = self._flow_factory(problem)
        records: list[StageRecord] = []
        scores: list[GateScore] = []
        prompt_tokens = completion_tokens = 0
        paper_sha: str | None = None
        resumed = False
        executed_pre: tuple[str, ...] = ()  # kill 前旧任务流的执行记录（恢复后拼接去重）

        async def _verdict(stage: str, data: dict[str, Any]) -> tuple[str, str]:
            """本阶段门禁裁决 → (decision, 失败原因)；rubric 分数记入 scores。

            硬检查驳回（SP1-3）优先判定：编排器已按 reject 语义消费掉本次门禁，
            若继续走策略并补 pass，会与「无挂起门禁」冲突而以 ValueError 冒出，
            失败被错记成编排故障——归因失真且原样掩盖了门禁问题。
            """
            hard = flow.hard_reject_reason()
            if hard:
                return "reject", f"门禁驳回（{stage}）：{hard}"
            outcome = await self._policy.decide(problem, stage, data)
            if outcome.score is not None:
                scores.append(outcome.score)
            if outcome.decision != "pass":
                score = outcome.score
                detail = (
                    f"{stage} 门禁评审未通过（加权 {score.total_score:.2f} "
                    f"< 阈值 {score.threshold:.2f}，评委重试 {score.attempts} 次）"
                    if score is not None
                    else f"{stage} 门禁评审未通过（策略裁决 reject）"
                )
                return "reject", detail
            return "pass", ""

        def _result(**overrides: Any) -> ProblemResult:  # noqa: ANN401 - 结果装配收敛
            defaults: dict[str, Any] = {
                "business_id": problem.business_id,
                "category": problem.category,
                "channel": self._channel,
                "passed": True,
                "failure_module": None,
                "failure_detail": "",
                "stages_passed": len(STAGES),
                "stage_records": tuple(records),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "paper_sha256": paper_sha,
                "resumed": resumed,
                "kill_after_stage": kill_after_stage,
                "executed_stages": executed_pre + flow.executed_stages(),
                "gate_scores": tuple(scores),
            }
            defaults.update(overrides)
            return ProblemResult(**defaults)

        index = STAGES.index(flow.current_stage())
        try:
            while index < len(STAGES):
                stage = STAGES[index]
                started = time.perf_counter()
                data = await flow.run_stage(stage)
                elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                usage = data.get("usage") or {}
                stage_prompt = int(usage.get("prompt_tokens", 0) or 0)
                stage_completion = int(usage.get("completion_tokens", 0) or 0)
                records.append(StageRecord(stage, elapsed_ms, stage_prompt, stage_completion))
                prompt_tokens += stage_prompt
                completion_tokens += stage_completion
                if stage == "writing":
                    paper_sha = data.get("paper_sha256")

                if kill_after_stage == stage and index < len(STAGES) - 1:
                    # 模拟进程 kill：硬杀任务流（RPC 驱动即杀引擎进程），从检查点重建续跑
                    executed_pre = flow.executed_stages()
                    flow.close(force=True)
                    flow = self._flow_factory(problem)
                    restored = flow.current_stage()
                    resumed = True
                    if restored == STAGES[index + 1]:
                        # kill 落在门禁应答之后：门禁已过，直接续跑下一阶段
                        index += 1
                        continue
                    if restored != stage:
                        return _result(
                            passed=False, failure_module=FailureModule.ORCHESTRATOR,
                            failure_detail=(
                                f"恢复定位失败：期望 {stage} 或 {STAGES[index + 1]}，实际 {restored}"
                            ),
                            stages_passed=index,
                        )
                    # kill 落在门禁未决窗口（本运行器 kill 时序的预期形态）：
                    # 恢复后门禁重挂（阶段不重放，DEC-005），补门禁应答后续跑。
                    # 阶段记录/usage 已在 kill 前计入，此处不重复记账。
                    replay = await flow.run_stage(stage)
                    decision, reason = await _verdict(stage, replay)
                    if decision != "pass":
                        return _result(
                            passed=False, failure_module=FailureModule.GATE,
                            failure_detail=reason, stages_passed=index,
                        )
                    await flow.answer_gate(decision)
                    index += 1
                    continue

                decision, reason = await _verdict(stage, data)
                if decision != "pass":
                    return _result(
                        passed=False, failure_module=FailureModule.GATE,
                        failure_detail=reason, stages_passed=index,
                    )
                await flow.answer_gate(decision)
                index += 1
            return _result()
        except Exception as exc:  # noqa: BLE001 - 失败样本不删除，分层归因
            return _result(
                passed=False, failure_module=_classify_exception(exc),
                failure_detail=f"{type(exc).__name__}: {exc}", stages_passed=index,
            )
        finally:
            flow.close()


def _classify_exception(exc: Exception) -> str:
    """异常分类到模块（编排/沙箱/适配器）。"""
    name = type(exc).__name__
    if "Sandbox" in name or "Timeout" in name:
        return FailureModule.SANDBOX
    if "Adapter" in name or "Network" in name or "Connection" in name or "Http" in name:
        return FailureModule.ADAPTER
    return FailureModule.ORCHESTRATOR
