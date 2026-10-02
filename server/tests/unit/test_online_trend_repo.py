"""在线分钟桶仓储集成测试：upsert 新建语义与裁剪（独立会话工厂）。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.repository.metrics import SQLAlchemyOnlineTrendRepository
from app.repository.models import PresenceMinuteAgg

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


async def test_upsert_minute_returns_created_flag(session_factory) -> None:
    async with session_factory() as session:
        repo = SQLAlchemyOnlineTrendRepository(session)
        assert await repo.upsert_minute(NOW, 5) is True
        # 同分钟再次写入：覆盖而非新建
        assert await repo.upsert_minute(NOW, 9) is False
        await session.commit()
    async with session_factory() as session:
        rows = (await session.execute(select(PresenceMinuteAgg))).scalars().all()
    assert len(rows) == 1
    assert rows[0].online_count == 9, "同分钟覆盖为最新值"


async def test_prune_before_removes_old_rows(session_factory) -> None:
    async with session_factory() as session:
        repo = SQLAlchemyOnlineTrendRepository(session)
        await repo.upsert_minute(NOW - timedelta(days=40), 3)
        await repo.upsert_minute(NOW - timedelta(days=10), 4)
        await repo.upsert_minute(NOW, 5)
        removed = await repo.prune_before(NOW - timedelta(days=30))
        await session.commit()
    assert removed == 1, "仅 30 天前的旧桶被裁剪"
    async with session_factory() as session:
        rows = (await session.execute(select(PresenceMinuteAgg))).scalars().all()
    remaining = sorted(r.online_count for r in rows)
    assert remaining == [4, 5], "保留期内桶不受影响"


async def test_prune_before_empty_returns_zero(session_factory) -> None:
    async with session_factory() as session:
        repo = SQLAlchemyOnlineTrendRepository(session)
        await repo.upsert_minute(NOW, 1)
        removed = await repo.prune_before(NOW - timedelta(days=30))
        await session.commit()
    assert removed == 0
