"""引擎回归验收（SP1-7）：题集 + 运行器 + 报告。"""

from engine.regression.problems import (
    REGRESSION_SET,
    RegressionProblem,
    category_distribution,
)
from engine.regression.runner import (
    AcceptanceReport,
    AcceptanceRunner,
    FailureModule,
    ProblemResult,
)

__all__ = [
    "REGRESSION_SET",
    "AcceptanceReport",
    "AcceptanceRunner",
    "FailureModule",
    "ProblemResult",
    "RegressionProblem",
    "category_distribution",
]
