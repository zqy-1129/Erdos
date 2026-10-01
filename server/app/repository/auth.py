"""刷新令牌仓储落库实现（条件吊销防并发轮换，无可读明文本体）。"""

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auth.ports import RefreshSession, RefreshTokenRepository
from app.repository.models import AuthRefreshToken


class SQLAlchemyRefreshTokenRepository(RefreshTokenRepository):
    """auth_refresh_tokens 表实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, token_hash: str) -> RefreshSession | None:
        stmt = select(AuthRefreshToken).where(AuthRefreshToken.token_hash == token_hash)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return RefreshSession(
            token_hash=row.token_hash,
            user_id=row.user_id,
            roles=tuple(row.roles or []),
            device_id=row.device_id,
            issued_at=_utc_required(row.issued_at),
            expires_at=_utc_required(row.expires_at),
            revoked_at=_ensure_utc(row.revoked_at),
            replaced_by_hash=row.replaced_by_hash,
        )

    async def save(self, session: RefreshSession) -> None:
        self._session.add(
            AuthRefreshToken(
                user_id=session.user_id,
                roles=list(session.roles),
                token_hash=session.token_hash,
                device_id=session.device_id,
                issued_at=session.issued_at,
                expires_at=session.expires_at,
                revoked_at=session.revoked_at,
                replaced_by_hash=session.replaced_by_hash,
            )
        )
        await self._session.flush()

    async def revoke_if_active(
        self,
        token_hash: str,
        revoked_at: datetime,
        replaced_by_hash: str | None,
    ) -> bool:
        stmt = (
            update(AuthRefreshToken)
            .where(
                AuthRefreshToken.token_hash == token_hash,
                AuthRefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=revoked_at, replaced_by_hash=replaced_by_hash)
        )
        result = await self._session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0) == 1

    async def revoke_device(
        self, subject: str, device_id: str, revoked_at: datetime
    ) -> int:
        stmt = (
            update(AuthRefreshToken)
            .where(
                AuthRefreshToken.user_id == subject,
                AuthRefreshToken.device_id == device_id,
                AuthRefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=revoked_at)
        )
        result = await self._session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)

    async def revoke_user(self, user_id: str, revoked_at: datetime) -> int:
        stmt = (
            update(AuthRefreshToken)
            .where(
                AuthRefreshToken.user_id == user_id,
                AuthRefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=revoked_at)
        )
        result = await self._session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)


def _ensure_utc(value: datetime | None) -> datetime | None:
    """SQLite 读回的 naive 时间戳按 UTC 解析（双驱动一致口径，历史教训）。"""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _utc_required(value: datetime | None) -> datetime:
    """非空时间列读取（表约束保证非 NULL；异常视为数据损坏）。"""
    if value is None:
        raise ValueError("非空时间列读取为 NULL")
    return _ensure_utc(value)  # type: ignore[return-value]  # 已排除 None