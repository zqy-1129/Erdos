"""行为分析数据源落库实现（SP4-2）：遥测事件 + 业务表聚合（SQLite）。

生产 ClickHouse 物化视图预聚合（禁止全表扫），此处 SQLite 降级实现等价口径。
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.analytics.ports import AnalyticsSource
from app.repository.models import Account, PointLedger, TelemetryEventRecord

STAGES = ("analysis", "modeling", "solving", "writing")


class SQLAlchemyAnalyticsSource(AnalyticsSource):
    """SQLite 数据源：telemetry_events + users + point_ledgers 聚合。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def distinct_users(self, since: datetime) -> int:
        """某时段去重用户数。"""
        result = await self._session.execute(
            select(func.count(func.distinct(TelemetryEventRecord.distinct_id))).where(
                TelemetryEventRecord.event_ts >= since
            )
        )
        return int(result.scalar_one())

    async def funnel_counts(self) -> dict[str, int]:
        """漏斗四步计数：注册（users）、首题（stage_start）、付费（pay_success）、复购（≥2 次 pay）。"""
        # 注册
        reg_count = int((await self._session.execute(select(func.count(Account.id)))).scalar_one())
        # 首题（跑过 analysis 阶段）
        first_stage = int(
            (
                await self._session.execute(
                    select(func.count(func.distinct(TelemetryEventRecord.distinct_id))).where(
                        TelemetryEventRecord.event_name == "stage_start"
                    )
                )
            ).scalar_one()
        )
        # 付费（支付成功去重）
        paid = int(
            (
                await self._session.execute(
                    select(func.count(func.distinct(TelemetryEventRecord.distinct_id))).where(
                        TelemetryEventRecord.event_name == "pay_success"
                    )
                )
            ).scalar_one()
        )
        # 复购（支付成功 ≥2 次的用户数）
        repurchase = int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(
                        select(TelemetryEventRecord.distinct_id)
                        .where(TelemetryEventRecord.event_name == "pay_success")
                        .group_by(TelemetryEventRecord.distinct_id)
                        .having(func.count(TelemetryEventRecord.id) >= 2)
                        .subquery()
                    )
                )
            ).scalar_one()
        )
        return {"注册": reg_count, "首题": first_stage, "付费": paid, "复购": repurchase}

    async def stage_conversion(self) -> dict[str, tuple[int, int]]:
        """各阶段 (start 数, success 数)。"""
        result: dict[str, tuple[int, int]] = {}
        for stage in STAGES:
            start = int(
                (
                    await self._session.execute(
                        select(func.count(TelemetryEventRecord.id)).where(
                            TelemetryEventRecord.event_name == "stage_start",
                            TelemetryEventRecord.props["stage"].as_string() == stage,
                        )
                    )
                ).scalar_one()
            )
            success = int(
                (
                    await self._session.execute(
                        select(func.count(TelemetryEventRecord.id)).where(
                            TelemetryEventRecord.event_name == "stage_success",
                            TelemetryEventRecord.props["stage"].as_string() == stage,
                        )
                    )
                ).scalar_one()
            )
            result[stage] = (start, success)
        return result

    async def gate_retry_rate(self) -> tuple[int, int]:
        """(门禁重试数, 阶段启动数)。"""
        retry = int(
            (
                await self._session.execute(
                    select(func.count(TelemetryEventRecord.id)).where(
                        TelemetryEventRecord.event_name == "gate_retry"
                    )
                )
            ).scalar_one()
        )
        start = int(
            (
                await self._session.execute(
                    select(func.count(TelemetryEventRecord.id)).where(
                        TelemetryEventRecord.event_name == "stage_start"
                    )
                )
            ).scalar_one()
        )
        return retry, start

    async def pay_conversion(self) -> tuple[int, int]:
        """(下单数, 支付成功数)。"""
        orders = int(
            (
                await self._session.execute(
                    select(func.count(TelemetryEventRecord.id)).where(
                        TelemetryEventRecord.event_name == "order_created"
                    )
                )
            ).scalar_one()
        )
        paid = int(
            (
                await self._session.execute(
                    select(func.count(TelemetryEventRecord.id)).where(
                        TelemetryEventRecord.event_name == "pay_success"
                    )
                )
            ).scalar_one()
        )
        return orders, paid

    async def points_consumed(self) -> int:
        """积分消耗总量（负数流水绝对值之和）。"""
        result = await self._session.execute(
            select(func.coalesce(func.sum(PointLedger.delta), 0)).where(PointLedger.delta < 0)
        )
        return abs(int(result.scalar_one()))

    async def content_usage(self) -> int:
        """内容库使用次数（feature_click 中 content 相关）。"""
        result = await self._session.execute(
            select(func.count(TelemetryEventRecord.id)).where(
                TelemetryEventRecord.event_name == "feature_click",
                TelemetryEventRecord.props["feature"].as_string() == "content",
            )
        )
        return int(result.scalar_one())
