"""Erdos 服务端压力测试（独立脚本，非 pytest）。

场景（SP2-8 口径，2026-10-03 修订）：
  判定集（生产形状写密集，PG 模式 P95 < 200ms Go 门槛）：
    1b 积分 reserve 多用户 100 并发
    2  支付回调幂等重放 100
    3  下单幂等 100 并发
  参考集（一致性/吞吐，不参与延迟门槛）：
    1a 积分 reserve 同账户 100 并发（单行锁漏斗上界；防超扣/幂等断言）
    4  注册防刷 50 并发（bcrypt cost=12 CPU 红线）
    5  presence 心跳 500 次（分钟桶全局单行漏斗）

运行模式：
  默认（ASGI 直连）：<venv>/Scripts/python.exe scripts/stress_test.py
  真实 HTTP（推荐验收口径）：ERDOS_STRESS_REAL_HTTP=1 时脚本自行拉起
  uvicorn 子进程（127.0.0.1 随机端口）并走 TCP 客户端——规避 ASGITransport
  同事件循环「百协程同时 checkout」的伪竞争（2026-10-03 定位，见
  reports/SP2-8验收预案 口径修订与根因记录）。
连接串：ERDOS_DATABASE_URL（默认 SQLite 临时库）。池参数 ERDOS_DB_POOL_SIZE /
  ERDOS_DB_POOL_MAX_OVERFLOW 可覆盖（PG 验收建议 106/0：池常驻不收缩，规避
  Windows asyncpg 建连 ~15ms 串行 + overflow 场景间回收重建）。
"""

from __future__ import annotations

import asyncio
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.domain.billing.service import callback_digest
from app.infra.db import create_engine, create_session_factory
from app.main import create_app
from app.repository.models import Base, PointAccount, PointLedger, Product

# 回调 HMAC 验签共享密钥（压测专用；与生产 ERDOS_PAYMENT_CALLBACK_SECRET 无关）
STRESS_CALLBACK_SECRET = "stress-test-callback-secret"

# P95 采集双口径（SP2-8）：perf = 生产形状写密集（参与 P95<200ms 判定）；
# ref = 一致性/吞吐参考（单行漏斗、bcrypt CPU 红线等，不参与延迟门槛）
_PERF_P95: list[tuple[str, float]] = []
_REF_P95: list[tuple[str, float]] = []


class Ctx:
    """压测上下文：被测 HTTP 客户端 + 种子/校验用独立引擎。"""

    def __init__(self, client: httpx.AsyncClient, seed_engine: AsyncEngine) -> None:
        self.client = client
        self.seed_engine = seed_engine
        self.proc: subprocess.Popen[bytes] | None = None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _wait_ready(client: httpx.AsyncClient, tries: int = 60) -> None:
    for _ in range(tries):
        try:
            r = await client.get("/v1/health")
            if r.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.5)
    raise RuntimeError("uvicorn 服务未在预期时间内就绪")


async def _build_context(db_url: str) -> Ctx:
    """按 ERDOS_STRESS_REAL_HTTP 选择被测形态；种子引擎两形态均独立于被测服务。"""
    seed_engine = create_engine(db_url)
    real_http = os.environ.get("ERDOS_STRESS_REAL_HTTP") == "1"
    if real_http:
        port = _free_port()
        env = dict(os.environ)
        env["ERDOS_DATABASE_URL"] = db_url
        env["ERDOS_PAYMENT_CALLBACK_SECRET"] = STRESS_CALLBACK_SECRET
        env["ERDOS_AUTH_ENFORCE"] = "false"
        env["ERDOS_RATE_LIMIT_REQUESTS"] = "100000"
        env["ERDOS_AUDIT_RATE_LIMIT_REQUESTS"] = "100000"
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
            ],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=60.0)
        await _wait_ready(client)
        ctx = Ctx(client, seed_engine)
        ctx.proc = proc
        return ctx

    settings = Settings(
        env="test",
        database_url=db_url,
        rate_limit_requests=100000,
        audit_rate_limit_requests=100000,
        database_echo=False,
        payment_callback_secret=STRESS_CALLBACK_SECRET,
    )
    app = create_app(settings)
    app_engine = app.state.engine
    async with app_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    transport = httpx.ASGITransport(app=app)
    ctx = Ctx(httpx.AsyncClient(transport=transport, base_url="http://testserver"), seed_engine)
    return ctx


# ----------------------------------------------------------------------
# 种子与校验（独立引擎，绕过被测服务，避免污染延迟测量）
# ----------------------------------------------------------------------
async def _seed_products(ctx: Ctx) -> None:
    factory = create_session_factory(ctx.seed_engine)
    async with factory() as session:
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


async def _seed_points(ctx: Ctx, user_id: str, purchased: int) -> None:
    factory = create_session_factory(ctx.seed_engine)
    async with factory() as session:
        row = (
            await session.execute(select(PointAccount).where(PointAccount.user_id == user_id))
        ).scalar_one_or_none()
        if row is None:
            session.add(
                PointAccount(
                    user_id=user_id, purchased_balance=purchased, monthly_balance=0,
                    frozen=False, version=0,
                )
            )
        else:
            row.purchased_balance = purchased
        await session.commit()


async def _balance(ctx: Ctx, user_id: str) -> PointAccount | None:
    factory = create_session_factory(ctx.seed_engine)
    async with factory() as session:
        return (
            await session.execute(select(PointAccount).where(PointAccount.user_id == user_id))
        ).scalar_one_or_none()


async def _ledgers(ctx: Ctx, user_id: str, prefix: str) -> list[PointLedger]:
    factory = create_session_factory(ctx.seed_engine)
    async with factory() as session:
        rows = (
            await session.execute(
                select(PointLedger).where(
                    PointLedger.user_id == user_id,
                    PointLedger.exec_id.startswith(prefix),
                )
            )
        ).scalars().all()
        return list(rows)


# ----------------------------------------------------------------------
# 测量
# ----------------------------------------------------------------------
async def _measure(label: str, coros: list, *, perf: bool = True) -> dict:
    """并发执行一组协程，采集吞吐与延迟。

    perf=True 的场景计入 SP2-8 P95<200ms 判定集（生产形状写密集）；
    perf=False 的场景（单行锁漏斗/CPU 红线）仅作一致性与吞吐参考。
    """
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
    p95_ms = round(statistics.quantiles(latencies, n=20)[18] * 1000, 2)

    result = {
        "label": label,
        "total": len(results),
        "elapsed_s": round(elapsed, 3),
        "qps": round(len(results) / elapsed, 1) if elapsed > 0 else 0,
        "lat_p50_ms": round(statistics.median(latencies) * 1000, 2),
        "lat_p95_ms": p95_ms,
        "lat_max_ms": round(max(latencies) * 1000, 2),
        "results": results,
    }
    (_PERF_P95 if perf else _REF_P95).append((label, p95_ms))
    return result


# ----------------------------------------------------------------------
# 场景
# ----------------------------------------------------------------------
async def _warmup_pool(ctx: Ctx) -> None:
    """预热连接池（稳态口径）：生产连接池常驻，建连只发生在启动/扩容瞬间。

    asyncpg 在 Windows 上建连 ~15ms/个且串行化（实测 100 连接 1.5s）；
    若不预热且池会收缩，每场景首波请求的 P95 被建连成本污染。
    """
    async def one(_i: int) -> int:
        r = await ctx.client.get("/v1/health")
        return r.status_code

    m = await _measure("连接池预热（不计判定）", [one(i) for i in range(100)], perf=False)
    assert all(c == 200 for c in m["results"]), "预热请求应全部成功"
    print(f"[0] {m['label']}: 100/100, {m['elapsed_s']}s")


async def stress_reserve(ctx: Ctx) -> None:
    """场景 1a：同账户 100 并发 reserve（单行锁漏斗上界，参考口径）。

    一致性断言（防超扣/幂等/流水总额）必须 100%。
    """
    await _seed_points(ctx, "u1", 10000)

    async def one(i: int) -> int:
        r = await ctx.client.post(
            "/v1/points/reserve",
            json={"exec_id": f"stress-{i}", "stage": "analysis", "points": 100},
            headers={"Authorization": "Bearer u1"},
        )
        return r.status_code

    m = await _measure("积分 reserve 同账户 100 并发（一致性漏斗）", [one(i) for i in range(100)], perf=False)
    ok_count = sum(1 for c in m["results"] if c == 200)

    bal = await _balance(ctx, "u1")
    reserves = await _ledgers(ctx, "u1", "stress-")

    print(f"[1a] {m['label']}: {ok_count}/100 成功, QPS={m['qps']}, P50={m['lat_p50_ms']}ms, P95={m['lat_p95_ms']}ms")
    print(f"    余额={bal.purchased_balance if bal else 'NA'}, 成功流水={len(reserves)}, 扣减总额={sum(-it.delta for it in reserves)}")
    assert ok_count == 100, "100 并发应全部成功（余额 10000 足够）"
    assert bal is not None and bal.purchased_balance == 0, "余额应恰好扣净，不为负"
    assert sum(-it.delta for it in reserves) == 10000, "流水总额应等于扣减额，无重复/丢失"


async def stress_reserve_multi_user(ctx: Ctx) -> None:
    """场景 1b：多用户并发 reserve（生产形状写密集，参与 P95<200ms 判定）。"""
    for i in range(100):
        await _seed_points(ctx, f"mu-{i}", 100)

    async def one(i: int) -> int:
        r = await ctx.client.post(
            "/v1/points/reserve",
            json={"exec_id": f"stress-mu-{i}", "stage": "analysis", "points": 100},
            headers={"Authorization": f"Bearer mu-{i}"},
        )
        return r.status_code

    m = await _measure("积分 reserve 多用户 100 并发", [one(i) for i in range(100)])
    from collections import Counter

    dist = dict(Counter(m["results"]))
    ok_count = sum(1 for c in m["results"] if c == 200)
    print(f"[1b] {m['label']}: {ok_count}/100 成功, 状态分布={dist}, QPS={m['qps']}, P50={m['lat_p50_ms']}ms, P95={m['lat_p95_ms']}ms")
    assert ok_count == 100, "多用户并发 reserve 应全部成功"


async def stress_callback_replay(ctx: Ctx) -> None:
    """场景 2：支付回调同 payment_no 重放 100 次幂等（判定集）。"""
    await _seed_products(ctx)
    r = await ctx.client.post(
        "/v1/billing/orders",
        json={"product_code": "pack_400", "channel": "mock", "idempotency_key": "cb-key"},
        headers={"Authorization": "Bearer u1"},
    )
    order_id = r.json()["data"]["order"]["id"]

    async def one(_i: int) -> int:
        r = await ctx.client.post(
            "/v1/billing/callbacks/payment",
            json={
                "payment_no": "stress-pay-1",
                "order_id": order_id,
                "raw_digest": callback_digest(STRESS_CALLBACK_SECRET, "stress-pay-1", order_id),
            },
        )
        return r.status_code

    m = await _measure("回调重放 100 次", [one(i) for i in range(100)])
    bal = await _balance(ctx, "u1")

    print(f"[2] {m['label']}: QPS={m['qps']}, P50={m['lat_p50_ms']}ms, P95={m['lat_p95_ms']}ms")
    print(f"    购买余额={bal.purchased_balance if bal else 'NA'}（应恰好 400，只入账一次）")
    assert bal is not None and bal.purchased_balance == 400, "回调重放 100 次应只入账一次"


async def stress_order_idempotency(ctx: Ctx) -> None:
    """场景 3：同 idempotency_key 100 并发下单，只生成一单（判定集）。"""
    await _seed_products(ctx)

    async def one(_i: int) -> str:
        r = await ctx.client.post(
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


async def stress_registration_gift(ctx: Ctx) -> None:
    """场景 4：同指纹 50 并发注册（bcrypt-12 CPU 红线，参考口径）。"""
    fingerprint = "stress-fp-1"

    async def one(i: int) -> int:
        try:
            r = await ctx.client.post(
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

    m = await _measure("注册防刷 50 并发（bcrypt CPU 红线）", [one(i) for i in range(50)], perf=False)
    ok = sum(1 for c in m["results"] if c == 200)
    failed = sum(1 for c in m["results"] if c == -1)
    print(f"[4] {m['label']}: {ok}/50 成功, {failed} 异常, QPS={m['qps']}, P50={m['lat_p50_ms']}ms, P95={m['lat_p95_ms']}ms")
    assert ok >= 1, "至少一个注册成功"


async def stress_presence_heartbeat(ctx: Ctx) -> None:
    """场景 5：presence 心跳高频吞吐（500 次，分钟桶全局单行漏斗，参考口径）。"""
    async def one(_i: int) -> int:
        try:
            r = await ctx.client.post(
                "/v1/presence/heartbeat",
                json={"device_id": "stress-dev"},
                headers={"Authorization": "Bearer u1"},
            )
            return r.status_code
        except Exception:
            return -1

    m = await _measure("presence 心跳 500 次（分钟桶漏斗）", [one(i) for i in range(500)], perf=False)
    ok = sum(1 for c in m["results"] if c == 200)
    failed = sum(1 for c in m["results"] if c == -1)
    print(f"[5] {m['label']}: {ok}/500 成功, {failed} 异常, QPS={m['qps']}, P50={m['lat_p50_ms']}ms, P95={m['lat_p95_ms']}ms, Max={m['lat_max_ms']}ms")
    assert ok >= 450, "心跳应有 90% 以上成功"


async def main() -> None:
    db_url = os.environ.get("ERDOS_DATABASE_URL")
    driver = "PostgreSQL" if db_url else "SQLite"
    real_http = os.environ.get("ERDOS_STRESS_REAL_HTTP") == "1"
    if not db_url:
        db_file = os.path.join(tempfile.gettempdir(), "erdos_stress.db")
        if os.path.exists(db_file):
            os.remove(db_file)
        db_url = f"sqlite+aiosqlite:///{db_file}"

    ctx = await _build_context(db_url)
    try:
        print("=" * 60)
        print(f"Erdos 服务端压力测试（{driver} 单实例，{'真实 HTTP' if real_http else 'ASGI 直连'}）")
        print("=" * 60)
        await _warmup_pool(ctx)
        await stress_reserve(ctx)
        await stress_reserve_multi_user(ctx)
        await stress_callback_replay(ctx)
        await stress_order_idempotency(ctx)
        await stress_registration_gift(ctx)
        await stress_presence_heartbeat(ctx)
        print("=" * 60)
        print("全部压力场景通过")

        # PG 模式按验收口径判定（SP2-8：写密集 P95 < 200ms = Go，否则 No-Go）
        if db_url.startswith("postgresql"):
            threshold = float(os.environ.get("ERDOS_PERF_P95_MS", "200"))
            worst = max((p95 for _, p95 in _PERF_P95), default=0.0)
            print(f"判定集（生产形状写密集）：" +
                  "；".join(f"{label}={p95}ms" for label, p95 in _PERF_P95))
            print(f"参考集（漏斗/CPU 红线，不参与门槛）：" +
                  "；".join(f"{label}={p95}ms" for label, p95 in _REF_P95))
            failed = [f"{label}={p95}ms" for label, p95 in _PERF_P95 if p95 > threshold]
            if failed:
                raise SystemExit(
                    f"SP2-8 性能验收 No-Go：以下写密集场景 P95 超 {threshold}ms —— {'；'.join(failed)}"
                )
            print(f"SP2-8 性能验收 Go：判定集全部 P95 ≤ {threshold}ms（最差 {worst}ms）")
    finally:
        await ctx.client.aclose()
        await ctx.seed_engine.dispose()
        if ctx.proc is not None:
            ctx.proc.terminate()
            try:
                ctx.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                ctx.proc.kill()


if __name__ == "__main__":
    asyncio.run(main())