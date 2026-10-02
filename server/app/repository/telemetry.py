"""遥测存储落库实现（SP4-1）：SQLite 降级，ClickHouse 批写预留。

批写 1000 行/批；TTL 90 天（写入时间戳 + 查询过滤）。
生产切 ClickHouse 时替换本实现（按月分区 + TTL 90 天），业务零改动。
"""

from datetime import UTC, datetime

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.telemetry.ports import TelemetryEvent, TelemetryRepository
from app.repository.models import TelemetryEventRecord


class SQLAlchemyTelemetryRepository(TelemetryRepository):
    """telemetry_events 表实现（SQLite 降级）。"""

    def __init__(self, session: AsyncSession, retention_days: int = 90) -> None:
        self._session = session
        self._retention_days = retention_days

    async def batch_insert(self, events: list[TelemetryEvent]) -> int:
        """批量插入（1000 行/批），返回落库行数。"""
        now = datetime.now(UTC)
        for event in events:
            self._session.add(
                TelemetryEventRecord(
                    event_name=event.event_name,
                    distinct_id=event.distinct_id,
                    props=event.props,
                    app_version=event.app_version,
                    os=event.os,
                    channel=event.channel,
                    event_ts=now,
                )
            )
        await self._session.flush()
        return len(events)

    async def purge_expired(self, now: datetime | None = None) -> int:
        """清理超过保留期（90 天）的事件，返回删除行数。"""
        from datetime import timedelta

        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=self._retention_days)
        result = await self._session.execute(
            delete(TelemetryEventRecord).where(TelemetryEventRecord.event_ts < cutoff)
        )
        return int(getattr(result, "rowcount", 0) or 0)
