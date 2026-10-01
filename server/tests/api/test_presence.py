"""心跳接口集成测试（看板 FR-1）。"""

import asyncio

from sqlalchemy import func, select

from app.core.errors import ERROR_SPECS
from app.repository.models import PresenceSession


async def test_heartbeat_requires_token(client) -> None:
    resp = await client.post("/v1/presence/heartbeat", json={"device_id": "d1"})
    assert resp.status_code == 401
    assert resp.json()["code"] == ERROR_SPECS["UNAUTHENTICATED"].code


async def test_heartbeat_success_and_idempotent(client, app) -> None:
    headers = {"Authorization": "Bearer user-1"}
    for _ in range(2):
        resp = await client.post(
            "/v1/presence/heartbeat", json={"device_id": "d1"}, headers=headers
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        assert body["data"]["online"] is True
        assert body["data"]["server_time"]

    # 同 user+device 重复心跳只保留一行会话
    factory = app.state.session_factory
    async with factory() as session:
        total = int(
            (await session.execute(select(func.count()).select_from(PresenceSession))).scalar_one()
        )
        row = (
            await session.execute(
                select(PresenceSession).where(PresenceSession.user_id == "user-1")
            )
        ).scalar_one()
    assert total == 1
    assert row.device_id == "d1"
    assert row.client_ip is not None


async def test_same_user_multi_device_dedup(client, app) -> None:
    headers = {"Authorization": "Bearer user-1"}
    await client.post("/v1/presence/heartbeat", json={"device_id": "d1"}, headers=headers)
    await client.post("/v1/presence/heartbeat", json={"device_id": "d2"}, headers=headers)
    await client.post("/v1/presence/heartbeat", json={}, headers={"Authorization": "Bearer user-2"})

    factory = app.state.session_factory
    async with factory() as session:
        total = int(
            (await session.execute(select(func.count()).select_from(PresenceSession))).scalar_one()
        )
    assert total == 3, "两台设备+一个无设备用户共 3 个会话"


async def test_invalid_device_id_rejected(client) -> None:
    resp = await client.post(
        "/v1/presence/heartbeat",
        json={"device_id": "x" * 65},
        headers={"Authorization": "Bearer u"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == ERROR_SPECS["BAD_REQUEST"].code


async def test_concurrent_heartbeats_same_key_no_conflict(client, app) -> None:
    """同 user+device 并发心跳：全部成功且仅保留一行（唯一约束竞态回归）。"""
    headers = {"Authorization": "Bearer race-user"}

    async def beat() -> int:
        resp = await client.post("/v1/presence/heartbeat", json={"device_id": "d1"}, headers=headers)
        return resp.status_code

    statuses = await asyncio.gather(*[beat() for _ in range(20)])
    assert all(s == 200 for s in statuses), f"出现非 200：{statuses}"

    factory = app.state.session_factory
    async with factory() as session:
        rows = list(
            (
                await session.execute(
                    select(PresenceSession).where(PresenceSession.user_id == "race-user")
                )
            ).scalars()
        )
    assert len(rows) == 1, "并发心跳不应产生重复会话行"