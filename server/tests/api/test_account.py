"""账号域接口集成测试（SP2-3）：注册/防刷/登录/注销/改密/重置/设备。"""

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.config import Settings
from app.infra.auth import Ed25519TokenManager, JwtTokenIntrospector, build_signing_keys
from app.main import create_app
from app.repository.models import (
    Account,
    Base,
    DashboardEvent,
    Device,
    PointAccount,
    PointLedger,
    UsersDailyStats,
)

_KEY = Ed25519PrivateKey.generate()
KEY_HEX = _KEY.private_bytes(
    serialization.Encoding.Raw,
    serialization.PrivateFormat.Raw,
    serialization.NoEncryption(),
).hex()

REGISTER = {
    "email": "user1@example.com",
    "password": "abc12345",
    "fingerprint": "fp-aaa-111",
    "platform": "desktop",
}


@pytest.fixture
async def account_env(tmp_path):
    settings = Settings(
        env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'acc.db'}",
        auth_signing_keys=f"v1:{KEY_HEX}",
        auth_dev_users="",
        rate_limit_requests=1000,
        audit_rate_limit_requests=1000,
    )
    manager = Ed25519TokenManager(
        build_signing_keys(settings.auth_signing_keys, "test"),
        settings.auth_issuer,
        settings.auth_access_ttl_seconds,
    )
    app = create_app(settings, introspector=JwtTokenIntrospector(manager))
    async with app.state.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield app, client
    await app.state.engine.dispose()


async def _register(client, payload):
    return await client.post("/v1/auth/register", json=payload)


async def _login(client, username, password):
    return await client.post(
        "/v1/auth/login",
        json={"username": username, "password": password, "fingerprint": "fp-login-1"},
    )


async def _count(app, model) -> int:
    async with app.state.session_factory() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def test_register_creates_account_gift_and_daily_stats(account_env) -> None:
    app, client = account_env
    resp = await _register(client, REGISTER)
    assert resp.status_code == 200
    pair = resp.json()["data"]
    identity = app.state.token_manager.verify(pair["access_token"])
    assert identity is not None and identity.roles == ("user",)

    assert await _count(app, Account) == 1
    assert await _count(app, Device) == 1
    assert await _count(app, DashboardEvent) == 2  # account.register + points.grant
    async with app.state.session_factory() as session:
        stats = (await session.execute(select(UsersDailyStats))).scalars().all()
    assert stats[0].total_users == 1 and stats[0].new_users == 1


async def test_register_duplicate_identifier_conflict(account_env) -> None:
    _, client = account_env
    assert (await _register(client, REGISTER)).status_code == 200
    resp = await _register(client, {**REGISTER, "fingerprint": "fp-another"})
    assert resp.status_code == 409
    assert resp.json()["code"] == 40901


async def test_same_fingerprint_grants_gift_only_once(account_env) -> None:
    app, client = account_env
    await _register(client, REGISTER)
    resp = await _register(
        client, {**REGISTER, "email": "user2@example.com"}  # 同指纹新账号
    )
    assert resp.status_code == 200
    async with app.state.session_factory() as session:
        gifts = (
            await session.execute(
                select(DashboardEvent).where(DashboardEvent.type == "points.grant")
            )
        ).scalars().all()
    assert len(gifts) == 1  # 验收：同设备指纹多次注册只赠一次


async def test_register_grants_points_into_account(account_env) -> None:
    """SP2-4 积分域接入：注册赠分同步入账到积分账户（余额 + 流水）。"""
    app, client = account_env
    resp = await _register(client, REGISTER)
    assert resp.status_code == 200

    async with app.state.session_factory() as session:
        account = (
            await session.execute(select(PointAccount))
        ).scalar_one()
        ledgers = (
            await session.execute(select(PointLedger))
        ).scalars().all()

    assert account.purchased_balance == 100  # 注册赠 100 分
    assert account.monthly_balance == 0
    assert len(ledgers) == 1
    assert ledgers[0].kind == "grant"
    assert ledgers[0].source == "register_gift"
    assert ledgers[0].delta == 100
    assert ledgers[0].status == "confirmed"


async def test_register_weak_password_rejected(account_env) -> None:
    _, client = account_env
    resp = await _register(client, {**REGISTER, "password": "abcdefgh"})
    assert resp.json()["code"] == 40001


async def test_deactivated_account_login_rejected_and_tokens_revoked(account_env) -> None:
    app, client = account_env
    pair = (await _register(client, REGISTER)).json()["data"]
    headers = {"Authorization": f"Bearer {pair['access_token']}"}

    resp = await client.delete("/v1/account", headers=headers)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["deactivated"] is True and body["revoked"] >= 1

    # 注销后登录被拒（匿名化后标识不存在 -> 40103；状态冻结保留标识 -> 40302），刷新令牌失效（40104）
    login = await _login(client, "user1@example.com", "abc12345")
    assert login.json()["code"] in (40103, 40302)
    refresh = await client.post(
        "/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert refresh.json()["code"] == 40104
    # 注销幂等：重复注销不再变更
    again = await client.delete("/v1/account", headers=headers)
    assert again.json()["data"]["deactivated"] is False

    async with app.state.session_factory() as session:
        row = (await session.execute(select(Account))).scalar_one()
    assert row.email is None and row.password_hash == "" and row.deleted_at is not None


async def test_change_password_revokes_sessions(account_env) -> None:
    _, client = account_env
    pair = (await _register(client, REGISTER)).json()["data"]
    headers = {"Authorization": f"Bearer {pair['access_token']}"}

    resp = await client.post(
        "/v1/account/password",
        json={"old_password": "abc12345", "new_password": "xyz98765"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert (await _login(client, "user1@example.com", "abc12345")).json()["code"] == 40103
    assert (await _login(client, "user1@example.com", "xyz98765")).status_code == 200
    refresh = await client.post(
        "/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert refresh.json()["code"] == 40104


async def test_password_reset_full_flow(account_env) -> None:
    app, client = account_env
    pair = (await _register(client, REGISTER)).json()["data"]

    resp = await client.post(
        "/v1/auth/password/reset/request", json={"identifier": "user1@example.com"}
    )
    assert resp.status_code == 200
    token = app.state.reset_notifier.sent[-1][1]

    # 60s 内重发 -> 429（不同标识不受影响）
    again = await client.post(
        "/v1/auth/password/reset/request", json={"identifier": "user1@example.com"}
    )
    assert again.json()["code"] == 42901
    other = await client.post(
        "/v1/auth/password/reset/request", json={"identifier": "other@x.com"}
    )
    assert other.status_code == 200

    resp = await client.post(
        "/v1/auth/password/reset/confirm",
        json={"reset_token": token, "new_password": "zxc12345"},
    )
    assert resp.status_code == 200
    assert (await _login(client, "user1@example.com", "zxc12345")).status_code == 200
    refresh = await client.post(
        "/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert refresh.json()["code"] == 40104  # 重置后全部会话吊销
    # 令牌一次性 & 无效令牌
    resp = await client.post(
        "/v1/auth/password/reset/confirm",
        json={"reset_token": token, "new_password": "zxc12345"},
    )
    assert resp.json()["code"] == 40002

    # 不存在账号：受理但确认失败（防枚举）
    await client.post(
        "/v1/auth/password/reset/request", json={"identifier": "ghost@x.com"}
    )
    ghost_token = app.state.reset_notifier.sent[-1][1]
    resp = await client.post(
        "/v1/auth/password/reset/confirm",
        json={"reset_token": ghost_token, "new_password": "zxc12345"},
    )
    assert resp.json()["code"] == 40002


async def test_frozen_account_login_returns_40302(account_env) -> None:
    """运营冻结场景：标识与凭据保留但状态非 active -> 登录返回 ACCOUNT_FROZEN。"""
    app, client = account_env
    from app.infra.auth import BcryptPasswordHasher
    from app.repository.models import Account as AccountModel

    hasher = BcryptPasswordHasher()
    async with app.state.session_factory() as session:
        session.add(
            AccountModel(
                email="frozen@example.com",
                password_hash=hasher.hash("abc12345"),
                status="frozen",
                role="user",
            )
        )
        await session.commit()
    resp = await _login(client, "frozen@example.com", "abc12345")
    assert resp.json()["code"] == 40302


async def test_profile_and_devices(account_env) -> None:
    _, client = account_env
    pair = (await _register(client, REGISTER)).json()["data"]
    headers = {"Authorization": f"Bearer {pair['access_token']}"}

    profile = await client.get("/v1/account/profile", headers=headers)
    assert profile.status_code == 200
    assert profile.json()["data"]["email"] == "user1@example.com"
    assert profile.json()["data"]["role"] == "user"

    devices = await client.get("/v1/account/devices", headers=headers)
    assert devices.status_code == 200
    items = devices.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["first_gift_used"] is False

    anon = await client.get("/v1/account/profile")
    assert anon.json()["code"] == 40101