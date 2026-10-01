"""账号域仓储落库实现（users/devices，与《数据模型设计》4.1/4.3 对齐）。"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.account.ports import (
    AccountRecord,
    AccountRepository,
    DeviceCard,
    DeviceRepository,
)
from app.repository.models import Account, Device


def _new_id() -> str:
    return str(uuid.uuid4())


class SQLAlchemyAccountRepository(AccountRepository):
    """users 表实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, record: AccountRecord) -> AccountRecord:
        row = Account(
            id=_new_id(),
            email=record.email,
            phone=record.phone,
            password_hash=record.password_hash,
            status=record.status,
            role=record.role,
            created_at=record.created_at,
            deleted_at=record.deleted_at,
        )
        self._session.add(row)
        await self._session.flush()
        return _record(row)

    async def find_by_identifier(self, identifier: str) -> AccountRecord | None:
        stmt = (
            select(Account).where(Account.email == identifier)
            if "@" in identifier
            else select(Account).where(Account.phone == identifier)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _record(row) if row is not None else None

    async def get(self, user_id: str) -> AccountRecord | None:
        row = (
            await self._session.execute(select(Account).where(Account.id == user_id))
        ).scalar_one_or_none()
        return _record(row) if row is not None else None

    async def update_password(self, user_id: str, password_hash: str) -> bool:
        result = await self._session.execute(
            update(Account)
            .where(Account.id == user_id)
            .values(password_hash=password_hash)
        )
        return int(getattr(result, "rowcount", 0) or 0) == 1

    async def anonymize(self, user_id: str, deleted_at: datetime) -> bool:
        result = await self._session.execute(
            update(Account)
            .where(Account.id == user_id, Account.deleted_at.is_(None))
            .values(
                email=None,
                phone=None,
                password_hash="",
                status="frozen",
                deleted_at=deleted_at,
            )
        )
        return int(getattr(result, "rowcount", 0) or 0) == 1


class SQLAlchemyDeviceRepository(DeviceRepository):
    """devices 表实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists_fingerprint(self, fingerprint: str) -> bool:
        stmt = select(Device.id).where(Device.fingerprint == fingerprint).limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def register(
        self,
        user_id: str,
        fingerprint: str,
        platform: str | None,
        first_gift_used: bool,
        now: datetime,
    ) -> str:
        device = Device(
            id=_new_id(),
            user_id=user_id,
            fingerprint=fingerprint,
            platform=platform,
            last_seen_at=now,
            first_gift_used=first_gift_used,
        )
        self._session.add(device)
        await self._session.flush()
        return device.id

    async def touch(
        self,
        user_id: str,
        fingerprint: str,
        platform: str | None,
        now: datetime,
    ) -> None:
        stmt = select(Device).where(
            Device.user_id == user_id, Device.fingerprint == fingerprint
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is not None:
            row.last_seen_at = now
            if platform:
                row.platform = platform
            await self._session.flush()
            return
        await self.register(
            user_id=user_id,
            fingerprint=fingerprint,
            platform=platform,
            first_gift_used=await self.exists_fingerprint(fingerprint),
            now=now,
        )

    async def list_by_user(self, user_id: str) -> list[DeviceCard]:
        stmt = (
            select(Device)
            .where(Device.user_id == user_id)
            .order_by(Device.last_seen_at.desc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [
            DeviceCard(
                id=row.id,
                platform=row.platform,
                last_seen_at=_utc_required(row.last_seen_at),
                first_gift_used=bool(row.first_gift_used),
            )
            for row in rows
        ]


def _record(row: Account) -> AccountRecord:
    return AccountRecord(
        id=row.id,
        email=row.email,
        phone=row.phone,
        password_hash=row.password_hash,
        status=row.status,
        role=row.role,
        created_at=_utc_required(row.created_at),
        deleted_at=_ensure_utc(row.deleted_at),
    )


def _ensure_utc(value: datetime | None) -> datetime | None:
    """SQLite 读回 naive 时间戳按 UTC 解析（双驱动一致口径）。"""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _utc_required(value: datetime | None) -> datetime:
    """非空时间列读取（表约束保证非 NULL；异常视为数据损坏）。"""
    if value is None:
        raise ValueError("非空时间列读取为 NULL")
    return _ensure_utc(value)  # type: ignore[return-value]  # 已排除 None