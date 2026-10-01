"""认证授权领域服务（SP2-2）：登录/刷新轮换/设备吊销编排（无 SQL、无 JWT 细节）。"""

import hashlib
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta

from app.core.clock import utc_now
from app.core.errors import ACCOUNT_LOCKED, INVALID_CREDENTIALS, TOKEN_REVOKED, AppError
from app.domain.auth.lockout import LoginLockout
from app.domain.auth.ports import (
    AuthIdentity,
    CredentialVerifier,
    RefreshSession,
    RefreshTokenRepository,
    TokenManager,
    TokenPair,
)

_REFRESH_ENTROPY_BYTES = 48


class AuthService:
    """认证用例：双令牌签发、刷新轮换（旧令牌即吊销）与设备维度注销。"""

    def __init__(
        self,
        *,
        verifier: CredentialVerifier,
        tokens: TokenManager,
        lockout: LoginLockout,
        access_ttl_seconds: int,
        refresh_ttl_days: int,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._verifier = verifier
        self._tokens = tokens
        self._lockout = lockout
        self._access_ttl = access_ttl_seconds
        self._refresh_ttl = refresh_ttl_days
        self._now = now

    async def login(
        self,
        repo: RefreshTokenRepository,
        *,
        username: str,
        password: str,
        device_id: str | None,
        client_ip: str | None,
    ) -> TokenPair:
        """密码登录：防爆破检查 -> 凭据校验 -> 签发双令牌。"""
        if self._lockout.is_locked(username, client_ip or "-"):
            raise AppError(ACCOUNT_LOCKED)
        identity = await self._verifier.verify(username, password)
        if identity is None:
            self._lockout.register_failure(username, client_ip or "-")
            raise AppError(INVALID_CREDENTIALS)
        self._lockout.clear_account(username)
        return await self.issue_pair(repo, identity, device_id)

    async def refresh(
        self,
        repo: RefreshTokenRepository,
        *,
        refresh_token: str,
        device_id: str | None,
    ) -> TokenPair:
        """刷新轮换：旧刷新令牌立即吊销，返回新双令牌。"""
        session = await repo.get(hash_token(refresh_token))
        if session is None or session.revoked_at is not None:
            raise AppError(TOKEN_REVOKED)
        if session.expires_at <= self._now():
            raise AppError(TOKEN_REVOKED)
        if device_id is not None and session.device_id != device_id:
            raise AppError(TOKEN_REVOKED)
        new_refresh = new_refresh_token()
        new_hash = hash_token(new_refresh)
        revoked = await repo.revoke_if_active(
            session.token_hash, self._now(), replaced_by_hash=new_hash
        )
        if not revoked:
            raise AppError(TOKEN_REVOKED)  # 并发下已被他人轮换
        identity = AuthIdentity(
            subject=session.user_id, roles=session.roles, device_id=session.device_id
        )
        await self._save_refresh(repo, new_hash, identity)
        return self._pair(identity, new_refresh)

    async def logout(self, repo: RefreshTokenRepository, *, refresh_token: str) -> int:
        """注销（幂等）：携带设备标识时吊销该设备全部未吊销令牌。"""
        session = await repo.get(hash_token(refresh_token))
        if session is None or session.revoked_at is not None:
            return 0
        if session.device_id is not None:
            return await repo.revoke_device(
                session.user_id, session.device_id, self._now()
            )
        return 1 if await repo.revoke_if_active(session.token_hash, self._now(), None) else 0

    async def issue_pair(
        self, repo: RefreshTokenRepository, identity: AuthIdentity, device_id: str | None
    ) -> TokenPair:
        """为已认证主体签发双令牌（登录/注册即登等场景复用）。"""
        identity = AuthIdentity(
            subject=identity.subject, roles=identity.roles, device_id=device_id
        )
        refresh = new_refresh_token()
        await self._save_refresh(repo, hash_token(refresh), identity)
        return self._pair(identity, refresh)

    def _pair(self, identity: AuthIdentity, refresh_token: str) -> TokenPair:
        return TokenPair(
            access_token=self._tokens.issue_access(identity),
            refresh_token=refresh_token,
            expires_in=self._access_ttl,
            refresh_expires_in=self._refresh_ttl * 86400,
        )

    async def _save_refresh(
        self,
        repo: RefreshTokenRepository,
        token_hash: str,
        identity: AuthIdentity,
    ) -> None:
        now = self._now()
        await repo.save(
            RefreshSession(
                token_hash=token_hash,
                user_id=identity.subject,
                roles=identity.roles,
                device_id=identity.device_id,
                issued_at=now,
                expires_at=now + timedelta(days=self._refresh_ttl),
            )
        )


def hash_token(token: str) -> str:
    """令牌 -> SHA-256 十六进制（服务端永不落明文本体）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_refresh_token() -> str:
    """生成不可预测的刷新令牌。"""
    return secrets.token_urlsafe(_REFRESH_ENTROPY_BYTES)