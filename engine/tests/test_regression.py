"""真题回归验收测试（SP1-7 验收方案口径）。

覆盖：题集规模与分层（≥20 题、4 类题型）、冻结 manifest 确定性、分层抽样、
FakeLLM 基线通道全通过、通道分离统计（DEC-024）、kill/恢复副作用不重放
（DEC-005）、门禁/异常失败归因、证据落盘与 Markdown 报告。
"""

import json
from pathlib import Path

import pytest

from engine.gates.evaluator import Evaluator
from engine.gates.schema import Dimension, Rubric
from engine.orchestrator.graph import STAGES
from engine.regression.evidence import EvidenceWriter, render_markdown
from engine.regression.flow import FakeLLMFlow
from engine.regression.problems import (
    CATEGORIES,
    REGRESSION_SET,
    RegressionProblem,
    category_distribution,
    manifest,
    stratified_sample,
)
from engine.regression.runner import (
    CHANNEL_BASELINE,
    CHANNEL_REAL,
    AcceptanceReport,
    AcceptanceRunner,
    FailureModule,
    RubricGatePolicy,
)


class FailEvaluator(Evaluator):
    """门禁总是不通过的评委。"""

    async def evaluate(self, stage, artifact, rubric) -> str:
        return json.dumps({
            "scores": [{"dimension": d.name, "score": 0.1, "reason": "bad"} for d in rubric.dimensions],
        })


def _rubrics() -> dict[str, Rubric]:
    return {
        stage: Rubric(
            version=1, stage=stage, threshold=0.7,
            dimensions=(Dimension("维度1", 0.5, ""), Dimension("维度2", 0.5, "")),
        )
        for stage in STAGES
    }


def _flow_factory(root: Path, **flow_kwargs):
    """FakeLLM 任务流工厂（每题独立检查点库与工作目录，支持 kill/恢复）。"""

    def build(problem: RegressionProblem) -> FakeLLMFlow:
        return FakeLLMFlow(
            task_id=f"reg-{problem.business_id}",
            title=problem.title,
            problem_text=problem.statement,
            checkpoint_path=root / "ckpt" / f"{problem.business_id}.db",
            work_root=root / "work" / problem.business_id,
            **flow_kwargs,
        )

    return build


# ----------------------------------------------------------------------
# 题集与冻结工具
# ----------------------------------------------------------------------

def test_regression_set_covers_20_problems_4_categories() -> None:
    """回归集 ≥20 题覆盖 4 类题型，每类 ≥4 题（验收方案 §2 口径）。"""
    assert len(REGRESSION_SET) >= 20
    dist = category_distribution()
    assert set(dist) == set(CATEGORIES)
    assert min(dist.values()) >= 4
    ids = [p.business_id for p in REGRESSION_SET]
    assert len(ids) == len(set(ids))  # business_id 唯一
    for problem in REGRESSION_SET:
        assert problem.title and problem.statement and problem.reference_digest
        assert problem.difficulty in ("basic", "advanced")
        assert problem.year >= 2012


def test_manifest_is_deterministic_and_content_sensitive() -> None:
    """冻结 manifest 确定性（同集合同哈希）且对内容变更敏感。"""
    assert manifest() == manifest()
    full = manifest()
    assert full["count"] == len(REGRESSION_SET)
    assert len(full["set_sha256"]) == 64
    assert all(len(entry["sha256"]) == 64 for entry in full["problems"])

    original = REGRESSION_SET[0]
    altered = RegressionProblem(
        original.business_id, original.category, original.title,
        original.statement + "（内容变更）", original.reference_digest,
        original.year, original.difficulty,
    )
    assert manifest((altered,))["problems"][0]["sha256"] != \
        manifest((original,))["problems"][0]["sha256"]


def test_stratified_sample_per_category() -> None:
    """分层抽样：每类题型取前 N 道；None 返回全量。"""
    half = stratified_sample(2)
    assert len(half) == 8
    assert set(category_distribution(half).values()) == {2}
    assert stratified_sample(None) == REGRESSION_SET


# ----------------------------------------------------------------------
# 运行器：基线通道 / 通道分离 / 决策门
# ----------------------------------------------------------------------

async def test_baseline_channel_full_pass_and_baseline_only_decision(tmp_path) -> None:
    """FakeLLM 基线通道全通过；无真实通道结果时决策门为 BASELINE_ONLY（DEC-024）。"""
    problem = REGRESSION_SET[0]
    runner = AcceptanceRunner(_flow_factory(tmp_path), channel=CHANNEL_BASELINE)
    report = await runner.run_all((problem,))

    result = report.results[0]
    assert result.passed and result.failure_module is None
    assert [record.stage for record in result.stage_records] == list(STAGES)
    assert result.prompt_tokens > 0 and result.completion_tokens > 0
    assert result.paper_sha256 and len(result.paper_sha256) == 64
    assert result.executed_stages == STAGES
    assert result.resumed is False

    assert report.success_rate(CHANNEL_BASELINE) == 1.0
    assert report.scoped(CHANNEL_REAL) == []
    assert report.decision().verdict == "BASELINE_ONLY"


async def test_channel_separation_stats_and_no_go(tmp_path) -> None:
    """通道分离统计（DEC-024）：真实通道失败不污染基线统计，决策门 NO-GO 带归因。"""
    problem = REGRESSION_SET[0]
    baseline = await AcceptanceRunner(
        _flow_factory(tmp_path), channel=CHANNEL_BASELINE,
    ).run_all((problem,))
    real = await AcceptanceRunner(
        _flow_factory(tmp_path),
        gate_policy=RubricGatePolicy(FailEvaluator(), _rubrics()),
        channel=CHANNEL_REAL,
    ).run_all((problem,))

    merged = AcceptanceReport(results=[*baseline.results, *real.results])
    assert merged.success_rate(CHANNEL_BASELINE) == 1.0
    assert merged.success_rate(CHANNEL_REAL) == 0.0
    assert merged.failure_by_module(CHANNEL_REAL) == {FailureModule.GATE: 1}
    assert merged.failure_by_module(CHANNEL_BASELINE) == {}

    decision = merged.decision()
    assert decision.verdict == "NO-GO"
    assert any("成功率" in reason for reason in decision.reasons)


async def test_kill_resume_no_side_effect_replay(tmp_path) -> None:
    """断点恢复演练（DEC-005）：modeling 后 kill，恢复续跑且已成功副作用不重放。"""
    problem = next(p for p in REGRESSION_SET if p.category == "prediction")
    runner = AcceptanceRunner(_flow_factory(tmp_path), channel=CHANNEL_BASELINE)
    result = await runner.run_one(problem, kill_after_stage="modeling")

    assert result.passed and result.resumed and result.kill_after_stage == "modeling"
    assert result.executed_stages == STAGES  # 每阶段恰执行一次
    assert len(set(result.executed_stages)) == len(result.executed_stages)
    assert result.paper_sha256 and len(result.paper_sha256) == 64


async def test_gate_reject_attributes_to_gate(tmp_path) -> None:
    """门禁评审 reject：归因 gate 模块，明细含失败阶段。"""
    runner = AcceptanceRunner(
        _flow_factory(tmp_path),
        gate_policy=RubricGatePolicy(FailEvaluator(), _rubrics()),
        channel=CHANNEL_BASELINE,
    )
    report = await runner.run_all(stratified_sample(1)[:2])
    assert len(report.results) == 2
    for result in report.results:
        assert not result.passed
        assert result.failure_module == FailureModule.GATE
        assert "门禁评审未通过" in result.failure_detail
        assert result.stages_passed == 0


async def test_exception_classified_to_adapter_and_sandbox(tmp_path) -> None:
    """异常分层归因：模型连接失败 → adapter；沙箱超时 → sandbox。"""

    async def broken_llm(messages: list[dict[str, str]], stage: str) -> dict:
        raise ConnectionError("厂商不可达（模拟）")

    class ExplodingSandbox:
        async def execute(self, code, files, work_dir):  # noqa: ANN001 - 测试替身
            raise TimeoutError("沙箱强杀（模拟）")

    adapter_runner = AcceptanceRunner(_flow_factory(tmp_path / "a", llm=broken_llm))
    adapter_result = await adapter_runner.run_one(REGRESSION_SET[0])
    assert not adapter_result.passed
    assert adapter_result.failure_module == FailureModule.ADAPTER
    assert "ConnectionError" in adapter_result.failure_detail

    sandbox_runner = AcceptanceRunner(
        _flow_factory(tmp_path / "b", sandbox=ExplodingSandbox())
    )
    sandbox_result = await sandbox_runner.run_one(REGRESSION_SET[0])
    assert not sandbox_result.passed
    assert sandbox_result.failure_module == FailureModule.SANDBOX


async def test_flow_stage_order_guard(tmp_path) -> None:
    """任务流阶段顺序约束：跳级请求被拒（禁跳级红线）。"""
    flow = FakeLLMFlow(
        task_id="reg-order", title="顺序", problem_text="题面",
        checkpoint_path=tmp_path / "ckpt.db", work_root=tmp_path / "work",
    )
    with pytest.raises(ValueError, match="阶段顺序约束"):
        await flow.run_stage("modeling")  # analysis 未完成


# ----------------------------------------------------------------------
# 证据落盘与报告
# ----------------------------------------------------------------------

async def test_evidence_writer_and_markdown_report(tmp_path) -> None:
    """证据落盘：逐题 JSON + summary.json + report.md（SP1-7 §5 证据归档）。"""
    evidence = EvidenceWriter(tmp_path / "evidence")
    problem = REGRESSION_SET[0]
    report = await AcceptanceRunner(
        _flow_factory(tmp_path), channel=CHANNEL_BASELINE,
    ).run_all((problem,), evidence=evidence)

    result_file = tmp_path / "evidence" / f"{problem.business_id}.json"
    payload = json.loads(result_file.read_text(encoding="utf-8"))
    assert payload["problem"]["business_id"] == problem.business_id
    assert payload["result"]["channel"] == CHANNEL_BASELINE
    assert payload["result"]["paper_sha256"]

    summary_path, report_path = evidence.write_summary(report)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["total"] == 1 and summary["passed"] == 1
    assert summary["success_rate_baseline"] == 1.0

    markdown = report_path.read_text(encoding="utf-8")
    assert "## 2. 题型分布" in markdown
    assert "## 3. 失败归因" in markdown
    assert problem.business_id in markdown
    assert "成功率" in markdown


def test_render_markdown_baseline_only_note() -> None:
    """基线报告渲染：无结果时决策门结论为 BASELINE_ONLY 说明。"""
    from engine.regression.runner import AcceptanceReport

    markdown = render_markdown(AcceptanceReport(), title="空报告")
    assert "BASELINE_ONLY" in markdown
    assert "空报告" in markdown


# ----------------------------------------------------------------------
# 判定通道的门禁硬化（S6-2）：rubric 分数入证据、硬检查归因到 gate
# ----------------------------------------------------------------------


class SpyEvaluator:
    """按 rubric 全维度给同一分数的评委替身，并记录被投喂的产物。"""

    def __init__(self, score: float = 0.9) -> None:
        self.score = score
        self.seen: list[tuple[str, dict]] = []

    async def evaluate(self, stage, artifact, rubric) -> str:
        self.seen.append((stage, artifact))
        return json.dumps({
            "scores": [
                {"dimension": d.name, "score": self.score, "reason": "ok"}
                for d in rubric.dimensions
            ]
        })


class _FailedSandbox:
    """沙箱执行失败替身：exit_code!=0 且 stdout/artifacts 同时为空（触发 SP1-3 硬检查）。"""

    def __init__(self) -> None:
        from engine.sandbox.base import ExecutionResult

        self._result = ExecutionResult(exit_code=1, stdout="", stderr="boom", artifacts=[])

    async def execute(self, code, files, work_dir):  # noqa: ANN001 - Sandbox 端口替身
        return self._result


async def test_rubric_gate_scores_recorded_per_stage(tmp_path) -> None:
    """判定通道硬条件「门禁通过」需可举证：逐阶段 rubric 分数入结果。"""
    spy = SpyEvaluator()
    runner = AcceptanceRunner(
        _flow_factory(tmp_path),
        gate_policy=RubricGatePolicy(spy, _rubrics()),
        channel=CHANNEL_REAL,
    )
    result = await runner.run_one(REGRESSION_SET[0])

    assert result.passed
    assert [g.stage for g in result.gate_scores] == list(STAGES)
    assert all(g.passed and g.attempts == 1 for g in result.gate_scores)
    assert result.gate_scores[0].total_score == pytest.approx(0.9)
    assert result.gate_scores[0].threshold == 0.7


async def test_rubric_policy_feeds_problem_statement_to_judge(tmp_path) -> None:
    """评委必须看到题面：rubric 有「问题理解」维度，无题面即无从判定（不得盲评）。"""
    spy = SpyEvaluator()
    problem = REGRESSION_SET[0]
    runner = AcceptanceRunner(
        _flow_factory(tmp_path), gate_policy=RubricGatePolicy(spy, _rubrics())
    )
    await runner.run_one(problem)

    stage, artifact = spy.seen[0]
    assert stage == "analysis"
    assert artifact["problem_text"] == problem.statement
    assert artifact["title"] == problem.title


async def test_hard_check_reject_attributes_to_gate_not_orchestrator(tmp_path) -> None:
    """硬检查自动驳回后不得记成编排故障：runner 补 pass 会与已消费的门禁冲突。

    原实现下该场景以 ValueError("门禁冲突…") 冒出来，归因落在 orchestrator，
    SP1-7 的失败分层归因因此失真（门禁问题被算进编排器）。
    """
    runner = AcceptanceRunner(_flow_factory(tmp_path, sandbox=_FailedSandbox()))
    result = await runner.run_one(REGRESSION_SET[0])

    assert not result.passed
    assert result.failure_module == FailureModule.GATE
    assert "硬检查不通过" in result.failure_detail
    assert "solving" in result.failure_detail
    assert result.stages_passed == 2


async def test_evidence_carries_gate_scores_and_extra(tmp_path) -> None:
    """证据面：逐题 JSON 含 rubric 分数，summary 可附带评审口径与评委用量。"""
    spy = SpyEvaluator()
    evidence = EvidenceWriter(tmp_path / "evidence")
    problem = REGRESSION_SET[0]
    report = await AcceptanceRunner(
        _flow_factory(tmp_path), gate_policy=RubricGatePolicy(spy, _rubrics())
    ).run_all((problem,), evidence=evidence)

    payload = json.loads(
        (tmp_path / "evidence" / f"{problem.business_id}.json").read_text(encoding="utf-8")
    )
    assert payload["result"]["gate_scores"][0]["stage"] == "analysis"

    summary_path, report_path = evidence.write_summary(
        report, title="SP1-7 回归报告", extra={"gate_mode": "rubric", "judge_usage": {"calls": 4}}
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["gate_mode"] == "rubric"
    assert summary["judge_usage"]["calls"] == 4

    markdown = report_path.read_text(encoding="utf-8")
    assert "门禁评分" in markdown
    assert "rubric 评审明细" in markdown

