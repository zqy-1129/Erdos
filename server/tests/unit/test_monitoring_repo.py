"""监测仓储集成测试：分钟快照 upsert/查询/裁剪（独立会话工厂）。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.domain.monitoring.ports import MonitoringSample
from app.repository.models import MonitoringMinuteSnapshot
from app.repository.monitoring import SQLAlchemyMonitoringTrendRepository

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _sample(**overrides) -> MonitoringSample:
    base = dict(
        sampled_at=NOW,
        qps=10.0,
        error_rate=0.01,
        p50_ms=20.0,
        p95_ms=80.0,
        cpu_percent=10.0,
        memory_percent=30.0,
        rss_mb=64.0,
        db_query_p95_ms=30.0,
        db_pool_usage=0.1,
    )
    base.update(overrides)
    return MonitoringSample(**base)


async def test_upsert_minute_overwrites_and_dedupes(session_factory) -> None:
    async with session_factory() as session:
        repo = SQLAlchemyMonitoringTrendRepository(session)
        await repo.upsert_minute(NOW, _sample(qps=1.0))
        # 同分钟再次写入：覆盖而非新增
        await repo.upsert_minute(NOW, _sample(qps=2.0))
        await session.commit()
    async with session_factory() as session:
        rows = (
            (await session.execute(select(MonitoringMinuteSnapshot)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].qps == 2.0


async def test_list_between_returns_metric_column(session_factory) -> None:
    async with session_factory() as session:
        repo = SQLAlchemyMonitoringTrendRepository(session)
        await repo.upsert_minute(NOW, _sample(qps=3.0))
        await repo.upsert_minute(NOW + timedelta(minutes=1), _sample(qps=5.0))
        await session.commit()
    async with session_factory() as session:
        repo = SQLAlchemyMonitoringTrendRepository(session)
        points = await repo.list_between(
            "qps", NOW - timedelta(minutes=1), NOW + timedelta(minutes=2)
        )
    assert [(p.ts.minute, p.value) for p in points] == [(0, 3.0), (1, 5.0)]


async def test_list_between_rejects_unknown_metric(session_factory) -> None:
    async with session_factory() as session:
        repo = SQLAlchemyMonitoringTrendRepository(session)
        try:
            await repo.list_between("nope", NOW, NOW)
        except ValueError as exc:
            assert "未知监测指标" in str(exc)
        else:
            raise AssertionError("未知指标应抛 ValueError")


async def test_prune_before_removes_old_rows(session_factory) -> None:
    async with session_factory() as session:
        repo = SQLAlchemyMonitoringTrendRepository(session)
        await repo.upsert_minute(NOW - timedelta(days=10), _sample())
        await repo.upsert_minute(NOW, _sample())
        removed = await repo.prune_before(NOW - timedelta(days=7))
        await session.commit()
    assert removed == 1