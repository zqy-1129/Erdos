"""调度批次账本测试：卡死批次可重占 + 批次行裁剪。

要钉住的两件事都是上一轮实现里发现的真实风险：
① `claim` 原本是撞键即拒——月赠若在写入 running 行之后崩溃，当月批次永久占位，月赠再也不会
   发放（重复发放的防护本就在积分流水 exec_id 上，不该靠卡死行来挡）；
② 查单兜底把 scheduler_runs 当去重账本用，行数随订单线性增长，没有裁剪就是慢速泄漏。
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app.domain.scheduler.ports import GrantOutcome
from app.domain.scheduler.service import SchedulerService
from app.repository.models import SchedulerRun
from app.repository.scheduler import SQLAlchemySchedulerRunRepository
from app.repository.uow import UnitOfWork

NOW = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)


class CountingGrant:
    def __init__(self) -> None:
        self.calls = 0

    async def grant_all_active(self, now: datetime) -> GrantOutcome:
        self.calls += 1
        return GrantOutcome(granted=3, failed=0)


class _NoopRunner:
    async def expire_overdue(self, now: datetime) -> int:
        return 0


class FakeRuns:
    async def list_accounts(self) -> list[tuple[str, int]]:
        return []

    async def ledger_net(self, user_id: str) -> int:
        return 0


def _service(runs: Any, stale_after: int) -> tuple[SchedulerService, CountingGrant]:
    grant = CountingGrant()
    svc = SchedulerService(
        runs, FakeRuns(), grant, _NoopRunner(), stale_after_seconds=stale_after
    )
    return svc, grant


async def test_crashed_batch_is_reclaimed_after_stale_window(session_factory) -> None:
    """崩在 mark_done 之前：阈值内不重复执行，超阈值可重占（否则当月月赠永久缺失）。"""
    async with UnitOfWork(session_factory) as uow:
        svc, grant = _service(SQLAlchemySchedulerRunRepository(uow.session), stale_after=1800)
        row = SchedulerRun(
            id="batch-1", task_name="monthly_grant", batch_key="202610",
            status="running", started_at=NOW,
        )
        uow.session.add(row)
        await uow.session.flush()
        # 认领一个"正在跑"且未超阈值的批次 -> 不重复执行
        assert (await svc.run_monthly_grant(NOW + timedelta(minutes=10))).result == "already_ran"
        assert grant.calls == 0

    async with UnitOfWork(session_factory) as uow:
        svc2, grant2 = _service(SQLAlchemySchedulerRunRepository(uow.session), stale_after=1800)
        assert (await svc2.run_monthly_grant(NOW + timedelta(minutes=31))).result == "granted:3 failed:0"
        assert grant2.calls == 1

    async with UnitOfWork(session_factory) as uow:
        finished = (
            await uow.session.execute(
                select(SchedulerRun).where(SchedulerRun.id == "batch-1")
            )
        ).scalars().one()
        assert finished.status == "done" and finished.result == "granted:3 failed:0"


async def test_done_batch_is_never_reclaimed(session_factory) -> None:
    """跑完的批次哪怕再旧也不重跑：完成态与崩溃态必须区分开。"""
    async with UnitOfWork(session_factory) as uow:
        svc, _ = _service(SQLAlchemySchedulerRunRepository(uow.session), stale_after=1800)
        assert (await svc.run_monthly_grant(NOW)).result == "granted:3 failed:0"

    async with UnitOfWork(session_factory) as uow:
        svc2, grant2 = _service(SQLAlchemySchedulerRunRepository(uow.session), stale_after=1800)
        later = NOW + timedelta(days=6)  # 同月、远超 stale 阈值
        assert (await svc2.run_monthly_grant(later)).result == "already_ran"
        assert grant2.calls == 0


async def test_zero_stale_window_keeps_strict_behavior(session_factory) -> None:
    """stale_after=0 时与改动前完全一致（默认保守，不给未配置的部署带来重跑）。"""
    async with UnitOfWork(session_factory) as uow:
        uow.session.add(
            SchedulerRun(
                id="strict", task_name="expire_subscriptions", batch_key="2026-10-08",
                status="running", started_at=NOW - timedelta(hours=2),
            )
        )
        await uow.session.flush()
        runs = SQLAlchemySchedulerRunRepository(uow.session)
        assert (
            await runs.claim("expire_subscriptions", "2026-10-08", NOW, stale_after_seconds=0)
            is False
        )
        svc, _ = _service(runs, stale_after=0)
        assert await svc.run_expire_subscriptions(NOW) == "already_ran"


async def test_reclaim_is_scoped_to_the_same_batch(session_factory) -> None:
    """重占只针对同 task+batch_key：跨批次/跨任务不得互相放行。"""
    async with UnitOfWork(session_factory) as uow:
        uow.session.add(
            SchedulerRun(
                id="q1", task_name="order_query", batch_key="order-a:100",
                status="running", started_at=NOW - timedelta(days=2),
            )
        )
        await uow.session.flush()
        runs = SQLAlchemySchedulerRunRepository(uow.session)
        stale = NOW - timedelta(days=1)
        assert (
            await runs.claim("order_query", "order-a:100", stale, stale_after_seconds=60) is True
        )
        assert await runs.claim("order_query", "order-b:100", stale, stale_after_seconds=60) is True
        assert (
            await runs.claim("reconcile", "order-a:100", stale, stale_after_seconds=60) is True
        )
        left = (
            await uow.session.execute(select(func.count()).select_from(SchedulerRun))
        ).scalar_one()
        assert left == 3


async def test_prune_before_removes_only_old_batches(session_factory) -> None:
    """批次账本裁剪：只删早于阈值的行，当前窗口内的去重记录必须留着。"""
    async with UnitOfWork(session_factory) as uow:
        runs = SQLAlchemySchedulerRunRepository(uow.session)
        for i in range(3):
            uow.session.add(
                SchedulerRun(
                    id=f"old-{i}", task_name="order_query", batch_key=f"o{i}:1",
                    status="done", started_at=NOW - timedelta(days=40),
                )
            )
        uow.session.add(
            SchedulerRun(
                id="fresh", task_name="order_query", batch_key="o9:1",
                status="done", started_at=NOW - timedelta(minutes=5),
            )
        )
        await uow.session.flush()

        removed = await runs.prune_before(NOW - timedelta(days=30))
        remaining = (
            await uow.session.execute(select(func.count()).select_from(SchedulerRun))
        ).scalar_one()

    assert removed == 3
    assert remaining == 1


async def test_reconcile_task_prunes_as_it_runs(session_factory, settings) -> None:
    """每日对账顺带裁剪批次账本：不另起清理任务，也不会漏裁。"""
    from app.infra import scheduler_tasks

    class _NoopOutlet:
        def __init__(self) -> None:
            self.emitted: list[Any] = []

        async def emit(self, alert, now, dedupe=True) -> bool:
            self.emitted.append(alert)
            return True

    async with UnitOfWork(session_factory) as uow:
        uow.session.add(
            SchedulerRun(
                id="stale", task_name="order_query", batch_key="z:1",
                status="done", started_at=datetime.now(UTC) - timedelta(days=120),
            )
        )
        await uow.session.flush()

    tuned = settings.model_copy(update={"scheduler_runs_retention_days": 30})
    await scheduler_tasks.run_reconcile_task(
        session_factory, tuned, _NoopOutlet(), datetime.now(UTC)
    )

    async with UnitOfWork(session_factory) as uow:
        left = (
            (
                await uow.session.execute(
                    select(SchedulerRun).where(SchedulerRun.id == "stale")
                )
            )
            .scalars()
            .all()
        )
    assert left == [], "超过保留期的批次行应被每日对账裁掉"
