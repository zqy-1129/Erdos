"""趋势指标仓储落库实现（分钟桶 upsert 含并发竞态兜底，同 presence 模式）。"""

from collections.abc import Sequence
from datetime import date, datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.metrics.ports import (
    DailyStatPoint,
    OnlineTrendRepository,
    TrendPoint,
    UsersTrendRepository,
)
from app.repository.models import PresenceMinuteAgg, UsersDailyStats


def _daily_points(rows: Sequence[UsersDailyStats]) -> list[DailyStatPoint]:
    """行模型 → 日快照视图（list_between / list_all 共用）。"""
    return [
        DailyStatPoint(
            stat_date=row.stat_date,
            total_users=row.total_users,
            new_users=row.new_users,
            active_users=row.active_users,
        )
        for row in rows
    ]


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

    async def upsert_minute(self, minute_ts: datetime, count: int) -> bool:
        stmt = select(PresenceMinuteAgg).where(PresenceMinuteAgg.minute_ts == minute_ts)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            try:
                # 保存点而不是会话回滚：聚合器在同一次 flush 里已经写过别的分钟桶
                async with self._session.begin_nested():
                    self._session.add(
                        PresenceMinuteAgg(minute_ts=minute_ts, online_count=count)
                    )
                    await self._session.flush()
                return True
            except IntegrityError:
                # 并发窗口：同分钟行已被对手事务提交 -> 重查后转更新
                row = (await self._session.execute(stmt)).scalar_one()
        row.online_count = count
        await self._session.flush()
        return False

    async def prune_before(self, cutoff: datetime) -> int:
        stmt = delete(PresenceMinuteAgg).where(PresenceMinuteAgg.minute_ts < cutoff)
        result = await self._session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)


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
        return _daily_points(rows)

    async def list_all(self) -> list[DailyStatPoint]:
        stmt = select(UsersDailyStats).order_by(UsersDailyStats.stat_date)
        rows = (await self._session.execute(stmt)).scalars().all()
        return _daily_points(rows)

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
                # 建行也放进保存点：注册/注销调用方在同一会话里已经写了账号与积分流水，
                # 整会话回滚会把那次注册一起撤销，而它并不认为自己是失败了的
                async with self._session.begin_nested():
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
                row = (await self._session.execute(stmt)).scalar_one()
        row.total_users = max(row.total_users + total_delta, 0)
        row.new_users = max(row.new_users + new_delta, 0)
        row.active_users = max(row.active_users + active_delta, 0)
        await self._session.flush()