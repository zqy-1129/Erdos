"""调度批次与对账数据源仓储实现（scheduler_runs + point_accounts/point_ledgers）。

对账口径：账户余额 = 有效流水净额（grant 入账 + reserve/offline_sync 扣减，
refunded 的 reserve 流水已返还故不计入）。
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.scheduler.ports import AccountLedgerSource, SchedulerRunRepository
from app.repository.models import PointAccount, PointLedger, SchedulerRun


def _new_id() -> str:
    return str(uuid.uuid4())


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class SQLAlchemySchedulerRunRepository(SchedulerRunRepository):
    """scheduler_runs 表实现（task_name + batch_key 幂等）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(
        self, task_name: str, batch_key: str, now: datetime, *, stale_after_seconds: int = 0
    ) -> bool:
        """占用批次：首次占用返回 True；已被占用返回 False。

        stale_after_seconds > 0 时，"running 且开始时间早于该阈值"的行视为上次执行崩在半路，
        允许被重新占用（reclaim）——否则月赠这类任务一旦在 claim 之后崩溃，批次键会永久占位、
        当月不再发放。重复发放的安全性不依赖这里，而在积分流水的 exec_id 幂等上。

        撞键只回滚到本次插入的保存点：早期实现直接 rollback 整个会话，会把调用方在同一事务里
        先写的业务数据一起抹掉——claim 一旦不是事务中的第一个动作就静默丢数据。
        """
        try:
            async with self._session.begin_nested():
                self._session.add(
                    SchedulerRun(
                        id=_new_id(), task_name=task_name, batch_key=batch_key,
                        status="running", started_at=now,
                    )
                )
                await self._session.flush()
            return True
        except IntegrityError:
            if stale_after_seconds <= 0:
                return False
            stale_before = now - timedelta(seconds=stale_after_seconds)
            result = await self._session.execute(
                update(SchedulerRun)
                .where(
                    SchedulerRun.task_name == task_name,
                    SchedulerRun.batch_key == batch_key,
                    SchedulerRun.status == "running",
                    SchedulerRun.started_at <= stale_before,
                )
                .values(started_at=now, result="reclaimed")
            )
            return int(getattr(result, "rowcount", 0) or 0) == 1

    async def mark_done(self, task_name: str, batch_key: str, result: str, now: datetime) -> None:
        await self._session.execute(
            update(SchedulerRun)
            .where(SchedulerRun.task_name == task_name, SchedulerRun.batch_key == batch_key)
            .values(status="done", result=result, finished_at=now)
        )

    async def prune_before(self, cutoff: datetime) -> int:
        """裁剪早于 cutoff 的批次行（查单去重账本按订单线性增长，必须有个尽头）。"""
        result = await self._session.execute(
            delete(SchedulerRun).where(SchedulerRun.started_at < cutoff)
        )
        return int(getattr(result, "rowcount", 0) or 0)


class SQLAlchemyAccountLedgerSource(AccountLedgerSource):
    """对账数据源：point_accounts + point_ledgers。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_accounts(self) -> list[tuple[str, int]]:
        rows = (
            (
                await self._session.execute(
                    select(PointAccount.user_id, PointAccount.purchased_balance, PointAccount.monthly_balance)
                )
            )
            .all()
        )
        return [(r.user_id, int(r.purchased_balance) + int(r.monthly_balance)) for r in rows]

    async def ledger_net(self, user_id: str) -> int:
        """有效流水净额：grant/offline_sync/reserve(非 refunded) 的 delta 之和。

        refunded 的 reserve 流水已返还余额，不计入净额。
        """
        rows = (
            (
                await self._session.execute(
                    select(PointLedger).where(PointLedger.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        net = 0
        for r in rows:
            # refunded 的 reserve 已返还，不计入
            if r.kind == "reserve" and r.status == "refunded":
                continue
            net += int(r.delta)
        return net
