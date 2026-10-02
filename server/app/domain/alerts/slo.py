"""SLO 预算燃尽（SP4-3）：月度 99.5% 可用性预算燃尽计算。"""

from datetime import datetime


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
