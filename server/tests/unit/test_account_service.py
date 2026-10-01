"""账号域服务测试（SP2-3）：注册防刷、注销、密码强度与重置流程。"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import (
    BAD_REQUEST,
    CONFLICT,
    RATE_LIMITED,
    RESET_TOKEN_INVALID,
    AppError,
)
from app.domain.account.ports import (
    AccountRecord,
    RegistrationRequest,
)
from app.domain.account.reset import PasswordResetService
from app.domain.account.service import (
    DeactivateService,
    PasswordPolicy,
    RegisterService,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class StubHasher:
    def __init__(self) -> None:
        self.hashed: list[str] = []

    def hash(self, plain: str) -> str:
        self.hashed.append(plain)
        return f"bcrypt<{plain}>"

    def verify(self, plain: str, hashed: str) -> bool:
        return hashed == f"bcrypt<{plain}>"


class StubAccounts:
    def __init__(self, existing: list[AccountRecord] | None = None) -> None:
        self.records: dict[str, AccountRecord] = {
            r.id: r for r in (existing or [])
        }
        self.by_identifier: dict[str, AccountRecord] = {}
        self.created: list[AccountRecord] = []
        self.passwords: dict[str, str] = {}
        self.deleted: list[tuple[str, datetime]] = []

    async def create(self, record: AccountRecord) -> AccountRecord:
        created = AccountRecord(
            id=f"u-{len(self.created) + 1}",
            email=record.email,
            phone=record.phone,
            password_hash=record.password_hash,
            status=record.status,
            role=record.role,
            created_at=record.created_at,
            deleted_at=record.deleted_at,
        )
        self.created.append(created)
        self.records[created.id] = created
        if created.email:
            self.by_identifier[created.email] = created
        if created.phone:
            self.by_identifier[created.phone] = created
        return created

    async def find_by_identifier(self, identifier: str) -> AccountRecord | None:
        return self.by_identifier.get(identifier)

    async def get(self, user_id: str) -> AccountRecord | None:
        return self.records.get(user_id)

    async def update_password(self, user_id: str, password_hash: str) -> bool:
        record = self.records.get(user_id)
        if record is None:
            return False
        self.passwords[user_id] = password_hash
        return True

    async def anonymize(self, user_id: str, deleted_at: datetime) -> bool:
        record = self.records.get(user_id)
        if record is None or record.deleted_at is not None:
            return False
        self.deleted.append((user_id, deleted_at))
        self.records[user_id] = AccountRecord(
            id=record.id,
            email=None,
            phone=None,
            password_hash="",
            status="frozen",
            role=record.role,
            created_at=record.created_at,
            deleted_at=deleted_at,
        )
        return True


class StubDevices:
    def __init__(self, fingerprints: set[str] | None = None) -> None:
        self.seen = fingerprints or set()
        self.registered: list[tuple[str, str, bool]] = []

    async def exists_fingerprint(self, fingerprint: str) -> bool:
        return fingerprint in self.seen

    async def register(
        self,
        user_id: str,
        fingerprint: str,
        platform: str | None,
        first_gift_used: bool,
        now: datetime,
    ) -> str:
        self.seen.add(fingerprint)
        self.registered.append((user_id, fingerprint, first_gift_used))
        return f"d-{len(self.registered)}"

    async def touch(self, user_id, fingerprint, platform, now) -> None:
        self.seen.add(fingerprint)


class StubNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, identifier: str, token: str) -> None:
        self.sent.append((identifier, token))


def _request(email="a@b.com", password="abc12345", fingerprint="fp-111111", phone=None):
    return RegistrationRequest(
        email=email, phone=phone, password=password,
        fingerprint=fingerprint, platform="desktop", client_ip="1.1.1.1",
    )


# ---------- PasswordPolicy ----------

def test_password_policy_enforces_strength() -> None:
    policy = PasswordPolicy(8)
    policy.validate("abc12345")  # ok
    with pytest.raises(AppError) as exc:
        policy.validate("short1")
    assert exc.value.spec.code == BAD_REQUEST.code
    with pytest.raises(AppError):
        policy.validate("allletters")


# ---------- RegisterService ----------

async def test_register_grants_gift_for_new_fingerprint() -> None:
    accounts = StubAccounts()
    devices = StubDevices()
    service = RegisterService(accounts, devices, StubHasher(), PasswordPolicy(8))
    result = await service.register(_request(), NOW)
    assert result.gift_granted is True
    assert result.account.id.startswith("u-")
    assert devices.registered[-1][2] is False  # 首次指纹未标记已用


async def test_register_same_fingerprint_granted_once() -> None:
    accounts = StubAccounts()
    devices = StubDevices(fingerprints={"fp-111111"})
    service = RegisterService(accounts, devices, StubHasher(), PasswordPolicy(8))
    result = await service.register(_request(email="x@y.com"), NOW)
    assert result.gift_granted is False
    assert devices.registered[-1][2] is True


async def test_register_conflict_on_existing_identifier() -> None:
    existing = AccountRecord(
        id="u-0", email="a@b.com", phone=None, password_hash="h",
        status="active", role="user", created_at=NOW, deleted_at=None,
    )
    accounts = StubAccounts(existing=[existing])
    accounts.by_identifier["a@b.com"] = accounts.records["u-0"]
    service = RegisterService(accounts, StubDevices(), StubHasher(), PasswordPolicy(8))
    with pytest.raises(AppError) as exc:
        await service.register(_request(), NOW)
    assert exc.value.spec.code == CONFLICT.code


async def test_register_requires_valid_identifier_and_password() -> None:
    service = RegisterService(StubAccounts(), StubDevices(), StubHasher(), PasswordPolicy(8))
    with pytest.raises(AppError) as exc:
        await service.register(_request(email="bad-email"), NOW)
    assert exc.value.spec.code == BAD_REQUEST.code


# ---------- DeactivateService ----------

async def test_deactivate_anonymizes_and_is_idempotent() -> None:
    record = AccountRecord(
        id="u-1", email="a@b.com", phone=None, password_hash="h",
        status="active", role="user", created_at=NOW, deleted_at=None,
    )
    accounts = StubAccounts(existing=[record])
    service = DeactivateService(accounts)
    assert await service.deactivate("u-1", NOW) is True
    frozen = accounts.records["u-1"]
    assert frozen.email is None and frozen.password_hash == ""
    assert frozen.status == "frozen" and frozen.deleted_at is not None
    assert await service.deactivate("u-1", NOW) is False  # 幂等


# ---------- PasswordResetService ----------

def _reset_service(accounts: StubAccounts, notifier: StubNotifier, clock: dict) -> PasswordResetService:
    return PasswordResetService(
        StubHasher(),
        PasswordPolicy(8),
        notifier,
        token_ttl_seconds=900,
        resend_seconds=60,
        daily_limit=10,
        now=lambda: clock["now"],
    )


async def test_reset_request_and_confirm_success() -> None:
    record = AccountRecord(
        id="u-1", email="a@b.com", phone=None, password_hash="h",
        status="active", role="user", created_at=NOW, deleted_at=None,
    )
    accounts = StubAccounts(existing=[record])
    accounts.by_identifier["a@b.com"] = accounts.records["u-1"]
    notifier = StubNotifier()
    clock = {"now": NOW}
    service = _reset_service(accounts, notifier, clock)

    await service.request(accounts, "a@b.com")
    token = notifier.sent[-1][1]
    user_id = await service.confirm(accounts, token, "newpass123")
    assert user_id == "u-1"
    # 令牌一次性
    with pytest.raises(AppError):
        await service.confirm(accounts, token, "newpass123")


async def test_reset_request_rate_limited_resend() -> None:
    notifier = StubNotifier()
    clock = {"now": NOW}
    service = _reset_service(StubAccounts(), notifier, clock)
    await service.request(StubAccounts(), "a@b.com")
    with pytest.raises(AppError) as exc:
        await service.request(StubAccounts(), "a@b.com")
    assert exc.value.spec.code == RATE_LIMITED.code
    clock["now"] = NOW + timedelta(seconds=61)
    await service.request(StubAccounts(), "a@b.com")  # 窗口后可重发


async def test_reset_confirm_unknown_account_and_expired_token() -> None:
    notifier = StubNotifier()
    clock = {"now": NOW}
    accounts = StubAccounts()
    service = _reset_service(accounts, notifier, clock)
    await service.request(accounts, "ghost@x.com")
    token = notifier.sent[-1][1]
    with pytest.raises(AppError) as exc:
        await service.confirm(accounts, token, "newpass123")
    assert exc.value.spec.code == RESET_TOKEN_INVALID.code

    with pytest.raises(AppError):
        await service.confirm(accounts, "not-a-token", "newpass123")

    # 令牌过期
    record = AccountRecord(
        id="u-1", email="a@b.com", phone=None, password_hash="h",
        status="active", role="user", created_at=NOW, deleted_at=None,
    )
    accounts2 = StubAccounts(existing=[record])
    accounts2.by_identifier["a@b.com"] = accounts2.records["u-1"]
    clock2 = {"now": NOW}
    service2 = _reset_service(accounts2, notifier, clock2)
    await service2.request(accounts2, "a@b.com")
    token2 = notifier.sent[-1][1]
    clock2["now"] = NOW + timedelta(seconds=901)
    with pytest.raises(AppError) as exc:
        await service2.confirm(accounts2, token2, "newpass123")
    assert exc.value.spec.code == RESET_TOKEN_INVALID.code