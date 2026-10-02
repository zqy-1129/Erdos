"""行为分析服务（SP4-2）：聚合漏斗/留存/转化率，输出看板 6 块指标。"""

from datetime import datetime, timedelta

from app.domain.analytics.ports import (
    AnalyticsSource,
    DashboardMetrics,
    conversion_rate,
    funnel_rate,
)


class AnalyticsService:
    """行为分析用例：从数据源聚合看板指标。"""

    FUNNEL_STEPS = ("注册", "首题", "付费", "复购")

    def __init__(self, source: AnalyticsSource) -> None:
        self._source = source

    async def funnel(self) -> dict:
        """漏斗四步（注册→首题→付费→复购）。"""
        counts = await self._source.funnel_counts()
        steps = funnel_rate(counts, self.FUNNEL_STEPS)
        total = steps[0].count if steps else 0
        return {
            "steps": [{"name": s.name, "count": s.count} for s in steps],
            "conversion_rates": {
                s.name: conversion_rate(s.count, total) for s in steps
            },
        }

    async def dashboard(self, since: datetime) -> DashboardMetrics:
        """聚合看板 6 块指标。"""
        dau = await self._source.distinct_users(since)
        new_users = await self._source.distinct_users(since - timedelta(days=1))
        order_count, pay_count = await self._source.pay_conversion()
        gate_retry, stage_start = await self._source.gate_retry_rate()

        # 阶段成功率
        stage_rates: dict[str, float] = {}
        for stage, (start, success) in (await self._source.stage_conversion()).items():
            stage_rates[stage] = conversion_rate(success, start)

        return DashboardMetrics(
            dau=dau,
            new_users=new_users,
            pay_conversion_rate=conversion_rate(pay_count, order_count),
            points_consumed=await self._source.points_consumed(),
            stage_success_rates=stage_rates,
            content_usage=await self._source.content_usage(),
        )
