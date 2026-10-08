"""调度器后台循环测试（SP2-7 调度器周期承诺）。

要钉住的是"自动触发"本身：三大任务此前只有 admin 端点，没有任何东西按周期去跑它们——
"每月 1 日 00:00（东八区）发放订阅月赠"在无人值守时并不会发生。逻辑判断（到点/去重）与
真实端到端（循环起来真的把月赠打到账上）分开测，端到端那条才有说服力。
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app.core.config import Settings
from app.domain.alerts.ports import BusinessAlert
from app.infra import scheduler_loop
from app.infra.scheduler_loop import due_tasks, run_scheduler_loop
from app.repository.models import SchedulerRun, Subscription
from app.repository.points import SQLAlchemyPointAccountRepository
from app.repository.uow import UnitOfWork


class RecordingOutlet:
    def __init__(self) -> None:
        self.emitted: list[BusinessAlert] = []

    async def emit(self, alert, now) -> bool:
        self.emitted.append(alert)
        return True


def test_due_tasks_gates_by_local_hour_and_dedupes_within_period() -> None:
    settings = Settings(env="test", scheduler_tz_offset_hours=8)
    fired: set[tuple[str, str]] = set()

    def names(now: datetime) -> list[str]:
        return [name for name, _ in due_tasks(now, settings, fired)]

    # 东八区 10 月 1 日 00:05 = UTC 9 月 30 日 16:05 → 只有月赠
    first_of_month = datetime(2026, 9, 30, 16, 5, tzinfo=UTC)
    assert names(first_of_month) == ["monthly_grant"]
    assert names(first_of_month) == [], "同月不重复触发"

    # 东八区 5 日 02:05 → 只有到期冻结（标记随任务名一起返回，供失败归还）
    assert due_tasks(datetime(2026, 10, 4, 18, 5, tzinfo=UTC), settings, set()) == [
        ("expire_subscriptions", ("expire", "2026-10-05"))
    ]
    # 东八区 5 日 03:05 → 只有对账
    assert due_tasks(datetime(2026, 10, 4, 19, 5, tzinfo=UTC), settings, set()) == [
        ("reconcile", ("reconcile", "2026-10-05"))
    ]
    # 其他时刻什么都不触发
    assert names(datetime(2026, 10, 4, 4, 0, tzinfo=UTC)) == []


def test_due_tasks_respects_configured_zone_offset() -> None:
    """偏移换了就要跟着换：东八区口径写死会误触发或漏触发月赠。"""
    settings = Settings(env="test", scheduler_tz_offset_hours=0)
    utc_00 = datetime(2026, 10, 1, 0, 5, tzinfo=UTC)
    assert [n for n, _ in due_tasks(utc_00, settings, set())] == ["monthly_grant"]
    # 同一时刻在 +8 口径下是 08:05，不该触发
    assert due_tasks(utc_00, Settings(env="test", scheduler_tz_offset_hours=8), set()) == []


async def test_loop_grants_monthly_points_end_to_end(session_factory, settings, monkeypatch) -> None:
    """循环真跑起来：活跃订阅在到点时拿到 400 月度积分，且重复触发不重复发放。"""
    now = datetime.now(UTC)
    local = now + timedelta(hours=settings.scheduler_tz_offset_hours)
    # 把周期口径钉到"当下"，只让月赠到点
    monkeypatch.setattr(scheduler_loop, "MONTHLY_GRANT_DAY", local.day)
    monkeypatch.setattr(scheduler_loop, "MONTHLY_GRANT_LOCAL_HOUR", local.hour)
    monkeypatch.setattr(scheduler_loop, "EXPIRE_LOCAL_HOUR", -1)
    monkeypatch.setattr(scheduler_loop, "RECONCILE_LOCAL_HOUR", -1)

    async with UnitOfWork(session_factory) as uow:
        uow.session.add(
            Subscription(
                user_id="sub-1",
                plan="monthly",
                status="active",
                start_at=now - timedelta(days=1),
                end_at=now + timedelta(days=29),
            )
        )
        await uow.session.flush()

    fast = settings.model_copy(update={"scheduler_tick_seconds": 1})
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=fast,
            session_factory=session_factory,
            alert_outlet=RecordingOutlet(),
        )
    )

    task = asyncio.create_task(run_scheduler_loop(app))  # type: ignore[arg-type]
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    async with UnitOfWork(session_factory) as uow:
        account = await SQLAlchemyPointAccountRepository(uow.session).get("sub-1")
        runs = (await uow.session.execute(select(SchedulerRun))).scalars().all()
    assert account is not None and account.monthly_balance == 400
    monthly = [r for r in runs if r.task_name == "monthly_grant"]
    assert monthly and monthly[0].status == "done"
    assert monthly[0].result == "granted:1"


async def test_loop_reconciles_and_alerts_end_to_end(session_factory, settings, monkeypatch) -> None:
    """到点自动对账：差异发现后必须经告警出口外发，而不是只写一行日志。"""
    from datetime import timedelta

    now = datetime.now(UTC)
    local = now + timedelta(hours=settings.scheduler_tz_offset_hours)
    monkeypatch.setattr(scheduler_loop, "MONTHLY_GRANT_DAY", -1)
    monkeypatch.setattr(scheduler_loop, "EXPIRE_LOCAL_HOUR", -1)
    monkeypatch.setattr(scheduler_loop, "RECONCILE_LOCAL_HOUR", local.hour)

    from app.repository.models import PointAccount

    async with UnitOfWork(session_factory) as uow:
        # 余额 100 但无流水：净额 0 —— 对账必须发现这条差异
        uow.session.add(
            PointAccount(
                user_id="drift-1", purchased_balance=100, monthly_balance=0,
                frozen=False, version=0,
            )
        )
        await uow.session.flush()

    fast = settings.model_copy(update={"scheduler_tick_seconds": 1})
    outlet = RecordingOutlet()
    app = SimpleNamespace(
        state=SimpleNamespace(settings=fast, session_factory=session_factory, alert_outlet=outlet)
    )

    task = asyncio.create_task(run_scheduler_loop(app))  # type: ignore[arg-type]
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert [a.key for a in outlet.emitted] == ["reconcile_diff"]
    assert "1 个账户" in outlet.emitted[0].message


async def test_expire_task_freezes_overdue_subscription(session_factory, settings) -> None:
    """到期冻结任务：过期订阅转 expired 并冻结积分账户（禁新任务、历史可读）。"""
    from datetime import timedelta

    from app.repository.models import PointAccount

    now = datetime.now(UTC)
    async with UnitOfWork(session_factory) as uow:
        uow.session.add(
            Subscription(
                user_id="due-1", plan="monthly", status="active",
                start_at=now - timedelta(days=40), end_at=now - timedelta(days=1),
            )
        )
        uow.session.add(
            PointAccount(
                user_id="due-1", purchased_balance=10, monthly_balance=0,
                frozen=False, version=0,
            )
        )
        await uow.session.flush()

    from app.infra import scheduler_tasks

    result = await scheduler_tasks.run_expire_subscriptions_task(
        session_factory, settings, now
    )
    assert result == "expired:1"

    async with UnitOfWork(session_factory) as uow:
        sub = (
            await uow.session.execute(select(Subscription).where(Subscription.user_id == "due-1"))
        ).scalar_one()
        account = await SQLAlchemyPointAccountRepository(uow.session).get("due-1")
    assert sub.status == "expired"
    assert account is not None and account.frozen is True


async def test_loop_survives_task_failure(session_factory, settings, monkeypatch) -> None:
    """单个任务抛异常不能拖死循环：下一周期还要继续尝试（批次键幂等兜住重复发放）。"""
    from datetime import timedelta

    now = datetime.now(UTC)
    local = now + timedelta(hours=settings.scheduler_tz_offset_hours)
    monkeypatch.setattr(scheduler_loop, "EXPIRE_LOCAL_HOUR", local.hour)

    attempts: list[str] = []

    async def boom(app, name, at):
        attempts.append(name)
        raise RuntimeError("任务内部失败")

    monkeypatch.setattr(scheduler_loop, "_dispatch", boom)
    fast = settings.model_copy(update={"scheduler_tick_seconds": 1})
    app = SimpleNamespace(
        state=SimpleNamespace(settings=fast, session_factory=session_factory, alert_outlet=RecordingOutlet())
    )

    task = asyncio.create_task(run_scheduler_loop(app))  # type: ignore[arg-type]
    await asyncio.sleep(2.4)
    assert task.done() is False, "异常不该结束循环"
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert len(attempts) >= 2, "下一周期应继续尝试"
