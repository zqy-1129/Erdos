"""监测仓储集成测试：分钟快照 upsert/查询/裁剪/窗口聚合（独立会话工厂）。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

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


async def test_window_error_minutes_aggregates_and_excludes_empty(
    session_factory,
) -> None:
    """SLO 燃尽要的窗口聚合：命中分钟数与 error_rate 之和，空窗口返回 0 而不是 None。"""
    async with session_factory() as session:
        repo = SQLAlchemyMonitoringTrendRepository(session)
        await repo.upsert_minute(NOW, _sample(error_rate=0.02))
        await repo.upsert_minute(NOW + timedelta(minutes=1), _sample(error_rate=0.03))
        await repo.upsert_minute(NOW + timedelta(minutes=9), _sample(error_rate=1.0))
        await session.commit()

    async with session_factory() as session:
        repo = SQLAlchemyMonitoringTrendRepository(session)
        observed, error_sum = await repo.window_error_minutes(
            NOW - timedelta(minutes=5), NOW + timedelta(minutes=5)
        )
        empty = await repo.window_error_minutes(
            NOW + timedelta(days=1), NOW + timedelta(days=2)
        )
    assert observed == 2
    assert error_sum == pytest.approx(0.05)
    assert empty == (0, 0.0)


class _Savepoint:
    def __init__(self, owner: "_ConflictSession") -> None:
        self._owner = owner

    async def __aenter__(self) -> "_Savepoint":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        if exc_type is not None:
            self._owner.savepoint_rollbacks += 1
        return False


class _ConflictSession:
    """只测撞键后的处理形状：必须走保存点，绝不整会话回滚。

    这条竞态要两个采样实例交错执行才会出现（pysqlite 的读不加快照，单会话里造不出
    确定性的"判空后插队"时序），所以这里用替身把"第一次 flush 撞键"钉住，被测的是
    upsert_minute 自己的控制流。
    """

    def __init__(self, existing_row: SimpleNamespace) -> None:
        self.existing_row = existing_row
        self.selects = 0
        self.flushes = 0
        self.session_rollbacks = 0
        self.savepoint_rollbacks = 0

    async def execute(self, _stmt):
        self.selects += 1
        row = None if self.selects == 1 else self.existing_row

        class _Result:
            def scalar_one_or_none(self) -> SimpleNamespace | None:
                return row

            def scalar_one(self) -> SimpleNamespace:
                return row

        return _Result()

    def add(self, _obj) -> None:
        return None

    async def flush(self) -> None:
        self.flushes += 1
        if self.flushes == 1:
            raise IntegrityError(
                "INSERT INTO monitoring_minute_snapshots ...", {}, Exception("UNIQUE constraint failed")
            )

    def begin_nested(self) -> _Savepoint:
        return _Savepoint(self)

    async def rollback(self) -> None:
        self.session_rollbacks += 1


async def test_upsert_minute_conflict_does_not_rollback_caller_transaction() -> None:
    """撞键转更新时，调用方同事务的既有写入必须活下来（sampling 的 upsert 与裁剪同事务）。

    旧实现用 session.rollback() 兜竞态，与 scheduler_runs.claim 改前同一个坑：
    保存点只撤本次插入，会话回滚会连调用方已写的行一起抹掉。
    """
    row = SimpleNamespace(
        minute_ts=NOW,
        qps=0.0,
        p50_ms=0.0,
        p95_ms=0.0,
        error_rate=0.0,
        cpu_percent=0.0,
        memory_percent=0.0,
        db_query_p95_ms=0.0,
        db_pool_usage=0.0,
    )
    session = _ConflictSession(row)
    repo = SQLAlchemyMonitoringTrendRepository(session)  # type: ignore[arg-type]

    await repo.upsert_minute(NOW, _sample(qps=4.0, error_rate=0.5))

    assert session.session_rollbacks == 0, "撞键不得回滚调用方事务"
    assert session.savepoint_rollbacks == 1
    assert row.qps == 4.0 and row.error_rate == 0.5, "撞键后要转更新而不是丢掉这次采样"
