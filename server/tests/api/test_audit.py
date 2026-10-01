"""审计事件接口集成测试（幂等/校验/统一信封）。"""

from app.core.errors import ERROR_SPECS

PAYLOAD = {
    "actor_type": "user",
    "actor_id": "u1",
    "action": "account.login",
    "resource_type": "account",
    "resource_id": "r1",
    "detail": {"mfa": False},
    "client_ip": "1.2.3.4",
}


async def test_create_audit_event_success(client) -> None:
    resp = await client.post(
        "/v1/audit/events", json=PAYLOAD, headers={"Idempotency-Key": "k-1"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["id"] == 1
    assert data["action"] == "account.login"
    assert data["client_ip"] in ("1.2.3.4", "127.0.0.1")
    assert data["created_at"]


async def test_idempotent_duplicate_conflict(client) -> None:
    headers = {"Idempotency-Key": "k-dup"}
    assert (await client.post("/v1/audit/events", json=PAYLOAD, headers=headers)).status_code == 200
    resp = await client.post("/v1/audit/events", json=PAYLOAD, headers=headers)
    assert resp.status_code == 409
    assert resp.json()["code"] == ERROR_SPECS["CONFLICT"].code


async def test_missing_idempotency_key_rejected(client) -> None:
    resp = await client.post("/v1/audit/events", json=PAYLOAD)
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == ERROR_SPECS["BAD_REQUEST"].code
    assert "Idempotency-Key" in body["detail"]


async def test_invalid_payload_rejected(client) -> None:
    resp = await client.post(
        "/v1/audit/events",
        json={**PAYLOAD, "action": ""},
        headers={"Idempotency-Key": "k-2"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == ERROR_SPECS["BAD_REQUEST"].code


async def test_unknown_field_rejected(client) -> None:
    resp = await client.post(
        "/v1/audit/events",
        json={**PAYLOAD, "hacker_field": 1},
        headers={"Idempotency-Key": "k-3"},
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