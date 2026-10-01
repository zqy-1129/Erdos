"""趋势指标仓储落库实现（分钟桶 upsert 含并发竞态兜底，同 presence 模式）。"""

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.metrics.ports import (
    DailyStatPoint,
    OnlineTrendRepository,
    TrendPoint,
    UsersTrendRepository,
)
from app.repository.models import PresenceMinuteAgg, UsersDailyStats


class SQLAlchemyOnlineTrendRepository(OnlineTrendRepository):
    """presence_minute_agg 表实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_between(self, start: datetime, end: datetime) -> list[TrendPoint]:
        stmt = (
            select(PresenceMinuteAgg)
            .where(
                PresenceMinuteAgg.minute_ts >= start,
                PresenceMinuteAgg.minute_ts <= end,
            )
            .order_by(PresenceMinuteAgg.minute_ts)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [TrendPoint(ts=row.minute_ts, value=row.online_count) for row in rows]

    async def upsert_minute(self, minute_ts: datetime, count: int) -> None:
        stmt = select(PresenceMinuteAgg).where(PresenceMinuteAgg.minute_ts == minute_ts)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            try:
                self._session.add(
                    PresenceMinuteAgg(minute_ts=minute_ts, online_count=count)
                )
                await self._session.flush()
                return
            except IntegrityError:
                await self._session.rollback()
                row = (await self._session.execute(stmt)).scalar_one()
        row.online_count = count
        await self._session.flush()


class SQLAlchemyUsersTrendRepository(UsersTrendRepository):
    """users_daily_stats 表实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_between(self, start: date, end: date) -> list[DailyStatPoint]:
        stmt = (
            select(UsersDailyStats)
            .where(
                UsersDailyStats.stat_date >= start,
                UsersDailyStats.stat_date <= end,
            )
            .order_by(UsersDailyStats.stat_date)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [
            DailyStatPoint(
                stat_date=row.stat_date,
                total_users=row.total_users,
                new_users=row.new_users,
                active_users=row.active_users,
            )
            for row in rows
        ]

    async def apply_daily_delta(
        self,
        stat_date: date,
        *,
        total_delta: int = 0,
        new_delta: int = 0,
        active_delta: int = 0,
    ) -> None:
        """按日累加计数（账号域注册/注销事件驱动；同行不存在则建行）。"""
        stmt = select(UsersDailyStats).where(UsersDailyStats.stat_date == stat_date)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            try:
                self._session.add(
                    UsersDailyStats(
                        stat_date=stat_date,
                        total_users=max(total_delta, 0),
                        new_users=max(new_delta, 0),
                        active_users=max(active_delta, 0),
                    )
                )
                await self._session.flush()
                return
            except IntegrityError:
                await self._session.rollback()
                row = (await self._session.execute(stmt)).scalar_one()
        row.total_users = max(row.total_users + total_delta, 0)
        row.new_users = max(row.new_users + new_delta, 0)
        row.active_users = max(row.active_users + active_delta, 0)
        await self._session.flush()