"""网关中间件集成测试（request_id / 指标 / 限流 / 鉴权透传）。"""

from fastapi import Request

from app.core.config import Settings
from app.core.errors import ERROR_SPECS
from app.infra.metrics import REQUEST_TOTAL
from app.main import create_app

AUDIT_PAYLOAD = {
    "actor_type": "user",
    "actor_id": "u1",
    "action": "account.login",
    "resource_type": "account",
    "resource_id": "r1",
}


def _sample_value(labels: dict[str, str]) -> float | None:
    for metric in REQUEST_TOTAL.collect():
        for sample in metric.samples:
            if sample.labels == labels:
                return sample.value
    return None


async def test_request_id_echo_valid(client) -> None:
    resp = await client.get("/v1/health", headers={"X-Request-Id": "trace-abc-123"})
    assert resp.status_code == 200
    assert resp.headers["X-Request-Id"] == "trace-abc-123"
    assert resp.json()["request_id"] == "trace-abc-123"


async def test_request_id_generated_when_absent_or_invalid(client) -> None:
    resp = await client.get("/v1/health")
    rid = resp.headers["X-Request-Id"]
    assert len(rid) == 32 and rid.isalnum(), "应为生成的 uuid hex"

    resp2 = await client.get("/v1/health", headers={"X-Request-Id": "bad id!"})
    assert resp2.headers["X-Request-Id"] != "bad id!", "非法格式应重新生成"


async def test_metrics_instrumented(client) -> None:
    # path 标签取 Starlette 路由模板（不含 /v1 聚合前缀）
    get_labels = {"method": "GET", "path": "/health", "status": "200"}
    post_labels = {"method": "POST", "path": "/audit/events", "status": "200"}
    get_before = _sample_value(get_labels) or 0.0
    post_before = _sample_value(post_labels) or 0.0

    await client.get("/v1/health")
    await client.post("/v1/audit/events", json=AUDIT_PAYLOAD, headers={"Idempotency-Key": "m1"})
    assert (_sample_value(get_labels) or 0.0) == get_before + 1.0
    assert (_sample_value(post_labels) or 0.0) == post_before + 1.0

    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "erdos_http_requests_total" in resp.text


async def test_auth_passthrough_and_probe(client, app) -> None:
    @app.get("/v1/_probe")
    async def probe(request: Request) -> dict:
        principal = getattr(request.state, "principal", None)
        return {
            "subject": principal.subject if principal else None,
            "verified": principal.verified if principal else None,
        }

    resp = await client.get("/v1/_probe", headers={"Authorization": "Bearer tok-1"})
    body = resp.json()
    assert body["subject"] == "tok-1"  # DevTokenIntrospector 透传原始令牌
    assert body["verified"] is False

    resp2 = await client.get("/v1/_probe")
    assert resp2.json()["subject"] is None


async def test_auth_enforce_rejects_missing_token(tmp_path) -> None:
    settings = Settings(
        env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}",
        auth_enforce=True,
    )
    from app.repository.models import Base

    application = create_app(settings)
    async with application.state.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as c:
        resp = await c.get("/v1/health")
        assert resp.status_code == 401
        assert resp.json()["code"] == ERROR_SPECS["UNAUTHENTICATED"].code

        resp2 = await c.get("/v1/health", headers={"Authorization": "Bearer tok"})
        assert resp2.status_code == 200
    await application.state.engine.dispose()


async def test_rate_limit_returns_429_envelope(tmp_path) -> None:
    settings = Settings(
        env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}",
        audit_rate_limit_requests=2,
        rate_limit_requests=1000,
    )
    from app.repository.models import Base

    application = create_app(settings)
    async with application.state.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as c:
        for i in range(2):
            resp = await c.post(
                "/v1/audit/events",
                json=AUDIT_PAYLOAD,
                headers={"Idempotency-Key": f"rl-{i}"},
            )
            assert resp.status_code == 200
        resp = await c.post(
            "/v1/audit/events", json=AUDIT_PAYLOAD, headers={"Idempotency-Key": "rl-3"}
        )
        assert resp.status_code == 429
        body = resp.json()
        assert body["code"] == ERROR_SPECS["RATE_LIMITED"].code
        assert int(resp.headers["Retry-After"]) >= 1
        # 限流跳过路径不受影响
        assert (await c.get("/metrics")).status_code == 200
    await application.state.engine.dispose()