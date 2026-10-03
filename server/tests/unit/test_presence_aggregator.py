"""在线分钟桶聚合器单元测试（SP2-8 优化项 1：心跳漏斗消除）。"""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.infra.presence_aggregator import MinuteAggregator
from app.repository.metrics import SQLAlchemyOnlineTrendRepository
from app.repository.models import PresenceMinuteAgg


def _minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


async def test_record_overwrites_same_minute(session_factory) -> None:
    """同分钟多次心跳取最后一次在线值（对齐原覆盖写语义）。"""
    now = datetime.now(UTC)
    agg = MinuteAggregator(session_factory, retention_days=30)
    await agg.record(_minute(now), 3)
    await agg.record(_minute(now), 7)
    n = await agg.flush_and_prune()
    assert n == 1
    async with session_factory() as session:
        row = (
            await session.execute(
                select(PresenceMinuteAgg).where(PresenceMinuteAgg.minute_ts == _minute(now))
            )
        ).scalar_one()
    assert row.online_count == 7


async def test_flush_writes_multiple_buckets_sorted(session_factory) -> None:
    """多分钟桶批量落库且按时间升序。"""
    now = datetime.now(UTC)
    agg = MinuteAggregator(session_factory, retention_days=30)
    await agg.record(_minute(now - timedelta(minutes=2)), 2)
    await agg.record(_minute(now), 5)
    await agg.record(_minute(now - timedelta(minutes=1)), 1)
    n = await agg.flush_and_prune()
    assert n == 3
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(PresenceMinuteAgg).order_by(PresenceMinuteAgg.minute_ts)
            )
        ).scalars().all()
    assert [r.online_count for r in rows] == [2, 1, 5]


async def test_flush_prunes_out_of_retention(session_factory) -> None:
    """落库时顺带裁剪保留期外分钟桶（30 天）。"""
    now = datetime.now(UTC)
    async with session_factory() as session:
        repo = SQLAlchemyOnlineTrendRepository(session)
        await repo.upsert_minute(_minute(now - timedelta(days=40)), 3)
        await repo.upsert_minute(_minute(now - timedelta(days=10)), 4)
        await session.commit()

    agg = MinuteAggregator(session_factory, retention_days=30)
    await agg.record(_minute(now), 5)
    await agg.flush_and_prune()

    async with session_factory() as session:
        rows = (
            await session.execute(select(PresenceMinuteAgg).order_by(PresenceMinuteAgg.minute_ts))
        ).scalars().all()
    assert [r.online_count for r in rows] == [4, 5]  # 40 天前桶被裁剪


async def test_flush_empty_returns_zero(session_factory) -> None:
    """无聚合桶时 flush 不落任何数据。"""
    agg = MinuteAggregator(session_factory, retention_days=30)
    assert await agg.flush_and_prune() == 0


async def test_run_loop_flushes_periodically(session_factory) -> None:
    """后台循环按周期落库；取消后可正常退出。"""
    now = datetime.now(UTC)
    agg = MinuteAggregator(session_factory, retention_days=30)
    task = asyncio.create_task(agg.run_loop(0.02))
    try:
        await agg.record(_minute(now), 9)
        await asyncio.sleep(0.08)
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(PresenceMinuteAgg).where(
                        PresenceMinuteAgg.minute_ts == _minute(now)
                    )
                )
            ).scalar_one_or_none()
        assert row is not None and row.online_count == 9
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass