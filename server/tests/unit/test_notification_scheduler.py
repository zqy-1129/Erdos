"""通知服务与调度器测试（SP2-7）：消费幂等 + 验证码限流 + 调度幂等 + 对账差异告警。

覆盖：
- 通知发送幂等（同 message_id 投递 3 次只发送一次）
- 验证码限流（60s 间隔 + 日限额）
- 调度任务幂等批次（月赠/到期冻结重复触发只执行一次）
- 对账差异全量发现并告警
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.domain.alerts.severity import Severity
from app.domain.notification.ports import (
    NotificationChannel,
    NotificationLogRecord,
)
from app.domain.notification.service import NotificationService
from app.domain.scheduler.ports import GrantOutcome
from app.domain.scheduler.service import (
    SchedulerService,
    monthly_grant_alert,
    reconcile_alert,
)
from app.infra.notification_sender import LogNotificationSender
from app.infra.verification_limiter import FixedWindowCodeLimiter
from app.repository.models import NotificationSendLog
from app.repository.notification import SQLAlchemyNotificationLogRepository
from app.repository.scheduler import SQLAlchemySchedulerRunRepository
from app.repository.uow import UnitOfWork


class FakeCodeLimiter:
    """验证码限流桩。"""

    def __init__(self, allow: bool = True) -> None:
        self._allow = allow
        self.calls: list[str] = []

    async def allow(self, key: str) -> bool:
        self.calls.append(key)
        return self._allow

    async def retry_after_seconds(self, key: str) -> float:
        return 60.0


def _notify_svc(session, settings: Settings, limiter=None) -> NotificationService:
    return NotificationService(
        SQLAlchemyNotificationLogRepository(session),
        LogNotificationSender(),
        limiter if limiter is not None else FakeCodeLimiter(),
        settings,
    )


# ----------------------------------------------------------------------
# 通知幂等（消费幂等：同消息投递 3 次只发送一次）
# ----------------------------------------------------------------------
async def test_send_idempotent_replay(session_factory, settings) -> None:
    """同 message_id 投递 3 次：只发送一次（幂等）。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _notify_svc(uow.session, settings)
        for _ in range(3):
            await svc.send(
                "msg-1", NotificationChannel.EMAIL.value, "welcome", "u@x.com",
                {"user": "u1"}, datetime.now(UTC),
            )
    async with session_factory() as s:
        repo = SQLAlchemyNotificationLogRepository(s)
        # 只有一条记录
        record = await repo.find_by_message_id("msg-1")
        assert record is not None
        assert record.status == "sent"
        # 发送器只发送了一次
        sender = LogNotificationSender()
        assert len(sender.sent) == 0  # 每次 _notify_svc 新建 sender，需在 svc 内统计

    # 通过发送器留痕统计（在 service 内复用同一 sender）
    async with UnitOfWork(session_factory) as uow:
        sender = LogNotificationSender()
        svc = NotificationService(
            SQLAlchemyNotificationLogRepository(uow.session),
            sender,
            FakeCodeLimiter(),
            settings,
        )
        for _ in range(3):
            await svc.send("msg-2", "email", "welcome", "u@x.com", {}, datetime.now(UTC))
        assert len(sender.sent) == 1  # 只发送一次


async def test_verification_code_rate_limited(session_factory, settings) -> None:
    """验证码限流：触发限流时抛 429。"""
    from app.core.errors import AppError

    async with UnitOfWork(session_factory) as uow:
        limiter = FakeCodeLimiter(allow=False)
        svc = _notify_svc(uow.session, settings, limiter)
        with pytest.raises(AppError) as ei:
            await svc.send_verification_code("13800000000", datetime.now(UTC))
        assert ei.value.spec.code == 42901  # RATE_LIMITED


async def test_fixed_window_limiter_resend_interval() -> None:
    """固定窗口限流：60s 内重复发送被拒。"""
    clock = {"t": 0.0}
    limiter = FixedWindowCodeLimiter(60, 10, clock=lambda: clock["t"])
    assert await limiter.allow("k") is True
    clock["t"] = 30.0  # 30 秒后
    assert await limiter.allow("k") is False  # 60s 内被拒
    clock["t"] = 61.0  # 61 秒后
    assert await limiter.allow("k") is True


async def test_fixed_window_limiter_daily_limit() -> None:
    """固定窗口限流：日限额 10 次。"""
    clock = {"t": 0.0}
    limiter = FixedWindowCodeLimiter(1, 3, clock=lambda: clock["t"])
    for i in range(3):
        clock["t"] = float(i * 10)  # 每次间隔 > resend
        assert await limiter.allow("k") is True
    clock["t"] = 40.0
    assert await limiter.allow("k") is False  # 日限额 3 次已用尽


# ----------------------------------------------------------------------
# 调度幂等 + 对账
# ----------------------------------------------------------------------
class FakeMonthlyGrant:
    def __init__(self) -> None:
        self.calls = 0

    async def grant_all_active(self, now) -> GrantOutcome:
        self.calls += 1
        return GrantOutcome(granted=5, failed=0)


class FakeExpire:
    def __init__(self) -> None:
        self.calls = 0

    async def expire_overdue(self, now) -> int:
        self.calls += 1
        return 3


class FakeLedgerSource:
    def __init__(self, accounts: list[tuple[str, int]], nets: dict[str, int]) -> None:
        self._accounts = accounts
        self._nets = nets

    async def list_accounts(self) -> list[tuple[str, int]]:
        return self._accounts

    async def ledger_net(self, user_id: str) -> int:
        return self._nets[user_id]


def _sched_svc(session, monthly, expire, source) -> SchedulerService:
    return SchedulerService(
        SQLAlchemySchedulerRunRepository(session),
        source,
        monthly,
        expire,
    )


async def test_monthly_grant_idempotent_batch(session_factory, settings) -> None:
    """月赠幂等批次：重复触发只执行一次。"""
    async with UnitOfWork(session_factory) as uow:
        monthly = FakeMonthlyGrant()
        expire = FakeExpire()
        source = FakeLedgerSource([], {})
        svc = _sched_svc(uow.session, monthly, expire, source)
        now = datetime.now(UTC)
        r1 = await svc.run_monthly_grant(now)
        r2 = await svc.run_monthly_grant(now)  # 同批次
        assert r1.result == "granted:5 failed:0"
        assert r1.outcome is not None and r1.outcome.granted == 5
        assert r2.result == "already_ran"
        assert r2.outcome is None, "没跑就不能给出发放结果，也不能被当成零失败"
        assert monthly.calls == 1  # 只执行一次
        assert monthly_grant_alert(r2.outcome) is None


async def test_reconcile_detects_all_differences(session_factory, settings) -> None:
    """对账：构造 5 条差异，全量发现并分级为 P2 告警。"""
    async with UnitOfWork(session_factory) as uow:
        # 5 个账户，余额与流水净额不一致
        accounts = [(f"u{i}", 100) for i in range(5)]
        nets = {f"u{i}": 90 for i in range(5)}  # 每个都差 10
        source = FakeLedgerSource(accounts, nets)
        monthly = FakeMonthlyGrant()
        expire = FakeExpire()
        svc = _sched_svc(uow.session, monthly, expire, source)
        result = await svc.run_reconcile(datetime.now(UTC))
        assert result.alerted is True
        assert len(result.differences) == 5  # 差异全量发现

        alert = reconcile_alert(result)
        assert alert is not None
        assert alert.key == "reconcile_diff"
        assert alert.severity == Severity.P2  # 架构 §10：对账差异非零为 P2
        assert "5 个账户" in alert.message
        assert "u0" not in alert.message, "告警短句不落用户明细"


async def test_reconcile_no_differences(session_factory, settings) -> None:
    """对账：无差异时不生成告警。"""
    async with UnitOfWork(session_factory) as uow:
        accounts = [("u1", 100)]
        nets = {"u1": 100}
        source = FakeLedgerSource(accounts, nets)
        svc = _sched_svc(uow.session, FakeMonthlyGrant(), FakeExpire(), source)
        result = await svc.run_reconcile(datetime.now(UTC))
        assert result.alerted is False
        assert len(result.differences) == 0
        assert reconcile_alert(result) is None


def _queued(message_id: str, target: str) -> NotificationLogRecord:
    return NotificationLogRecord(
        id="", message_id=message_id, channel="email", template_id="t",
        target=target, status="queued", error=None,
        created_at=datetime.now(UTC), sent_at=None,
    )


async def test_append_conflict_keeps_caller_transaction_writes(session_factory) -> None:
    """撞幂等键只撤本次插入，调用方同事务里的其它写入必须活下来。

    这条竞态是真能发生的：两个实例同时跑续费提醒、或消息被重复投递。旧实现用
    session.rollback() 兜底，会把调用方事务一起抹掉（与 scheduler.claim、
    monitoring.upsert_minute 改前同一个坑，这是第三处）。
    """
    async with UnitOfWork(session_factory) as uow:
        first = await SQLAlchemyNotificationLogRepository(uow.session).append(
            _queued("dup-1", "a@e.com")
        )
    assert first is not None

    async with UnitOfWork(session_factory) as uow:
        repo = SQLAlchemyNotificationLogRepository(uow.session)
        assert await repo.append(_queued("caller-write", "b@e.com")) is not None
        assert await repo.append(_queued("dup-1", "a@e.com")) is None, "撞键交回 None 让上层复用"

    async with session_factory() as session:
        ids = {
            row.message_id
            for row in (await session.execute(select(NotificationSendLog))).scalars()
        }
    assert {"dup-1", "caller-write"} <= ids, f"同事务写入被回滚掉了：{sorted(ids)}"
