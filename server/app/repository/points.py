"""积分域仓储落库实现（point_accounts / point_ledgers / stage_grants）。

资金域关键：余额扣减用原子 UPDATE ... WHERE balance ≥ amount 防超扣；
乐观锁 version 供并发冲突检测；流水与许可以唯一约束做幂等兜底。
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.points.ports import (
    AccountBalance,
    BalanceType,
    GrantRecord,
    GrantRepository,
    LedgerRecord,
    LedgerRepository,
    PointAccountRepository,
)
from app.repository.models import PointAccount, PointLedger, StageGrant


def _new_id() -> str:
    return str(uuid.uuid4())


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _utc_required(value: datetime | None) -> datetime:
    if value is None:
        raise ValueError("非空时间列读取为 NULL")
    return _ensure_utc(value)  # type: ignore[return-value]


def _account(row: PointAccount) -> AccountBalance:
    return AccountBalance(
        user_id=row.user_id,
        purchased_balance=int(row.purchased_balance),
        monthly_balance=int(row.monthly_balance),
        frozen=bool(row.frozen),
        version=int(row.version),
    )


def _ledger(row: PointLedger) -> LedgerRecord:
    return LedgerRecord(
        id=row.id,
        user_id=row.user_id,
        exec_id=row.exec_id,
        delta=int(row.delta),
        balance_type=row.balance_type,
        kind=row.kind,
        status=row.status,
        source=row.source,
        task_id=row.task_id,
        stage=row.stage,
        created_at=_utc_required(row.created_at),
    )


def _grant(row: StageGrant) -> GrantRecord:
    return GrantRecord(
        exec_id=row.exec_id,
        user_id=row.user_id,
        task_id=row.task_id,
        stage=row.stage,
        points=int(row.points),
        signature=row.signature,
        key_version=row.key_version,
        issued_at=_utc_required(row.issued_at),
        expires_at=_utc_required(row.expires_at),
        status=row.status,
    )


class SQLAlchemyPointAccountRepository(PointAccountRepository):
    """point_accounts 表实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create(self, user_id: str, now: datetime) -> AccountBalance:
        row = (
            await self._session.execute(
                select(PointAccount).where(PointAccount.user_id == user_id)
            )
        ).scalar_one_or_none()
        if row is not None:
            return _account(row)
        account = PointAccount(
            user_id=user_id, purchased_balance=0, monthly_balance=0, frozen=False, version=0
        )
        self._session.add(account)
        await self._session.flush()
        return _account(account)

    async def get(self, user_id: str) -> AccountBalance | None:
        row = (
            await self._session.execute(
                select(PointAccount).where(PointAccount.user_id == user_id)
            )
        ).scalar_one_or_none()
        return _account(row) if row is not None else None

    async def credit(
        self,
        user_id: str,
        balance_type: BalanceType,
        amount: int,
        now: datetime,
    ) -> AccountBalance:
        if amount <= 0:
            raise ValueError("入账金额必须为正")
        column = (
            PointAccount.purchased_balance
            if balance_type is BalanceType.PURCHASED
            else PointAccount.monthly_balance
        )
        await self._session.execute(
            update(PointAccount)
            .where(PointAccount.user_id == user_id)
            .values({column: column + amount, PointAccount.version: PointAccount.version + 1, PointAccount.updated_at: now})
        )
        return await self._reload(user_id)

    async def debit(
        self,
        user_id: str,
        balance_type: BalanceType,
        amount: int,
        now: datetime,
    ) -> AccountBalance | None:
        if amount <= 0:
            raise ValueError("扣减金额必须为正")
        column = (
            PointAccount.purchased_balance
            if balance_type is BalanceType.PURCHASED
            else PointAccount.monthly_balance
        )
        result = await self._session.execute(
            update(PointAccount)
            .where(PointAccount.user_id == user_id, column >= amount)
            .values({column: column - amount, PointAccount.version: PointAccount.version + 1, PointAccount.updated_at: now})
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            return None
        return await self._reload(user_id)

    async def set_frozen(self, user_id: str, frozen: bool, now: datetime) -> bool:
        result = await self._session.execute(
            update(PointAccount)
            .where(PointAccount.user_id == user_id)
            .values(frozen=frozen, updated_at=now, version=PointAccount.version + 1)
        )
        return int(getattr(result, "rowcount", 0) or 0) == 1

    async def _reload(self, user_id: str) -> AccountBalance:
        row = (
            await self._session.execute(
                select(PointAccount).where(PointAccount.user_id == user_id)
            )
        ).scalar_one()
        return _account(row)


class SQLAlchemyLedgerRepository(LedgerRepository):
    """point_ledgers 表实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find(self, user_id: str, exec_id: str, kind: str) -> LedgerRecord | None:
        row = (
            await self._session.execute(
                select(PointLedger).where(
                    PointLedger.user_id == user_id,
                    PointLedger.exec_id == exec_id,
                    PointLedger.kind == kind,
                )
            )
        ).scalar_one_or_none()
        return _ledger(row) if row is not None else None

    async def append(self, record: LedgerRecord) -> LedgerRecord | None:
        """追加流水；幂等键（user_id, exec_id, kind）冲突时回滚并返回 None。

        并发下同 exec_id 竞态：先到者成功落库，后到者撞唯一约束，
        由本方法捕获并回滚，返回 None 供服务层走「重读已有」幂等路径。
        """
        row = PointLedger(
            id=_new_id() if not record.id else record.id,
            user_id=record.user_id,
            exec_id=record.exec_id,
            delta=record.delta,
            balance_type=record.balance_type,
            kind=record.kind,
            status=record.status,
            source=record.source,
            task_id=record.task_id,
            stage=record.stage,
            created_at=record.created_at,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError:
            await self._session.rollback()
            return None
        return _ledger(row)

    async def transition(
        self,
        user_id: str,
        exec_id: str,
        kind: str,
        to_status: str,
    ) -> LedgerRecord | None:
        result = await self._session.execute(
            update(PointLedger)
            .where(
                PointLedger.user_id == user_id,
                PointLedger.exec_id == exec_id,
                PointLedger.kind == kind,
                PointLedger.status == "reserved",
            )
            .values(status=to_status)
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            return None
        return await self.find(user_id, exec_id, kind)

    async def list_by_user(
        self, user_id: str, limit: int, offset: int
    ) -> tuple[list[LedgerRecord], int]:
        base = select(PointLedger).where(PointLedger.user_id == user_id)
        total = int(
            (
                await self._session.execute(
                    select(func.count()).select_from(PointLedger).where(
                        PointLedger.user_id == user_id
                    )
                )
            ).scalar_one()
        )
        rows = (
            (
                await self._session.execute(
                    base.order_by(PointLedger.created_at.desc())
                    .order_by(PointLedger.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return [_ledger(r) for r in rows], total


class SQLAlchemyGrantRepository(GrantRepository):
    """stage_grants 表实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find(self, exec_id: str) -> GrantRecord | None:
        row = (
            await self._session.execute(
                select(StageGrant).where(StageGrant.exec_id == exec_id)
            )
        ).scalar_one_or_none()
        return _grant(row) if row is not None else None

    async def append(self, record: GrantRecord) -> GrantRecord:
        row = StageGrant(
            exec_id=record.exec_id,
            user_id=record.user_id,
            task_id=record.task_id,
            stage=record.stage,
            points=record.points,
            signature=record.signature,
            key_version=record.key_version,
            issued_at=record.issued_at,
            expires_at=record.expires_at,
            status=record.status,
        )
        self._session.add(row)
        await self._session.flush()
        return _grant(row)
