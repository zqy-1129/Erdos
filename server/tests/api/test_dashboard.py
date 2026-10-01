"""看板管理端接口集成测试（FR-2/FR-5/FR-6）。"""

import asyncio
import contextlib
import datetime as dt
import json
from datetime import datetime
from urllib.parse import urlsplit

from httpx import ASGITransport, AsyncClient

from app.core.errors import ERROR_SPECS
from app.main import create_app
from app.repository.base import SQLAlchemyRepository
from app.repository.models import Base, UsersDailyStats
from app.repository.uow import UnitOfWork
from conftest import StaticIntrospector

ADMIN = {"Authorization": "Bearer admin-token"}


async def _collect_sse(
    app, url: str, headers: dict, count: int, timeout: float = 10.0
) -> tuple[list[int], list[str], list[dict]]:
    """极简 ASGI 直驱采集 SSE 事件（httpx ASGITransport 对流式响应存在兼容问题）。

    返回 (ids, topics, payloads) 三元组，按接收顺序对齐。
    """
    parts = urlsplit(url)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": parts.path,
        "raw_path": parts.path.encode(),
        "query_string": parts.query.encode(),
        "root_path": "",
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
    }
    requested = False
    disconnect = asyncio.Event()

    async def receive() -> dict:
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    ids: list[int] = []
    topics: list[str] = []
    payloads: list[dict] = []
    got_enough = asyncio.Event()

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            assert message["status"] == 200, f"SSE 未授权或异常：{message['status']}"
        elif message["type"] == "http.response.body":
            text = message.get("body", b"").decode("utf-8")
            for line in text.splitlines():
                if line.startswith("id: "):
                    ids.append(int(line[4:]))
                elif line.startswith("event: "):
                    topics.append(line[7:])
                elif line.startswith("data: "):
                    payloads.append(json.loads(line[6:]))
            if len(ids) >= count:
                got_enough.set()

    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(got_enough.wait(), timeout)
    finally:
        disconnect.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return ids, topics[: len(ids)], payloads


async def _heartbeat(client, user: str) -> None:
    resp = await client.post(
        "/v1/presence/heartbeat",
        json={"device_id": "d1"},
        headers={"Authorization": f"Bearer {user}"},
    )
    assert resp.status_code == 200


async def test_online_endpoint_reflects_heartbeats(admin_client) -> None:
    await _heartbeat(admin_client, "u1")
    await _heartbeat(admin_client, "u2")
    await _heartbeat(admin_client, "u1")  # 重复心跳不重复计数

    resp = await admin_client.get("/v1/admin/dashboard/users/online", headers=ADMIN)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["current_online"] == 2
    assert data["window_seconds"] == 90.0
    assert data["server_time"]


async def test_users_total_returns_series(admin_client, admin_app) -> None:
    factory = admin_app.state.session_factory
    async with UnitOfWork(factory) as uow:
        await SQLAlchemyRepository(uow.session, UsersDailyStats).add(
            UsersDailyStats(
                stat_date=dt.date(2026, 9, 30), total_users=100, new_users=5, active_users=40
            )
        )

    resp = await admin_client.get("/v1/admin/dashboard/users/total", headers=ADMIN)
    assert resp.status_code == 200
    series = resp.json()["data"]["series"]
    assert len(series) == 1
    assert series[0]["total_users"] == 100


async def test_admin_endpoints_reject_unauthorized(admin_client) -> None:
    # 无凭证 -> 40101
    resp = await admin_client.get("/v1/admin/dashboard/users/online")
    assert resp.status_code == 401
    assert resp.json()["code"] == ERROR_SPECS["UNAUTHENTICATED"].code


async def test_admin_endpoints_reject_non_admin_role(settings) -> None:
    application = create_app(settings, introspector=StaticIntrospector(("user",)))
    async with application.state.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as c:
        resp = await c.get(
            "/v1/admin/dashboard/users/online", headers={"Authorization": "Bearer u"}
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == ERROR_SPECS["PERMISSION_DENIED"].code
    await application.state.engine.dispose()


async def test_stream_backlog_catch_up(admin_client, admin_app) -> None:
    """多主题（聚合+事件）游标追赶：since=0 补发积压；游标重连只收增量。"""
    for i in range(3):
        await _heartbeat(admin_client, f"u{i}")  # 每个新用户产生 online_count + user.online 两事件

    ids, topics, payloads = await _collect_sse(
        admin_app, "/v1/admin/dashboard/stream?since=0", ADMIN, count=6
    )
    # 顺序确定性：publish 按 online_count -> user.online 排列
    assert ids == [1, 2, 3, 4, 5, 6], "since=0 应补发缓冲内全部事件"
    assert topics == [
        "presence.online_count",
        "dashboard.events",
    ] * 3, "聚合与事件主题交替投递"
    assert all("current_online" in p for p in payloads[0::2])

    # 游标重连：Last-Event-ID=6 后新心跳产生 7/8 号事件，只收增量
    await _heartbeat(admin_client, "u9")
    new_ids, new_topics, _ = await _collect_sse(
        admin_app,
        "/v1/admin/dashboard/stream",
        {**ADMIN, "Last-Event-ID": "6"},
        count=2,
    )
    assert new_ids == [7, 8], "游标重连只补发增量，不重放旧事件"


async def test_stream_requires_admin_role(admin_client) -> None:
    resp = await admin_client.get("/v1/admin/dashboard/stream")
    assert resp.status_code == 401


async def test_dashboard_page_served(client) -> None:
    """管理看板静态页可访问（自包含单页，无需前端构建）。"""
    resp = await client.get("/admin", follow_redirects=True)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Erdos 用户数据看板" in resp.text


async def test_dashboard_index_redirect(client) -> None:
    """/admin 重定向到 /admin/ 索引页。"""
    resp = await client.get("/admin")
    assert resp.status_code == 307
    assert resp.headers["location"].endswith("/admin/")


async def _seed_minute_rows(admin_app, rows: list[tuple[datetime, int]]) -> None:
    from app.repository.models import PresenceMinuteAgg

    factory = admin_app.state.session_factory
    async with UnitOfWork(factory) as uow:
        for ts, count in rows:
            uow.session.add(PresenceMinuteAgg(minute_ts=ts, online_count=count))


async def _seed_daily_rows(admin_app, rows: list[tuple[dt.date, int, int, int]]) -> None:
    factory = admin_app.state.session_factory
    async with UnitOfWork(factory) as uow:
        for d, total, new, active in rows:
            uow.session.add(
                UsersDailyStats(stat_date=d, total_users=total, new_users=new, active_users=active)
            )


async def test_online_trend_granularity_and_range(admin_client, admin_app) -> None:
    import datetime as _dt
    from urllib.parse import quote

    base = _dt.datetime(2026, 10, 1, tzinfo=_dt.UTC)
    await _seed_minute_rows(
        admin_app,
        [
            (base + _dt.timedelta(seconds=60), 2),
            (base + _dt.timedelta(seconds=120), 9),
            (base + _dt.timedelta(seconds=600), 5),
        ],
    )
    url = (
        "/v1/admin/dashboard/online/trend?granularity=5m&"
        f"from={quote(base.isoformat())}&to={quote((base + _dt.timedelta(hours=1)).isoformat())}"
    )
    resp = await admin_client.get(url, headers=ADMIN)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["granularity"] == "5m"
    assert [(p["value"]) for p in data["points"]] == [9, 5], "桶内取 max"


async def test_online_trend_invalid_granularity(admin_client) -> None:
    resp = await admin_client.get("/v1/admin/dashboard/online/trend?granularity=2m", headers=ADMIN)
    assert resp.status_code == 400
    assert resp.json()["code"] == ERROR_SPECS["BAD_REQUEST"].code


async def test_online_trend_from_after_to_rejected(admin_client) -> None:
    import datetime as _dt
    from urllib.parse import quote

    now = _dt.datetime.now(_dt.UTC)
    after = quote((now + _dt.timedelta(hours=1)).isoformat())
    before = quote(now.isoformat())
    resp = await admin_client.get(
        f"/v1/admin/dashboard/online/trend?from={after}&to={before}", headers=ADMIN
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == ERROR_SPECS["BAD_REQUEST"].code


async def test_users_trend_range_filter(admin_client, admin_app) -> None:
    await _seed_daily_rows(
        admin_app,
        [
            (dt.date(2026, 9, 28), 90, 3, 30),
            (dt.date(2026, 9, 30), 100, 5, 40),
            (dt.date(2026, 10, 1), 110, 2, 35),
        ],
    )
    resp = await admin_client.get(
        "/v1/admin/dashboard/users/trend?from=2026-09-29&to=2026-10-01", headers=ADMIN
    )
    assert resp.status_code == 200
    series = resp.json()["data"]["series"]
    assert [r["stat_date"] for r in series] == ["2026-09-30", "2026-10-01"]


async def test_events_projection_filter_and_pagination(admin_client, admin_app) -> None:
    await _heartbeat(admin_client, "ev-user-1")
    await _heartbeat(admin_client, "ev-user-1")  # 重复心跳不产生第二个 user.online
    for i in range(2):
        resp = await admin_client.post(
            "/v1/audit/events",
            json={
                "actor_type": "user",
                "actor_id": "u1",
                "action": "account.login",
                "resource_type": "account",
                "resource_id": "r1",
            },
            headers={"Idempotency-Key": f"ev-key-{i}", **ADMIN},
        )
        assert resp.status_code == 200

    # 全量：1 个 user.online + 2 个审计事件
    resp = await admin_client.get("/v1/admin/dashboard/events", headers=ADMIN)
    body = resp.json()["data"]
    assert body["total"] == 3
    assert len(body["items"]) == 3

    # 类型筛选
    resp = await admin_client.get(
        "/v1/admin/dashboard/events?types=audit.account.login", headers=ADMIN
    )
    body = resp.json()["data"]
    assert body["total"] == 2
    assert {e["type"] for e in body["items"]} == {"audit.account.login"}

    # 分页
    resp = await admin_client.get("/v1/admin/dashboard/events?limit=1&offset=1", headers=ADMIN)
    body = resp.json()["data"]
    assert body["total"] == 3 and len(body["items"]) == 1

    # 新心跳幂等：user.online 仅一条
    await _heartbeat(admin_client, "ev-user-1")
    resp = await admin_client.get(
        "/v1/admin/dashboard/events?types=user.online", headers=ADMIN
    )
    body = resp.json()["data"]
    assert body["total"] == 1, "重复心跳不得重复投影上线事件"