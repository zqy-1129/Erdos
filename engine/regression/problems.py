"""回归题集（SP1-7）：10 道历史真题，覆盖 4 类题型。

题型分类（PRD）：优化 / 评价 / 预测 / 机理。
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RegressionProblem:
    """一道回归题。"""

    business_id: str
    category: str  # optimization / evaluation / prediction / mechanism
    title: str


# 10 道回归题（覆盖 4 类题型）
REGRESSION_SET: tuple[RegressionProblem, ...] = (
    # 优化类（3 道）
    RegressionProblem("cumcm-2023-A", "optimization", "装配线优化调度"),
    RegressionProblem("cumcm-2021-B", "optimization", "无人机路径规划"),
    RegressionProblem("cumcm-2020-A", "optimization", "资源分配与调度"),
    # 评价类（2 道）
    RegressionProblem("cumcm-2022-B", "evaluation", "综合评价与排序"),
    RegressionProblem("cumcm-2019-A", "evaluation", "风险评估与分级"),
    # 预测类（3 道）
    RegressionProblem("cumcm-2024-B", "prediction", "时间序列预测"),
    RegressionProblem("cumcm-2022-A", "prediction", "销量预测"),
    RegressionProblem("cumcm-2018-B", "prediction", "趋势外推预测"),
    # 机理类（2 道）
    RegressionProblem("cumcm-2023-B", "mechanism", "物理机理建模"),
    RegressionProblem("cumcm-2020-B", "mechanism", "生物机理建模"),
)


def category_distribution() -> dict[str, int]:
    """题型分布统计。"""
    dist: dict[str, int] = {}
    for p in REGRESSION_SET:
        dist[p.category] = dist.get(p.category, 0) + 1
    return dist
