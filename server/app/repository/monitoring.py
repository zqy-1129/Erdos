"""监测分钟快照仓储落库实现（upsert 用保存点兜并发竞态：撞键只撤本次插入）。"""

from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.monitoring.ports import (
    METRIC_BY_KEY,
    MonitoringPoint,
    MonitoringSample,
    MonitoringTrendRepository,
)
from app.repository.models import MonitoringMinuteSnapshot


class SQLAlchemyMonitoringTrendRepository(MonitoringTrendRepository):
    """monitoring_minute_snapshots 表实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_minute(self, minute_ts: datetime, sample: MonitoringSample) -> None:
        stmt = select(MonitoringMinuteSnapshot).where(
            MonitoringMinuteSnapshot.minute_ts == minute_ts
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            try:
                async with self._session.begin_nested():  # 撞键只撤本次插入，不动调用方已写数据
                    self._session.add(
                        MonitoringMinuteSnapshot(
                            minute_ts=minute_ts,
                            qps=sample.qps,
                            p50_ms=sample.p50_ms,
                            p95_ms=sample.p95_ms,
                            error_rate=sample.error_rate,
                            cpu_percent=sample.cpu_percent,
                            memory_percent=sample.memory_percent,
                            db_query_p95_ms=sample.db_query_p95_ms,
                            db_pool_usage=sample.db_pool_usage,
                        )
                    )
                    await self._session.flush()
                return
            except IntegrityError:
                # 并发窗口：同分钟行已被对手事务提交 -> 重查后转更新
                row = (await self._session.execute(stmt)).scalar_one()
        row.qps = sample.qps
        row.p50_ms = sample.p50_ms
        row.p95_ms = sample.p95_ms
        row.error_rate = sample.error_rate
        row.cpu_percent = sample.cpu_percent
        row.memory_percent = sample.memory_percent
        row.db_query_p95_ms = sample.db_query_p95_ms
        row.db_pool_usage = sample.db_pool_usage
        await self._session.flush()

    async def window_error_minutes(self, start: datetime, end: datetime) -> tuple[int, float]:
        """窗口内 (观测分钟数, error_rate 之和)——SLO 燃尽一次聚合查询算完，不拉全量分钟。

        `[start, end)` 左闭右开：跨小时/跨天边界同一个分钟不会被两个窗口重复计入。
        """
        stmt = select(
            func.count(MonitoringMinuteSnapshot.minute_ts),
            func.coalesce(func.sum(MonitoringMinuteSnapshot.error_rate), 0.0),
        ).where(
            MonitoringMinuteSnapshot.minute_ts >= start,
            MonitoringMinuteSnapshot.minute_ts < end,
        )
        row = (await self._session.execute(stmt)).one()
        return int(row[0] or 0), float(row[1] or 0.0)

    async def list_between(
        self, metric_key: str, start: datetime, end: datetime
    ) -> list[MonitoringPoint]:
        spec = METRIC_BY_KEY.get(metric_key)
        if spec is None:
            raise ValueError(f"未知监测指标：{metric_key}")
        column = getattr(MonitoringMinuteSnapshot, spec.column)
        stmt = (
            select(MonitoringMinuteSnapshot.minute_ts, column)
            .where(
                MonitoringMinuteSnapshot.minute_ts >= start,
                MonitoringMinuteSnapshot.minute_ts <= end,
            )
            .order_by(MonitoringMinuteSnapshot.minute_ts)
        )
        rows = (await self._session.execute(stmt)).all()
        return [MonitoringPoint(ts=row.minute_ts, value=float(row[1])) for row in rows]

    async def prune_before(self, cutoff: datetime) -> int:
        stmt = delete(MonitoringMinuteSnapshot).where(
            MonitoringMinuteSnapshot.minute_ts < cutoff
        )
        result = await self._session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)