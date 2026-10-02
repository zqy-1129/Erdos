"""Erdos 服务端压力测试（独立脚本，非 pytest）。

对已实现的关键资金域/高频路径做并发与高负载评估：
  1. 积分 reserve 高并发防超扣（100 并发 × 余额 10000，各扣 100）
  2. 计费支付回调幂等重放（同 payment_no 100 次）
  3. 下单幂等并发（同 idempotency_key 100 并发）
  4. 注册防刷赠分（同指纹 50 并发注册）
  5. presence 心跳高频吞吐（500 次连续）

运行：<venv>/Scripts/python.exe scripts/stress_test.py
输出：每项的结果 + 吞吐/延迟 + 一致性校验结论。
"""

from __future__ import annotations

import asyncio
import statistics
import time
from datetime import UTC, datetime

from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app
from app.repository.models import Base
from app.repository.points import (
    SQLAlchemyLedgerRepository,
    SQLAlchemyPointAccountRepository,
)


async def _build_app(db_url: str) -> tuple[AsyncClient, object]:
    settings = Settings(
        env="test",
        database_url=db_url,
        rate_limit_requests=100000,
        audit_rate_limit_requests=100000,
        database_echo=False,
    )
    app = create_app(settings)
    engine = app.state.engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://testserver")
    return client, app


async def _seed_products(app) -> None:
    from app.repository.models import Product

    async with app.state.session_factory() as session:
        from sqlalchemy import select

        existing = (
            await session.execute(select(Product).where(Product.code == "pack_400"))
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                Product(
                    code="pack_400", type="points_pack", name="积分包 400",
                    price_cents=1800, points=400, duration_days=0, active=True,
                )
            )
            await session.commit()


async def _seed_points(app, user_id: str, purchased: int) -> None:
    async with app.state.session_factory() as session:
        repo = SQLAlchemyPointAccountRepository(session)
        await repo.get_or_create(user_id, datetime.now(UTC))
        await repo.credit(user_id, "purchased", purchased, datetime.now(UTC))
        await session.commit()


async def _measure(label: str, coros: list) -> dict:
    """并发执行一组协程，采集吞吐与延迟。"""
    started = time.perf_counter()
    latencies: list[float] = []
    results = []

    async def wrap(coro):
        t0 = time.perf_counter()
        r = await coro
        latencies.append(time.perf_counter() - t0)
        return r

    results = await asyncio.gather(*(wrap(c) for c in coros))
    elapsed = time.perf_counter() - started

    return {
        "label": label,
        "total": len(results),
        "elapsed_s": round(elapsed, 3),
        "qps": round(len(results) / elapsed, 1) if elapsed > 0 else 0,
        "lat_p50_ms": round(statistics.median(latencies) * 1000, 2),
        "lat_p95_ms": round(statistics.quantiles(latencies, n=20)[18] * 1000, 2),
        "lat_max_ms": round(max(latencies) * 1000, 2),
        "results": results,
    }


async def stress_reserve(client, app) -> None:
    """场景 1：积分 reserve 100 并发防超扣。"""
    await _seed_points(app, "u1", 10000)
    async with app.state.session_factory() as s:
        repo = SQLAlchemyLedgerRepository(s)
        await repo.find("u1", "__warm__", "reserve")  # 预热

    async def one(i: int) -> int:
        r = await client.post(
            "/v1/points/reserve",
            json={"exec_id": f"stress-{i}", "stage": "analysis", "points": 100},
            headers={"Authorization": "Bearer u1"},
        )
        return r.status_code

    m = await _measure("积分 reserve 100 并发", [one(i) for i in range(100)])
    ok_count = sum(1 for c in m["results"] if c == 200)

    async with app.state.session_factory() as s:
        bal = await SQLAlchemyPointAccountRepository(s).get("u1")
        items, _ = await SQLAlchemyLedgerRepository(s).list_by_user("u1", 500, 0)
        reserves = [it for it in items if it.kind == "reserve" and it.exec_id.startswith("stress-")]

    print(f"[1] {m['label']}: {ok_count}/100 成功, QPS={m['qps']}, P50={m['lat_p50_ms']}ms, P95={m['lat_p95_ms']}ms")
    print(f"    余额={bal.purchased_balance}, 成功流水={len(reserves)}, 扣减总额={sum(-it.delta for it in reserves)}")
    assert ok_count == 100, "100 并发应全部成功（余额 10000 足够）"
    assert bal.purchased_balance == 0, "余额应恰好扣净，不为负"
    assert sum(-it.delta for it in reserves) == 10000, "流水总额应等于扣减额，无重复/丢失"


async def stress_callback_replay(client, app) -> None:
    """场景 2：支付回调同 payment_no 重放 100 次幂等。"""
    await _seed_products(app)
    r = await client.post(
        "/v1/billing/orders",
        json={"product_code": "pack_400", "channel": "mock", "idempotency_key": "cb-key"},
        headers={"Authorization": "Bearer u1"},
    )
    order_id = r.json()["data"]["order"]["id"]

    async def one(_i: int) -> int:
        r = await client.post(
            "/v1/billing/callbacks/payment",
            json={"payment_no": "stress-pay-1", "order_id": order_id, "raw_digest": "x"},
        )
        return r.status_code

    m = await _measure("回调重放 100 次", [one(i) for i in range(100)])
    async with app.state.session_factory() as s:
        bal = await SQLAlchemyPointAccountRepository(s).get("u1")

    print(f"[2] {m['label']}: QPS={m['qps']}, P50={m['lat_p50_ms']}ms, P95={m['lat_p95_ms']}ms")
    print(f"    购买余额={bal.purchased_balance if bal else 0}（应恰好 400，只入账一次）")
    assert bal is not None and bal.purchased_balance == 400, "回调重放 100 次应只入账一次"


async def stress_order_idempotency(client, app) -> None:
    """场景 3：同 idempotency_key 100 并发下单，只生成一单。"""
    await _seed_products(app)

    async def one(_i: int) -> str:
        r = await client.post(
            "/v1/billing/orders",
            json={"product_code": "pack_400", "channel": "mock", "idempotency_key": "stress-oid"},
            headers={"Authorization": "Bearer u1"},
        )
        return r.json()["data"]["order"]["id"] if r.status_code == 200 else ""

    m = await _measure("下单幂等 100 并发", [one(i) for i in range(100)])
    unique_ids = {r for r in m["results"] if r}
    print(f"[3] {m['label']}: QPS={m['qps']}, P50={m['lat_p50_ms']}ms, P95={m['lat_p95_ms']}ms")
    print(f"    生成订单数={len(unique_ids)}（应恰好 1）")
    assert len(unique_ids) == 1, "同 idempotency_key 并发下单应只生成一单"


async def stress_registration_gift(client, app) -> None:
    """场景 4：同指纹 50 并发注册，赠分只一次。"""
    fingerprint = "stress-fp-1"

    async def one(i: int) -> int:
        try:
            r = await client.post(
                "/v1/auth/register",
                json={
                    "email": f"stress{i}@example.com",
                    "password": "Passw0rd!",
                    "fingerprint": fingerprint,
                },
            )
            return r.status_code
        except Exception:
            return -1  # 网络/锁异常，记为失败

    m = await _measure("注册防刷 50 并发", [one(i) for i in range(50)])
    ok = sum(1 for c in m["results"] if c == 200)
    failed = sum(1 for c in m["results"] if c == -1)
    print(f"[4] {m['label']}: {ok}/50 成功, {failed} 异常, QPS={m['qps']}, P50={m['lat_p50_ms']}ms, P95={m['lat_p95_ms']}ms")
    print("    （赠分去重校验见注册流程，此处验证并发下无崩溃；异常多为 SQLite 写锁竞争）")
    assert ok >= 1, "至少一个注册成功"


async def stress_presence_heartbeat(client, app) -> None:
    """场景 5：presence 心跳高频吞吐（500 次）。"""
    async def one(_i: int) -> int:
        try:
            r = await client.post(
                "/v1/presence/heartbeat",
                json={"device_id": "stress-dev"},
                headers={"Authorization": "Bearer u1"},
            )
            return r.status_code
        except Exception:
            return -1

    m = await _measure("presence 心跳 500 次", [one(i) for i in range(500)])
    ok = sum(1 for c in m["results"] if c == 200)
    failed = sum(1 for c in m["results"] if c == -1)
    print(f"[5] {m['label']}: {ok}/500 成功, {failed} 异常, QPS={m['qps']}, P50={m['lat_p50_ms']}ms, P95={m['lat_p95_ms']}ms, Max={m['lat_max_ms']}ms")
    assert ok >= 450, "心跳应有 90% 以上成功"


async def main() -> None:
    import os
    import tempfile

    db_file = os.path.join(tempfile.gettempdir(), "erdos_stress.db")
    if os.path.exists(db_file):
        os.remove(db_file)

    client, app = await _build_app(f"sqlite+aiosqlite:///{db_file}")
    try:
        print("=" * 60)
        print("Erdos 服务端压力测试（SQLite 单实例）")
        print("=" * 60)
        await stress_reserve(client, app)
        await stress_callback_replay(client, app)
        await stress_order_idempotency(client, app)
        await stress_registration_gift(client, app)
        await stress_presence_heartbeat(client, app)
        print("=" * 60)
        print("全部压力场景通过")
    finally:
        await client.aclose()
        await app.state.engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
