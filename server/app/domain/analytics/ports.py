"""行为分析聚合（SP4-2）：漏斗 / 留存 / 转化率 / 门禁重试率。

口径（对齐 SP4-2 提示词）：
- 漏斗四步：注册 → 跑通首题 → 订阅/购分 → 复购；
- 阶段转化：stage_start → stage_success（四阶段各自转化率）；
- 门禁重试率：gate_retry 事件占比；
- 付费转化率：order_created → pay_success。

SQLite 降级实现（生产 ClickHouse 物化视图预聚合，禁止全表扫）。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class FunnelStep:
    """漏斗一步。"""

    name: str
    count: int


@dataclass(slots=True)
class DashboardMetrics:
    """看板 6 块指标。"""

    dau: int = 0  # 日活跃用户
    new_users: int = 0  # 新增用户
    pay_conversion_rate: float = 0.0  # 付费转化率
    points_consumed: int = 0  # 积分消耗
    stage_success_rates: dict[str, float] = field(default_factory=dict)  # 各阶段成功率
    content_usage: int = 0  # 内容库使用


class AnalyticsSource(Protocol):
    """分析数据源端口（遥测事件 + 业务表聚合）。"""

    async def distinct_users(self, since: datetime) -> int:
        """某时段去重用户数（DAU/新增）。"""
        ...

    async def funnel_counts(self) -> dict[str, int]:
        """漏斗四步计数（注册/首题/付费/复购）。"""
        ...

    async def stage_conversion(self) -> dict[str, tuple[int, int]]:
        """各阶段 (start 数, success 数)。"""
        ...

    async def gate_retry_rate(self) -> tuple[int, int]:
        """(门禁重试数, 阶段启动数)。"""
        ...

    async def pay_conversion(self) -> tuple[int, int]:
        """(下单数, 支付成功数)。"""
        ...

    async def points_consumed(self) -> int:
        """积分消耗总量。"""
        ...

    async def content_usage(self) -> int:
        """内容库使用次数。"""
        ...


def funnel_rate(counts: dict[str, int], step_names: tuple[str, ...]) -> list[FunnelStep]:
    """按步骤顺序计算漏斗（含转化率）。"""
    return [FunnelStep(name=name, count=counts.get(name, 0)) for name in step_names]


def conversion_rate(numerator: int, denominator: int) -> float:
    """转化率（分母为 0 时返回 0）。"""
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 4)
