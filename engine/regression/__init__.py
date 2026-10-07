"""引擎真题回归验收（SP1-7）：题集 + 任务流 + 运行器 + 证据落盘。"""

from engine.regression.evidence import EvidenceWriter, render_markdown
from engine.regression.flow import FakeLLMFlow, FlowFactory, TaskFlow
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
    AutoPassGatePolicy,
    FailureModule,
    GateDecision,
    GatePolicy,
    ProblemResult,
    RubricGatePolicy,
    StageRecord,
)

__all__ = [
    "CATEGORIES",
    "CHANNEL_BASELINE",
    "CHANNEL_REAL",
    "REGRESSION_SET",
    "AcceptanceReport",
    "AcceptanceRunner",
    "AutoPassGatePolicy",
    "EvidenceWriter",
    "FakeLLMFlow",
    "FailureModule",
    "FlowFactory",
    "GateDecision",
    "GatePolicy",
    "ProblemResult",
    "RegressionProblem",
    "RubricGatePolicy",
    "StageRecord",
    "TaskFlow",
    "category_distribution",
    "manifest",
    "render_markdown",
    "stratified_sample",
]
