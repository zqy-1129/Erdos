"""调度批次与对账数据源仓储实现（scheduler_runs + point_accounts/point_ledgers）。

对账口径：账户余额 = 有效流水净额（grant 入账 + reserve/offline_sync 扣减，
refunded 的 reserve 流水已返还故不计入）。
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
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

    async def claim(self, task_name: str, batch_key: str, now: datetime) -> bool:
        row = SchedulerRun(
            id=_new_id(), task_name=task_name, batch_key=batch_key,
            status="running", started_at=now,
        )
        self._session.add(row)
        try:
            await self._session.flush()
            return True
        except IntegrityError:
            await self._session.rollback()
            return False

    async def mark_done(self, task_name: str, batch_key: str, result: str, now: datetime) -> None:
        from sqlalchemy import update

        await self._session.execute(
            update(SchedulerRun)
            .where(SchedulerRun.task_name == task_name, SchedulerRun.batch_key == batch_key)
            .values(status="done", result=result, finished_at=now)
        )


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
