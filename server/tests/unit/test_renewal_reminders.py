"""续费提醒接线（SP2-7 调度器承诺："到期前 3 天进入提醒队列"）。

`NotificationService.notify_renewal_reminder` 一直是有实现、有幂等 message_id、**零调用方**
的第四例"文档 ✅ / 代码有 / 没人触发"。本文件钉住四件事：

1. 候选集窗口与左连语义（没邮箱的订阅必须现身，不能被内连接静默吞掉）；
2. 只提醒一次——幂等落在通知的 message_id 上，任务侧不记状态，所以每日重跑安全；
3. 发不出去（无邮箱）与发送失败都要有计数，不静默；
4. 循环里真的会按天触发（顺带把到期冻结的 `_dispatch` 分支也端到端跑一遍——那条分支
   此前在测试里从未被循环走过，接线写坏了不会有人发现）。
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app.core.config import Settings
from app.infra import scheduler_loop
from app.infra.scheduler_loop import run_scheduler_loop
from app.infra.scheduler_runners import SQLAlchemyRenewalReminderRunner
from app.infra.scheduler_tasks import run_renewal_reminders_task
from app.repository.models import Account, NotificationSendLog, PointAccount, Subscription
from app.repository.uow import UnitOfWork

NOW = datetime(2026, 10, 9, 3, 0, tzinfo=UTC)


class RecordingSender:
    """发送桩：记录调用，可注入失败。"""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.fail = fail

    async def send(self, channel: str, template_id: str, target: str, payload: dict) -> None:
        if self.fail:
            raise RuntimeError("SMTP 未配置凭据（DEC-021）")
        self.sent.append((channel, template_id, target))


class NullLimiter:
    """提醒不走限流，这里只为满足构造签名。"""

    async def allow(self, key: str) -> bool:
        return True

    async def retry_after_seconds(self, key: str) -> float:
        return 0.0


class RecordingOutlet:
    def __init__(self) -> None:
        self.emitted: list[object] = []

    async def emit(self, alert, now, dedupe: bool = True) -> bool:
        self.emitted.append(alert)
        return True


def _fake_app(
    settings: Settings, session_factory, sender: RecordingSender
) -> SimpleNamespace:
    """循环需要的 app.state 形状（提醒分支比其它任务多 sender 与 code_limiter 两项）。"""
    return SimpleNamespace(
        state=SimpleNamespace(
            settings=settings,
            session_factory=session_factory,
            alert_outlet=RecordingOutlet(),
            notification_sender=sender,
            code_limiter=NullLimiter(),
        )
    )


def _settings() -> Settings:
    return Settings(env="test", renewal_remind_days=3)


async def _seed(
    session_factory,
    user_id: str,
    *,
    email: str | None,
    end_at: datetime | None,
    account: bool = True,
) -> None:
    """造一条订阅（可选带邮箱账号 / 只有手机号 / 无账号行）。"""
    async with UnitOfWork(session_factory) as uow:
        if account and email is not None:
            uow.session.add(
                Account(id=user_id, email=email, password_hash="x", status="active", role="user")
            )
        elif account:
            uow.session.add(Account(id=user_id, phone="13800000000", password_hash="x"))
        if end_at is not None:
            uow.session.add(
                Subscription(
                    user_id=user_id,
                    plan="monthly",
                    status="active",
                    start_at=end_at - timedelta(days=30),
                    end_at=end_at,
                )
            )
        await uow.session.flush()


# ----------------------------------------------------------------------
# 候选集
# ----------------------------------------------------------------------
async def test_list_due_window_and_left_join(session_factory) -> None:
    """到期窗口内全部现身（含无邮箱与无账号行），窗口外与已过期不入围。"""
    await _seed(session_factory, "u-soon", email="soon@e.com", end_at=NOW + timedelta(days=2))
    await _seed(session_factory, "u-late", email="late@e.com", end_at=NOW + timedelta(days=5))
    await _seed(session_factory, "u-expired", email="old@e.com", end_at=NOW - timedelta(days=1))
    await _seed(session_factory, "u-noemail", email=None, end_at=NOW + timedelta(days=1))
    await _seed(
        session_factory, "ghost-1", email=None, end_at=NOW + timedelta(hours=6), account=False
    )

    async with session_factory() as session:
        due = await SQLAlchemyRenewalReminderRunner(session).list_due(NOW, 3)

    assert [(d.user_id, d.email) for d in due] == [
        ("ghost-1", None),
        ("u-noemail", None),
        ("u-soon", "soon@e.com"),
    ]
    assert all(d.end_at.tzinfo is not None for d in due), "SQLite 读回的 naive 时间要补回 UTC"


# ----------------------------------------------------------------------
# 任务：只提醒一次，失败与无联系方式都不静默
# ----------------------------------------------------------------------
async def test_task_reminds_once_even_when_run_daily(session_factory) -> None:
    """隔一天再跑：候选集仍然命中，但 message_id 幂等复用既有记录，不再发第二封。"""
    await _seed(session_factory, "u-1", email="a@e.com", end_at=NOW + timedelta(days=2))
    sender = RecordingSender()

    first = await run_renewal_reminders_task(
        session_factory, _settings(), sender, NullLimiter(), NOW
    )
    second = await run_renewal_reminders_task(
        session_factory, _settings(), sender, NullLimiter(), NOW + timedelta(days=1)
    )

    assert first == "due:1 sent:1 failed:0 no_contact:0", first
    assert second == "due:1 sent:1 failed:0 no_contact:0", second
    assert len(sender.sent) == 1, "同一次到期只发一封提醒"
    assert sender.sent[0] == ("email", "renewal_reminder", "a@e.com")

    async with session_factory() as session:
        logs = (await session.execute(select(NotificationSendLog))).scalars().all()
    assert len(logs) == 1
    assert logs[0].message_id == "renew:u-1:2026-10-11"


async def test_task_counts_no_contact_without_sending(session_factory) -> None:
    await _seed(session_factory, "u-2", email=None, end_at=NOW + timedelta(days=1))
    sender = RecordingSender()

    result = await run_renewal_reminders_task(
        session_factory, _settings(), sender, NullLimiter(), NOW
    )

    assert result == "due:1 sent:0 failed:0 no_contact:1", result
    assert sender.sent == []


async def test_task_records_send_failure_and_keeps_going(session_factory) -> None:
    """发送失败落库为 failed 并计数，不抛出、不中断整批（DEC-021 未配凭据时的真实形态）。"""
    await _seed(session_factory, "u-3", email="b@e.com", end_at=NOW + timedelta(days=1))
    await _seed(session_factory, "u-4", email="c@e.com", end_at=NOW + timedelta(days=2))

    result = await run_renewal_reminders_task(
        session_factory, _settings(), RecordingSender(fail=True), NullLimiter(), NOW
    )

    assert result == "due:2 sent:0 failed:2 no_contact:0", result
    async with session_factory() as session:
        logs = (
            (await session.execute(select(NotificationSendLog).order_by(NotificationSendLog.target)))
            .scalars()
            .all()
        )
    assert [log.status for log in logs] == ["failed", "failed"]
    assert logs[0].error is not None and "DEC-021" in logs[0].error


# ----------------------------------------------------------------------
# 循环触发
# ----------------------------------------------------------------------
async def test_loop_fires_reminders_and_expiry_daily(
    session_factory, settings, monkeypatch
) -> None:
    """到点自动跑：提醒发出去、过期订阅被冻结。"""
    now = datetime.now(UTC)
    local = now + timedelta(hours=settings.scheduler_tz_offset_hours)
    monkeypatch.setattr(scheduler_loop, "MONTHLY_GRANT_DAY", -1)
    monkeypatch.setattr(scheduler_loop, "RENEWAL_REMIND_LOCAL_HOUR", local.hour)
    monkeypatch.setattr(scheduler_loop, "EXPIRE_LOCAL_HOUR", local.hour)
    monkeypatch.setattr(scheduler_loop, "RECONCILE_LOCAL_HOUR", -1)

    await _seed(session_factory, "u-loop", email="d@e.com", end_at=now + timedelta(days=2))
    async with UnitOfWork(session_factory) as uow:
        uow.session.add(
            Subscription(
                user_id="u-due",
                plan="monthly",
                status="active",
                start_at=now - timedelta(days=40),
                end_at=now - timedelta(days=1),
            )
        )
        uow.session.add(
            PointAccount(
                user_id="u-due", purchased_balance=5, monthly_balance=0, frozen=False, version=0
            )
        )
        await uow.session.flush()

    sender = RecordingSender()
    fast = settings.model_copy(update={"scheduler_tick_seconds": 1})
    task = asyncio.create_task(run_scheduler_loop(_fake_app(fast, session_factory, sender)))  # type: ignore[arg-type]
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert sender.sent == [("email", "renewal_reminder", "d@e.com")]
    async with UnitOfWork(session_factory) as uow:
        sub = (
            await uow.session.execute(
                select(Subscription).where(Subscription.user_id == "u-due")
            )
        ).scalar_one()
        account = (
            await uow.session.execute(
                select(PointAccount).where(PointAccount.user_id == "u-due")
            )
        ).scalar_one()
    assert sub.status == "expired"
    assert account.frozen is True
