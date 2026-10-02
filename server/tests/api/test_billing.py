"""计费订阅域接口集成测试（SP2-5）：商品 / 下单 / 回调 / 退款 / 订阅 / 月赠。

test 环境用 DevTokenIntrospector：Bearer <subject> 透传为请求主体。
"""

from app.repository.models import Product
from app.repository.uow import UnitOfWork


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
        json={"payment_no": "pay-1", "order_id": order_id, "raw_digest": "x"},
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
            json={"payment_no": "pay-1", "order_id": order_id, "raw_digest": "x"},
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
        json={"payment_no": "pay-sub", "order_id": order_id, "raw_digest": "x"},
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
        json={"payment_no": "pay-1", "order_id": order_id, "raw_digest": "x"},
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
