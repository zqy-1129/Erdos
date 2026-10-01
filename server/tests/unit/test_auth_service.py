"""登录防爆破与认证服务编排测试（纯逻辑，内存仓储桩）。"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import ACCOUNT_LOCKED, INVALID_CREDENTIALS, TOKEN_REVOKED, AppError
from app.domain.auth.lockout import LoginLockout
from app.domain.auth.ports import AuthIdentity, RefreshSession
from app.domain.auth.service import AuthService, hash_token

# ---------- LoginLockout ----------

def _lockout(start: datetime) -> tuple[LoginLockout, dict]:
    clock = {"now": start}

    class Clock:
        @staticmethod
        def now() -> datetime:
            return clock["now"]

    return LoginLockout(threshold=5, lock_seconds=900, now=Clock.now), clock


def test_lockout_after_threshold_and_auto_unlock() -> None:
    start = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    lockout, clock = _lockout(start)

    for _ in range(5):  # 5 次失败 -> 武装锁定
        lockout.register_failure("alice", "1.2.3.4")
    assert lockout.is_locked("alice", "1.2.3.4") is True  # 第 6 次起被拒

    lockout.clear_account("alice")
    assert lockout.is_locked("alice", "5.6.7.8") is False  # 账号维度清零

    for _ in range(5):
        lockout.register_failure("alice", "1.2.3.4")
    clock["now"] = start + timedelta(seconds=901)  # 15 分钟到期
    assert lockout.is_locked("alice", "1.2.3.4") is False


def test_lockout_ip_dimension_independent() -> None:
    lockout, _ = _lockout(datetime(2026, 10, 1, 12, 0, tzinfo=UTC))
    for _ in range(5):
        lockout.register_failure("bob", "9.9.9.9")
    assert lockout.is_locked("charlie", "9.9.9.9") is True  # IP 维度锁住其他账号
    assert lockout.is_locked("bob", "8.8.8.8") is True  # 账号维度


# ---------- AuthService ----------

class StubVerifier:
    def __init__(self, passwords: dict[str, tuple[str, tuple[str, ...]]]) -> None:
        self._passwords = passwords

    async def verify(self, username: str, password: str) -> AuthIdentity | None:
        entry = self._passwords.get(username)
        if entry is None or entry[0] != password:
            return None
        return AuthIdentity(subject=username, roles=entry[1])


class StubTokens:
    def __init__(self) -> None:
        self.issued: list[AuthIdentity] = []

    def issue_access(self, identity: AuthIdentity) -> str:
        self.issued.append(identity)
        return f"access-for-{identity.subject}-{len(self.issued)}"

    def verify(self, token: str) -> AuthIdentity | None:  # pragma: no cover - 端口完整性
        raise NotImplementedError

    def public_jwks(self) -> dict:  # pragma: no cover - 端口完整性
        raise NotImplementedError


class StubRepo:
    def __init__(self) -> None:
        self.sessions: dict[str, RefreshSession] = {}

    async def get(self, token_hash: str) -> RefreshSession | None:
        return self.sessions.get(token_hash)

    async def save(self, session: RefreshSession) -> None:
        self.sessions[session.token_hash] = session

    async def revoke_if_active(
        self, token_hash: str, revoked_at: datetime, replaced_by_hash: str | None
    ) -> bool:
        session = self.sessions[token_hash]
        if session.revoked_at is not None:
            return False
        self.sessions[token_hash] = RefreshSession(
            token_hash=session.token_hash,
            user_id=session.user_id,
            roles=session.roles,
            device_id=session.device_id,
            issued_at=session.issued_at,
            expires_at=session.expires_at,
            revoked_at=revoked_at,
            replaced_by_hash=replaced_by_hash,
        )
        return True

    async def revoke_device(
        self, subject: str, device_id: str, revoked_at: datetime
    ) -> int:
        count = 0
        for token_hash, session in list(self.sessions.items()):
            if (
                session.user_id == subject
                and session.device_id == device_id
                and session.revoked_at is None
            ):
                self.sessions[token_hash] = RefreshSession(
                    token_hash=session.token_hash,
                    user_id=session.user_id,
                    roles=session.roles,
                    device_id=session.device_id,
                    issued_at=session.issued_at,
                    expires_at=session.expires_at,
                    revoked_at=revoked_at,
                    replaced_by_hash=None,
                )
                count += 1
        return count

    async def revoke_user(self, user_id: str, revoked_at: datetime) -> int:
        count = 0
        for token_hash, session in list(self.sessions.items()):
            if session.user_id == user_id and session.revoked_at is None:
                self.sessions[token_hash] = RefreshSession(
                    token_hash=session.token_hash,
                    user_id=session.user_id,
                    roles=session.roles,
                    device_id=session.device_id,
                    issued_at=session.issued_at,
                    expires_at=session.expires_at,
                    revoked_at=revoked_at,
                    replaced_by_hash=None,
                )
                count += 1
        return count


def _service(
    start: datetime,
) -> tuple[AuthService, StubTokens, StubRepo]:
    clock = {"now": start}
    verifier = StubVerifier({"alice": ("pw", ("user", "teacher"))})
    tokens = StubTokens()
    repo = StubRepo()
    lockout = LoginLockout(threshold=5, lock_seconds=900, now=lambda: clock["now"])
    service = AuthService(
        verifier=verifier,
        tokens=tokens,
        lockout=lockout,
        access_ttl_seconds=900,
        refresh_ttl_days=30,
        now=lambda: clock["now"],
    )
    return service, tokens, repo


async def test_login_success_issues_pair_and_clears_lockout() -> None:
    service, tokens, repo = _service(datetime(2026, 10, 1, 12, 0, tzinfo=UTC))
    pair = await service.login(
        repo, username="alice", password="pw", device_id="d1", client_ip="1.1.1.1"
    )
    assert pair.access_token.startswith("access-for-alice")
    assert len(pair.refresh_token) >= 32
    assert hash_token(pair.refresh_token) in repo.sessions
    assert tokens.issued[0].roles == ("user", "teacher")


async def test_login_failure_counts_and_error_codes() -> None:
    service, _, repo = _service(datetime(2026, 10, 1, 12, 0, tzinfo=UTC))
    for _ in range(5):
        with pytest.raises(AppError) as exc:
            await service.login(
                repo, username="alice", password="bad", device_id=None, client_ip="1.1.1.1"
            )
        assert exc.value.spec.code == INVALID_CREDENTIALS.code
    with pytest.raises(AppError) as exc:
        await service.login(
            repo, username="alice", password="pw", device_id=None, client_ip="1.1.1.1"
        )
    assert exc.value.spec.code == ACCOUNT_LOCKED.code


async def test_refresh_rotation_revokes_old_token() -> None:
    service, _, repo = _service(datetime(2026, 10, 1, 12, 0, tzinfo=UTC))
    pair = await service.login(
        repo, username="alice", password="pw", device_id="d1", client_ip="1.1.1.1"
    )
    rotated = await service.refresh(repo, refresh_token=pair.refresh_token, device_id="d1")
    assert rotated.refresh_token != pair.refresh_token
    with pytest.raises(AppError) as exc:
        await service.refresh(repo, refresh_token=pair.refresh_token, device_id="d1")
    assert exc.value.spec.code == TOKEN_REVOKED.code
    # 轮换续签保留角色与设备
    session = repo.sessions[hash_token(rotated.refresh_token)]
    assert session.user_id == "alice"
    assert session.roles == ("user", "teacher")
    assert session.device_id == "d1"


async def test_refresh_rejects_device_mismatch() -> None:
    service, _, repo = _service(datetime(2026, 10, 1, 12, 0, tzinfo=UTC))
    pair = await service.login(
        repo, username="alice", password="pw", device_id="d1", client_ip="1.1.1.1"
    )
    with pytest.raises(AppError) as exc:
        await service.refresh(repo, refresh_token=pair.refresh_token, device_id="d2")
    assert exc.value.spec.code == TOKEN_REVOKED.code


async def test_logout_revokes_device_and_is_idempotent() -> None:
    service, _, repo = _service(datetime(2026, 10, 1, 12, 0, tzinfo=UTC))
    pair1 = await service.login(
        repo, username="alice", password="pw", device_id="d1", client_ip="1.1.1.1"
    )
    pair2 = await service.login(
        repo, username="alice", password="pw", device_id="d1", client_ip="1.1.1.1"
    )
    revoked = await service.logout(repo, refresh_token=pair1.refresh_token)
    assert revoked == 2  # 同设备全部吊销
    with pytest.raises(AppError) as exc:
        await service.refresh(repo, refresh_token=pair2.refresh_token, device_id="d1")
    assert exc.value.spec.code == TOKEN_REVOKED.code
    assert await service.logout(repo, refresh_token=pair1.refresh_token) == 0  # 幂等