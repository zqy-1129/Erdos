"""认证接口集成测试（SP2-2）：登录 / 轮换 / 设备吊销 / JWKS / RBAC。"""

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.infra.auth import (
    DevCredentialVerifier,
    Ed25519TokenManager,
    JwtTokenIntrospector,
    build_signing_keys,
)
from app.main import create_app
from app.repository.models import Base

# 进程内固定密钥：保证应用内签发与测试注入验签同源
_KEY = Ed25519PrivateKey.generate()
KEY_HEX = _KEY.private_bytes(
    serialization.Encoding.Raw,
    serialization.PrivateFormat.Raw,
    serialization.NoEncryption(),
).hex()

USERS = "alice:pw123:user,teacher;boss:secret123:operator,admin"


@pytest.fixture
async def auth_env(tmp_path):
    settings = Settings(
        env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}",
        auth_signing_keys=f"v1:{KEY_HEX}",
        auth_dev_users=USERS,
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


async def _login(client, username, password):
    return await client.post(
        "/v1/auth/login", json={"username": username, "password": password, "device_id": "d1"}
    )


async def test_login_issues_verifiable_access_token(auth_env) -> None:
    app, client = auth_env
    resp = await _login(client, "alice", "pw123")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["token_type"] == "bearer"
    assert data["expires_in"] == 900
    assert data["refresh_expires_in"] == 30 * 86400

    identity = app.state.token_manager.verify(data["access_token"])
    assert identity is not None
    assert identity.subject == "alice"
    assert identity.roles == ("user", "teacher")
    assert identity.device_id == "d1"


async def test_login_invalid_credentials(auth_env) -> None:
    _, client = auth_env
    resp = await _login(client, "alice", "wrong")
    assert resp.status_code == 401
    assert resp.json()["code"] == 40103


async def test_lockout_after_five_failures(auth_env) -> None:
    _, client = auth_env
    for _ in range(5):
        resp = await _login(client, "alice", "wrong")
        assert resp.json()["code"] == 40103
    resp = await _login(client, "alice", "pw123")  # 第 6 次即使密码正确也被锁
    assert resp.status_code == 423
    assert resp.json()["code"] == 42301


async def test_refresh_rotation_revokes_old_token(auth_env) -> None:
    _, client = auth_env
    login = (await _login(client, "alice", "pw123")).json()["data"]
    rotated = await client.post(
        "/v1/auth/refresh", json={"refresh_token": login["refresh_token"], "device_id": "d1"}
    )
    assert rotated.status_code == 200
    new_pair = rotated.json()["data"]
    assert new_pair["refresh_token"] != login["refresh_token"]

    replay = await client.post(
        "/v1/auth/refresh", json={"refresh_token": login["refresh_token"], "device_id": "d1"}
    )
    assert replay.status_code == 401
    assert replay.json()["code"] == 40104

    again = await client.post(
        "/v1/auth/refresh", json={"refresh_token": new_pair["refresh_token"], "device_id": "d1"}
    )
    assert again.status_code == 200


async def test_logout_revokes_device_tokens(auth_env) -> None:
    _, client = auth_env
    pair1 = (await _login(client, "alice", "pw123")).json()["data"]
    pair2 = (await _login(client, "alice", "pw123")).json()["data"]

    logout = await client.post(
        "/v1/auth/logout", json={"refresh_token": pair1["refresh_token"]}
    )
    assert logout.status_code == 200
    assert logout.json()["data"]["revoked"] == 2  # 同设备全部吊销

    refresh2 = await client.post(
        "/v1/auth/refresh", json={"refresh_token": pair2["refresh_token"]}
    )
    assert refresh2.json()["code"] == 40104
    # 幂等
    again = await client.post(
        "/v1/auth/logout", json={"refresh_token": pair1["refresh_token"]}
    )
    assert again.json()["data"]["revoked"] == 0


async def test_jwks_public_endpoint(auth_env) -> None:
    _, client = auth_env
    resp = await client.get("/v1/auth/jwks")
    assert resp.status_code == 200
    keys = resp.json()["data"]["keys"]
    assert len(keys) == 1
    assert keys[0]["kty"] == "OKP"
    assert keys[0]["kid"] == "v1"
    assert len(keys[0]["x"]) == 43


async def test_rbac_with_access_token(auth_env) -> None:
    _, client = auth_env
    alice = (await _login(client, "alice", "pw123")).json()["data"]
    boss = (await _login(client, "boss", "secret123")).json()["data"]

    headers = {"Authorization": f"Bearer {alice['access_token']}"}
    resp = await client.get("/v1/admin/dashboard/users/online", headers=headers)
    assert resp.json()["code"] == 40301  # user 角色无权访问管理端

    headers = {"Authorization": f"Bearer {boss['access_token']}"}
    resp = await client.get("/v1/admin/dashboard/users/online", headers=headers)
    assert resp.status_code == 200


def test_dev_users_parse_validates_roles() -> None:
    verifier = DevCredentialVerifier.parse("alice:pw:user,teacher")
    assert len(verifier._accounts) == 1  # noqa: SLF001 - 测试内部断言之需
    with pytest.raises(ValueError):
        DevCredentialVerifier.parse("alice:pw:superadmin")
    with pytest.raises(ValueError):
        DevCredentialVerifier.parse("malformed-entry")