"""门禁评审执行器（SP1-3）：LLM 评委 + 结构化评分解析 + 重试转人工。

红线（SP1-3 提示词）：
- 评分输出为结构化 JSON（维度分与理由），解析失败即视为不通过；
- 重试 ≤3，超限发 gate.failed 事件并携带 rubric 明细；
- 禁止关键词硬编码（评审由 LLM 评委完成，非规则匹配）。
"""

import json
from dataclasses import dataclass
from typing import Protocol

from engine.gates.schema import Rubric


class Evaluator(Protocol):
    """LLM 评委接口（SP1-5 BYOK 模型适配器实现）。

    输入阶段产物 + rubric，输出结构化评分 JSON 字符串。
    """

    async def evaluate(self, stage: str, artifact: dict, rubric: Rubric) -> str:
        """返回评分 JSON 字符串（见 _parse_score 的格式约定）。"""
        ...


@dataclass(frozen=True, slots=True)
class ScoreDetail:
    """单维度评分。"""

    dimension: str
    score: float
    reason: str


@dataclass(frozen=True, slots=True)
class GateResult:
    """一次评审结果。"""

    stage: str
    passed: bool
    total_score: float
    threshold: float
    details: tuple[ScoreDetail, ...]
    raw_output: str


@dataclass(slots=True)
class GateRunner:
    """门禁评审运行器：重试计数 + 超限转人工。

    每次评审不合格重试，重试 ≤ MAX_RETRIES；超限返回 retry_exhausted，
    由上层发 gate.failed 事件（携带 rubric 明细）。
    """

    MAX_RETRIES = 3

    evaluator: Evaluator
    retry_count: int = 0
    last_result: GateResult | None = None

    async def run(self, stage: str, artifact: dict, rubric: Rubric) -> GateResult:
        """执行一次评审；不合格则重试，超限转人工。"""
        for attempt in range(self.MAX_RETRIES):
            raw = await self.evaluator.evaluate(stage, artifact, rubric)
            result = _parse_score(stage, raw, rubric)
            self.retry_count = attempt + 1
            self.last_result = result
            if result.passed:
                return result
            # 不合格：继续重试（attempt+1）
        # 超限：转人工
        return _parse_score(stage, self.last_result.raw_output if self.last_result else "", rubric)


def _parse_score(stage: str, raw: str, rubric: Rubric) -> GateResult:
    """解析结构化评分 JSON；解析失败即视为不通过。"""
    try:
        obj = json.loads(raw)
        scores = obj["scores"]
        details = tuple(
            ScoreDetail(
                dimension=s["dimension"],
                score=float(s["score"]),
                reason=s.get("reason", ""),
            )
            for s in scores
        )
        total = _weighted_total(details, rubric)
        passed = total >= rubric.threshold
        return GateResult(
            stage=stage, passed=passed, total_score=total, threshold=rubric.threshold,
            details=details, raw_output=raw,
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        # 解析失败即视为不通过（SP1-3 红线）
        return GateResult(
            stage=stage, passed=False, total_score=0.0, threshold=rubric.threshold,
            details=(), raw_output=raw,
        )


def _weighted_total(details: tuple[ScoreDetail, ...], rubric: Rubric) -> float:
    """加权总分：按 rubric 维度权重加权。"""
    weights = {d.name: d.weight for d in rubric.dimensions}
    total = 0.0
    for detail in details:
        weight = weights.get(detail.dimension, 0.0)
        total += detail.score * weight
    return round(total, 4)
