"""门禁评审器单元测试（SP1-3）：解析异常 / 重试上限 / 转人工 3 类 + rubric 可见。"""

import json

from engine.gates.evaluator import GateRunner, _parse_score
from engine.gates.schema import load_rubric_for_stage

RUBRICS_DIR = "engine/gates/rubrics"


class FakeEvaluator:
    """LLM 评委桩：返回预设的评分 JSON 序列。"""

    def __init__(self, outputs: list[str]) -> None:
        self._outputs = outputs
        self.calls = 0

    async def evaluate(self, stage, artifact, rubric) -> str:
        idx = min(self.calls, len(self._outputs) - 1)
        self.calls += 1
        return self._outputs[idx]


def _pass_output() -> str:
    return json.dumps({
        "scores": [
            {"dimension": "问题理解", "score": 0.9, "reason": "理解准确"},
            {"dimension": "数据理解", "score": 0.8, "reason": "数据清晰"},
            {"dimension": "方法可行性", "score": 0.8, "reason": "方法可行"},
        ]
    })


def _fail_output() -> str:
    return json.dumps({
        "scores": [
            {"dimension": "问题理解", "score": 0.4, "reason": "理解偏差"},
            {"dimension": "数据理解", "score": 0.3, "reason": "数据遗漏"},
            {"dimension": "方法可行性", "score": 0.3, "reason": "方法不可行"},
        ]
    })


# ----------------------------------------------------------------------
# 解析异常（解析失败即视为不通过）
# ----------------------------------------------------------------------
async def test_parse_invalid_json_marks_fail() -> None:
    rubric = load_rubric_for_stage("analysis", RUBRICS_DIR)
    result = _parse_score("analysis", "not json", rubric)
    assert result.passed is False
    assert result.total_score == 0.0


async def test_parse_missing_scores_marks_fail() -> None:
    rubric = load_rubric_for_stage("analysis", RUBRICS_DIR)
    result = _parse_score("analysis", '{"foo": 1}', rubric)
    assert result.passed is False


async def test_parse_valid_pass_output() -> None:
    rubric = load_rubric_for_stage("analysis", RUBRICS_DIR)
    result = _parse_score("analysis", _pass_output(), rubric)
    assert result.passed is True
    assert result.total_score >= rubric.threshold


# ----------------------------------------------------------------------
# 重试上限 + 转人工
# ----------------------------------------------------------------------
async def test_retry_passes_on_second_attempt() -> None:
    """第一次不合格，第二次合格：重试 2 次通过。"""
    rubric = load_rubric_for_stage("analysis", RUBRICS_DIR)
    runner = GateRunner(FakeEvaluator([_fail_output(), _pass_output()]))
    result = await runner.run("analysis", {}, rubric)
    assert result.passed is True
    assert runner.retry_count == 2


async def test_retry_exhausted_after_3() -> None:
    """连续不合格 3 次：转人工（retry_count=3，未通过）。"""
    rubric = load_rubric_for_stage("analysis", RUBRICS_DIR)
    runner = GateRunner(FakeEvaluator([_fail_output(), _fail_output(), _fail_output()]))
    result = await runner.run("analysis", {}, rubric)
    assert result.passed is False
    assert runner.retry_count == 3  # 重试达上限


async def test_retry_never_exceeds_3() -> None:
    """重试永不超过 3 次。"""
    rubric = load_rubric_for_stage("analysis", RUBRICS_DIR)
    evaluator = FakeEvaluator([_fail_output()] * 10)
    runner = GateRunner(evaluator)
    await runner.run("analysis", {}, rubric)
    assert runner.retry_count == 3
    assert evaluator.calls == 3  # 评委只被调用了 3 次（不无限重试）


# ----------------------------------------------------------------------
# rubric 可见性 + 4 阶段独立
# ----------------------------------------------------------------------
async def test_4_stage_rubrics_all_loadable() -> None:
    """4 阶段 rubric 均可加载且阈值/维度合法。"""
    for stage in ("analysis", "modeling", "solving", "writing"):
        rubric = load_rubric_for_stage(stage, RUBRICS_DIR)
        assert rubric.stage == stage
        assert len(rubric.dimensions) >= 2
        assert 0 < rubric.threshold <= 1


async def test_rubric_weight_sum_is_1() -> None:
    """rubric 权重和为 1（schema 校验）。"""
    for stage in ("analysis", "modeling", "solving", "writing"):
        rubric = load_rubric_for_stage(stage, RUBRICS_DIR)
        assert abs(sum(d.weight for d in rubric.dimensions) - 1.0) < 1e-6


async def test_modeling_rubric_emphasizes_assumptions() -> None:
    """建模 rubric 侧重假设论证。"""
    rubric = load_rubric_for_stage("modeling", RUBRICS_DIR)
    assumption = next(d for d in rubric.dimensions if d.name == "假设论证")
    assert assumption.weight == max(d.weight for d in rubric.dimensions)


async def test_writing_rubric_emphasizes_charts() -> None:
    """报告 rubric 侧重图表引用。"""
    rubric = load_rubric_for_stage("writing", RUBRICS_DIR)
    charts = next(d for d in rubric.dimensions if d.name == "图表引用")
    assert charts.weight >= 0.3


async def test_5_pass_5_fail_artifacts() -> None:
    """合格/不合格产物各 5 例：重试计数与转人工正确。"""
    rubric = load_rubric_for_stage("analysis", RUBRICS_DIR)
    # 5 例合格：一次通过
    for _ in range(5):
        runner = GateRunner(FakeEvaluator([_pass_output()]))
        result = await runner.run("analysis", {}, rubric)
        assert result.passed is True
        assert runner.retry_count == 1
    # 5 例不合格：3 次重试后转人工
    for _ in range(5):
        runner = GateRunner(FakeEvaluator([_fail_output()] * 3))
        result = await runner.run("analysis", {}, rubric)
        assert result.passed is False
        assert runner.retry_count == 3
