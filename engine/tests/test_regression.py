"""验收运行器单元测试（SP1-7）：成功率统计 + 门禁硬条件 + 失败归因。"""

import json

from engine.gates.evaluator import Evaluator
from engine.gates.schema import Dimension, Rubric
from engine.regression.problems import REGRESSION_SET, category_distribution
from engine.regression.runner import AcceptanceRunner, FailureModule


class PassEvaluator(Evaluator):
    """门禁总是通过的评委。"""

    async def evaluate(self, stage, artifact, rubric) -> str:
        return json.dumps({
            "scores": [{"dimension": d.name, "score": 0.9, "reason": "ok"} for d in rubric.dimensions],
        })


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
            dimensions=(
                Dimension("维度1", 0.5, ""),
                Dimension("维度2", 0.5, ""),
            ),
        )
        for stage in ("analysis", "modeling", "solving", "writing")
    }


def test_regression_set_10_problems_4_categories() -> None:
    """回归题集：10 道题，覆盖 4 类题型。"""
    assert len(REGRESSION_SET) == 10
    dist = category_distribution()
    assert set(dist.keys()) == {"optimization", "evaluation", "prediction", "mechanism"}
    assert sum(dist.values()) == 10


async def test_acceptance_all_pass_100_percent() -> None:
    """全部门禁通过：成功率 100%，锁 MVP。"""
    runner = AcceptanceRunner(PassEvaluator(), _rubrics())
    report = await runner.run_all(REGRESSION_SET)
    assert report.passed == 10
    assert report.success_rate == 1.0
    assert report.decision() == "锁 MVP"


async def test_acceptance_gate_fail_attributes_to_gate() -> None:
    """门禁失败：归因到 gate 模块。"""
    runner = AcceptanceRunner(FailEvaluator(), _rubrics())
    report = await runner.run_all(REGRESSION_SET)
    assert report.passed == 0
    assert report.failure_by_module.get(FailureModule.GATE, 0) == 10
    assert report.decision() == "触发风险 R1 响应"


async def test_acceptance_partial_pass_below_threshold() -> None:
    """部分通过（成功率 < 85%）：触发风险响应。"""
    # 混合评委：前 7 题通过，后 3 题门禁失败
    class MixedEvaluator(Evaluator):
        def __init__(self) -> None:
            self._calls = 0

        async def evaluate(self, stage, artifact, rubric) -> str:
            self._calls += 1
            # 按题号（每 4 阶段一题）判定：前 7 题通过
            problem_index = (self._calls - 1) // 4
            score = 0.9 if problem_index < 7 else 0.1
            return json.dumps({
                "scores": [{"dimension": d.name, "score": score, "reason": "x"} for d in rubric.dimensions],
            })

    runner = AcceptanceRunner(MixedEvaluator(), _rubrics())
    report = await runner.run_all(REGRESSION_SET)
    assert report.passed == 7
    assert report.success_rate == 0.7  # < 0.85
    assert report.decision() == "触发风险 R1 响应"


def test_failure_classification() -> None:
    """异常分类到模块。"""
    from engine.regression.runner import _classify_exception

    assert _classify_exception(TimeoutError("timeout")) == FailureModule.SANDBOX
    assert _classify_exception(ConnectionError("network")) == FailureModule.ADAPTER
    assert _classify_exception(ValueError("bad")) == FailureModule.ORCHESTRATOR
