"""计费订阅域接口集成测试（SP2-5）：商品 / 下单 / 回调 / 退款 / 订阅 / 月赠。

test 环境用 DevTokenIntrospector：Bearer <subject> 透传为请求主体。
回调端点 HMAC 验签（conftest settings.payment_callback_secret="test-callback-secret"）。
"""

from datetime import UTC, datetime

import httpx

from app.core.config import Settings
from app.domain.billing.service import callback_digest
from app.main import create_app
from app.repository.models import Base, Product
from app.repository.uow import UnitOfWork

CALLBACK_SECRET = "test-callback-secret"


def _digest(payment_no: str, order_id: str) -> str:
    return callback_digest(CALLBACK_SECRET, payment_no, order_id)


async def _seed_products(client) -> None:
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    async with UnitOfWork(factory) as uow:
        uow.session.add(
            Product(
                code="sub_monthly",
                type="subscription",
                name="月订阅",
                price_cents=1900,
                points=400,
                duration_days=30,
                active=True,
            )
        )
        uow.session.add(
            Product(
                code="pack_400",
                type="points_pack",
                name="积分包 400",
                price_cents=1800,
                points=400,
                duration_days=0,
                active=True,
            )
        )
        await uow.session.flush()


async def test_products_listing(client) -> None:
    await _seed_products(client)
    resp = await client.get("/v1/billing/products")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 2
    codes = {p["code"] for p in data}
    assert "sub_monthly" in codes
    assert "pack_400" in codes


async def test_create_order_and_callback_flow(client) -> None:
    await _seed_products(client)
    # 下单
    r = await client.post(
        "/v1/billing/orders",
        json={"product_code": "pack_400", "channel": "mock", "idempotency_key": "key-1"},
        headers={"Authorization": "Bearer u1"},
    )
    assert r.status_code == 200
    order = r.json()["data"]["order"]
    assert order["status"] == "created"
    order_id = order["id"]

    # 回调
    c = await client.post(
        "/v1/billing/callbacks/payment",
        json={"payment_no": "pay-1", "order_id": order_id, "raw_digest": _digest("pay-1", order_id)},
    )
    assert c.status_code == 200
    assert c.json()["data"]["status"] == "paid"

    # 订单查询（补账兜底）
    g = await client.get(
        f"/v1/billing/orders/{order_id}", headers={"Authorization": "Bearer u1"}
    )
    assert g.status_code == 200
    assert g.json()["data"]["status"] == "paid"

    # 积分余额已入账
    b = await client.get("/v1/points/balance", headers={"Authorization": "Bearer u1"})
    assert b.json()["data"]["purchased_balance"] == 400


async def test_callback_idempotent(client) -> None:
    await _seed_products(client)
    r = await client.post(
        "/v1/billing/orders",
        json={"product_code": "pack_400", "channel": "mock", "idempotency_key": "key-1"},
        headers={"Authorization": "Bearer u1"},
    )
    order_id = r.json()["data"]["order"]["id"]
    for _ in range(5):
        await client.post(
            "/v1/billing/callbacks/payment",
            json={"payment_no": "pay-1", "order_id": order_id, "raw_digest": _digest("pay-1", order_id)},
        )
    b = await client.get("/v1/points/balance", headers={"Authorization": "Bearer u1"})
    assert b.json()["data"]["purchased_balance"] == 400  # 只入账一次


async def test_subscription_and_monthly_grant(client) -> None:
    await _seed_products(client)
    # 订阅下单 + 回调
    r = await client.post(
        "/v1/billing/orders",
        json={"product_code": "sub_monthly", "channel": "mock", "idempotency_key": "key-sub"},
        headers={"Authorization": "Bearer u1"},
    )
    order_id = r.json()["data"]["order"]["id"]
    await client.post(
        "/v1/billing/callbacks/payment",
        json={"payment_no": "pay-sub", "order_id": order_id, "raw_digest": _digest("pay-sub", order_id)},
    )
    # 订阅查询
    s = await client.get(
        "/v1/billing/subscription", headers={"Authorization": "Bearer u1"}
    )
    assert s.status_code == 200
    assert s.json()["data"]["status"] == "active"

    # 月赠触发
    m = await client.post(
        "/v1/billing/subscription/monthly-grant", headers={"Authorization": "Bearer u1"}
    )
    assert m.status_code == 200
    assert m.json()["data"]["granted_points"] == 400
    # 余额：月度 400
    b = await client.get("/v1/points/balance", headers={"Authorization": "Bearer u1"})
    assert b.json()["data"]["monthly_balance"] == 400


async def test_refund_points_pack_rejected(client) -> None:
    await _seed_products(client)
    r = await client.post(
        "/v1/billing/orders",
        json={"product_code": "pack_400", "channel": "mock", "idempotency_key": "key-1"},
        headers={"Authorization": "Bearer u1"},
    )
    order_id = r.json()["data"]["order"]["id"]
    await client.post(
        "/v1/billing/callbacks/payment",
        json={"payment_no": "pay-1", "order_id": order_id, "raw_digest": _digest("pay-1", order_id)},
    )
    ref = await client.post(
        f"/v1/billing/orders/{order_id}/refund", headers={"Authorization": "Bearer u1"}
    )
    assert ref.status_code == 409


async def test_billing_endpoints_require_auth(client) -> None:
    assert (await client.get("/v1/billing/products")).status_code == 200  # 商品列表公开
    assert (
        await client.post(
            "/v1/billing/orders",
            json={"product_code": "pack_400", "channel": "mock", "idempotency_key": "k"},
        )
    ).status_code == 401


async def test_create_order_invalid_payload_rejected(client) -> None:
    """Pydantic 校验：非法字段被 400 拒绝。"""
    await _seed_products(client)
    # 空 product_code
    r = await client.post(
        "/v1/billing/orders",
        json={"product_code": "", "channel": "mock", "idempotency_key": "k"},
        headers={"Authorization": "Bearer u1"},
    )
    assert r.status_code == 400
    # 超长 idempotency_key
    r2 = await client.post(
        "/v1/billing/orders",
        json={"product_code": "pack_400", "channel": "mock", "idempotency_key": "k" * 65},
        headers={"Authorization": "Bearer u1"},
    )
    assert r2.status_code == 400


async def test_callback_invalid_payload_rejected(client) -> None:
    """回调非法字段：400 拒绝。"""
    await _seed_products(client)
    r = await client.post(
        "/v1/billing/callbacks/payment",
        json={"payment_no": "", "order_id": "x", "raw_digest": "y"},
    )
    assert r.status_code == 400


async def test_callback_bad_signature_rejected(client) -> None:
    """回调签名不符：401 拒绝（防伪回调铸币）。"""
    await _seed_products(client)
    r = await client.post(
        "/v1/billing/orders",
        json={"product_code": "pack_400", "channel": "mock", "idempotency_key": "key-1"},
        headers={"Authorization": "Bearer u1"},
    )
    order_id = r.json()["data"]["order"]["id"]
    # 篡改 payment_no 使签名与订单不匹配
    c = await client.post(
        "/v1/billing/callbacks/payment",
        json={"payment_no": "pay-x", "order_id": order_id, "raw_digest": _digest("pay-1", order_id)},
    )
    assert c.status_code == 401
    # 订单仍未支付
    bal = await client.get("/v1/points/balance", headers={"Authorization": "Bearer u1"})
    assert bal.json()["data"]["purchased_balance"] == 0


async def test_callback_missing_secret_rejected(tmp_path) -> None:
    """签名密钥未配置：回调端点 fail-closed 返回 500（服务端配置缺失）。"""
    application = create_app(
        Settings(
            env="test",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}",
            payment_callback_secret="",
        )
    )
    engine = application.state.engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.post(
            "/v1/billing/callbacks/payment",
            json={"payment_no": "pay-1", "order_id": "o1", "raw_digest": "x"},
        )
        assert resp.status_code == 500
    await engine.dispose()


async def test_get_order_other_user_not_found(client) -> None:
    """订单查询水平越权防护：他人订单与不存在同响应 404。"""
    await _seed_products(client)
    r = await client.post(
        "/v1/billing/orders",
        json={"product_code": "pack_400", "channel": "mock", "idempotency_key": "key-1"},
        headers={"Authorization": "Bearer u1"},
    )
    order_id = r.json()["data"]["order"]["id"]
    # u2 查询 u1 的订单
    g = await client.get(
        f"/v1/billing/orders/{order_id}", headers={"Authorization": "Bearer u2"}
    )
    assert g.status_code == 404
    # 越权退款同样拒绝（既有规则回归）
    ref = await client.post(
        f"/v1/billing/orders/{order_id}/refund", headers={"Authorization": "Bearer u2"}
    )
    assert ref.status_code == 409


async def test_subscription_refund_blocked_after_monthly_grant(client) -> None:
    """订阅已领月赠：7 天内退款被拒（权益已使用）。"""
    await _seed_products(client)
    r = await client.post(
        "/v1/billing/orders",
        json={"product_code": "sub_monthly", "channel": "mock", "idempotency_key": "key-sub"},
        headers={"Authorization": "Bearer u1"},
    )
    order_id = r.json()["data"]["order"]["id"]
    await client.post(
        "/v1/billing/callbacks/payment",
        json={"payment_no": "pay-sub", "order_id": order_id, "raw_digest": _digest("pay-sub", order_id)},
    )
    # 领取月赠（权益已使用）
    m = await client.post(
        "/v1/billing/subscription/monthly-grant", headers={"Authorization": "Bearer u1"}
    )
    assert m.status_code == 200 and m.json()["data"]["granted_points"] == 400
    ref = await client.post(
        f"/v1/billing/orders/{order_id}/refund", headers={"Authorization": "Bearer u1"}
    )
    assert ref.status_code == 409


async def test_subscription_refund_trims_end_at(client) -> None:
    """订阅 7 天内未用退款：订单 refunded 且订阅时长回收（立即到期）。"""
    await _seed_products(client)
    r = await client.post(
        "/v1/billing/orders",
        json={"product_code": "sub_monthly", "channel": "mock", "idempotency_key": "key-sub"},
        headers={"Authorization": "Bearer u1"},
    )
    order_id = r.json()["data"]["order"]["id"]
    await client.post(
        "/v1/billing/callbacks/payment",
        json={"payment_no": "pay-sub", "order_id": order_id, "raw_digest": _digest("pay-sub", order_id)},
    )
    ref = await client.post(
        f"/v1/billing/orders/{order_id}/refund", headers={"Authorization": "Bearer u1"}
    )
    assert ref.status_code == 200
    assert ref.json()["data"]["status"] == "refunded"
    # 订阅时长被回收：end_at 不晚于当前时间
    s = await client.get("/v1/billing/subscription", headers={"Authorization": "Bearer u1"})
    sub = s.json()["data"]
    assert sub is not None
    end_at = datetime.fromisoformat(sub["end_at"])
    assert end_at <= datetime.now(UTC)
