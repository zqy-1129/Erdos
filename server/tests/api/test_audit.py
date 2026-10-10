"""审计事件接口集成测试（幂等/校验/统一信封/角色限制）。

写入端点已收紧为 admin|operator：actor_type/actor_id 来自请求体，无角色限制即任何人可伪造
他人审计记录（见 tests/api/test_authz_matrix.py 的越权面断言）。
"""

from app.core.errors import ERROR_SPECS

ADMIN = {"Authorization": "Bearer admin"}

PAYLOAD = {
    "actor_type": "user",
    "actor_id": "u1",
    "action": "account.login",
    "resource_type": "account",
    "resource_id": "r1",
    "detail": {"mfa": False},
    "client_ip": "1.2.3.4",
}


async def test_create_audit_event_success(admin_client) -> None:
    resp = await admin_client.post(
        "/v1/audit/events", json=PAYLOAD, headers={"Idempotency-Key": "k-1", **ADMIN}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["id"] == 1
    assert data["action"] == "account.login"
    assert data["client_ip"] in ("1.2.3.4", "127.0.0.1")
    assert data["created_at"]


async def test_plain_user_cannot_write_audit(client) -> None:
    """越权面：普通用户写审计必须 403（不得以他人身份落审计）。"""
    resp = await client.post(
        "/v1/audit/events",
        json=PAYLOAD,
        headers={"Idempotency-Key": "k-forge", "Authorization": "Bearer u1"},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == ERROR_SPECS["PERMISSION_DENIED"].code


async def test_idempotent_duplicate_conflict(admin_client) -> None:
    headers = {"Idempotency-Key": "k-dup", **ADMIN}
    assert (
        await admin_client.post("/v1/audit/events", json=PAYLOAD, headers=headers)
    ).status_code == 200
    resp = await admin_client.post("/v1/audit/events", json=PAYLOAD, headers=headers)
    assert resp.status_code == 409
    assert resp.json()["code"] == ERROR_SPECS["CONFLICT"].code


async def test_missing_idempotency_key_rejected(admin_client) -> None:
    resp = await admin_client.post("/v1/audit/events", json=PAYLOAD, headers=ADMIN)
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == ERROR_SPECS["BAD_REQUEST"].code
    assert "Idempotency-Key" in body["detail"]


async def test_invalid_payload_rejected(admin_client) -> None:
    resp = await admin_client.post(
        "/v1/audit/events",
        json={**PAYLOAD, "action": ""},
        headers={"Idempotency-Key": "k-2", **ADMIN},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == ERROR_SPECS["BAD_REQUEST"].code


async def test_unknown_field_rejected(admin_client) -> None:
    resp = await admin_client.post(
        "/v1/audit/events",
        json={**PAYLOAD, "hacker_field": 1},
        headers={"Idempotency-Key": "k-3", **ADMIN},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == ERROR_SPECS["BAD_REQUEST"].code


async def test_route_not_found_envelope(client) -> None:
    resp = await client.get("/v1/no-such-route")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == ERROR_SPECS["NOT_FOUND"].code
    assert body["request_id"] == resp.headers["X-Request-Id"], "错误信封同样可追踪"


async def test_method_not_allowed_envelope(client) -> None:
    resp = await client.get("/v1/audit/events")
    assert resp.status_code == 405
    assert resp.json()["code"] == ERROR_SPECS["METHOD_NOT_ALLOWED"].code