"""SLO 预算燃尽（SP4-3）：月度 99.5% 可用性的预算燃尽计算与判级。

窗口取 1 个自然日而非整月，理由不是省事：分钟级监测快照的保留期默认只有 7 天
（`monitoring_retention_days=7`），跨月的分钟数据本来就不存在。预算按"份额"线性切分
（30 天预算 / 30 = 单日份额），所以日窗口燃尽比例与月度燃尽比例同量纲，可以直接判级；
而且部署不满一个月时也不会因为"历史数据缺失"算出虚假的好看数字。

本检查跑在服务进程内，**发现不了"整个进程挂了"**——那种情况靠外部探活与采样侧的瞬时
可用性 P0（`monitoring/service.py` 的 availability 规则）。这里管的是"活着但质量不达标"。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from app.domain.alerts.severity import Severity

# 单日预算份额 = 30 天预算 / 30；改这里必须同时确认 monitoring_retention_days >= 窗口天数。
SLO_WINDOW_DAYS: Final[int] = 1
# 观测样本不足时不判燃尽（部署首小时、监测刚启动），改由数据覆盖率告警负责可见性。
SLO_MIN_OBSERVED_MINUTES: Final[int] = 60


def monthly_downtime_budget(target_availability: float = 0.995, days: int = 30) -> float:
    """月度允许宕机时长（秒）：30 天 * (1 - 可用性目标)。"""
    total_seconds = days * 24 * 3600
    return total_seconds * (1 - target_availability)


def burn_ratio(downtime_seconds: float, target_availability: float = 0.995, days: int = 30) -> float:
    """燃尽比例：已用宕机时长 / 月度预算。"""
    budget = monthly_downtime_budget(target_availability, days)
    if budget == 0:
        return 0.0
    return round(downtime_seconds / budget, 4)


def remaining_budget(downtime_seconds: float, target_availability: float = 0.995, days: int = 30) -> float:
    """剩余预算（秒）。"""
    return max(0.0, monthly_downtime_budget(target_availability, days) - downtime_seconds)


def elapsed_ratio(now: datetime, month_start: datetime) -> float:
    """本月已过时间占比（燃尽图 X 轴参考）。"""
    total = _month_duration(month_start)
    if total <= 0:
        return 0.0
    return round((now - month_start).total_seconds() / total, 4)


def _month_duration(month_start: datetime) -> float:
    """本月时长（秒），简化为 30 天。"""
    return 30 * 24 * 3600


@dataclass(frozen=True, slots=True)
class SloBurn:
    """一个评估窗口内的燃尽判定结果。"""

    window_days: int
    observed_minutes: int
    downtime_seconds: float
    budget_seconds: float
    burn_ratio: float
    availability: float
    severity: Severity | None

    @property
    def breached(self) -> bool:
        return self.severity is not None


def coverage_of(observed_minutes: int, window_days: int = SLO_WINDOW_DAYS) -> float:
    """窗口内观测覆盖率：无监测数据的时段不能被当作"一切正常"。"""
    expected = window_days * 24 * 60
    if expected <= 0:
        return 0.0
    return round(min(1.0, observed_minutes / expected), 4)


def evaluate_burn(
    error_rate_sum: float,
    observed_minutes: int,
    *,
    window_days: int = SLO_WINDOW_DAYS,
    target: float = 0.995,
    page_ratio: float = 2.0,
    warn_ratio: float = 1.0,
    min_observed_minutes: int = SLO_MIN_OBSERVED_MINUTES,
) -> SloBurn | None:
    """按分钟等权算窗口不可用秒数，与该窗口的预算份额比出燃尽比例并判级。

    - 不可用秒数 = Σ(该分钟 error_rate) × 60s：夜间低流量分钟的故障同样计入，不因请求少而"摊薄"；
    - 预算份额 = 30 天预算 × window_days/30，故日窗口比例与月度比例同量纲；
    - page_ratio（默认 2 倍）为 P0：维持此速率月底会烧光两倍预算；warn_ratio（1 倍）为 P1；
    - 观测分钟不足 `min_observed_minutes` 返回 None——刚启动没数据不判健康也不判违规，
      数据缺失本身由 `coverage_of` 那条告警负责。
    """
    if observed_minutes < min_observed_minutes:
        return None
    downtime_seconds = 60.0 * error_rate_sum
    budget_seconds = monthly_downtime_budget(target, window_days)
    ratio = burn_ratio(downtime_seconds, target, window_days)
    availability = 1.0 - (downtime_seconds / (observed_minutes * 60.0))
    severity: Severity | None = None
    if ratio >= page_ratio:
        severity = Severity.P0
    elif ratio >= warn_ratio:
        severity = Severity.P1
    return SloBurn(
        window_days=window_days,
        observed_minutes=observed_minutes,
        downtime_seconds=round(downtime_seconds, 3),
        budget_seconds=round(budget_seconds, 3),
        burn_ratio=ratio,
        availability=round(availability, 6),
        severity=severity,
    )
