"""端到端数据流闭环集成测试（SP2-8 数据流验收部分）。

覆盖执行计划 SP2-8 验收标准「DF-003/004/005/007/008 数据流闭环逐条演练」，
在单一客户端上走完整业务链路（下单→回调→预扣→确认/退还→月赠→权益快照），
验证跨模块集成正确性与一致性（对账一致率 100%）。

test 环境用 DevTokenIntrospector：Bearer u1 代表用户 u1。
"""

from app.repository.models import Product
from app.repository.scheduler import SQLAlchemyAccountLedgerSource
from app.repository.uow import UnitOfWork


async def _seed_products(client) -> None:
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    async with UnitOfWork(factory) as uow:
        uow.session.add(
            Product(
                code="sub_monthly", type="subscription", name="月订阅",
                price_cents=1900, points=400, duration_days=30, active=True,
            )
        )
        uow.session.add(
            Product(
                code="pack_400", type="points_pack", name="积分包 400",
                price_cents=1800, points=400, duration_days=0, active=True,
            )
        )
        await uow.session.flush()


async def _balance(client, user_id: str) -> dict:
    r = await client.get("/v1/points/balance", headers={"Authorization": f"Bearer {user_id}"})
    return r.json()["data"]


async def _buy_and_pay(client, user_id: str, code: str, key: str) -> str:
    """下单 + 回调支付，返回订单 ID。"""
    r = await client.post(
        "/v1/billing/orders",
        json={"product_code": code, "channel": "mock", "idempotency_key": key},
        headers={"Authorization": f"Bearer {user_id}"},
    )
    assert r.status_code == 200
    order_id = r.json()["data"]["order"]["id"]
    c = await client.post(
        "/v1/billing/callbacks/payment",
        json={"payment_no": f"pay-{key}", "order_id": order_id, "raw_digest": "x"},
    )
    assert c.status_code == 200
    return order_id


# ----------------------------------------------------------------------
# DF-003 + DF-004 + DF-007：订阅购买 → 预扣 → 确认/退还
# ----------------------------------------------------------------------
async def test_df003_004_007_full_cycle(client) -> None:
    """完整资金链路：买积分包 → 预扣 → 确认 → 退还，余额全程一致。"""
    await _seed_products(client)

    # DF-003：购买积分包 400 分
    await _buy_and_pay(client, "u1", "pack_400", "k-pack")
    bal = await _balance(client, "u1")
    assert bal["purchased_balance"] == 400

    # DF-004：预扣 100
    r = await client.post(
        "/v1/points/reserve",
        json={"exec_id": "e1", "stage": "analysis", "points": 100},
        headers={"Authorization": "Bearer u1"},
    )
    assert r.status_code == 200
    assert r.json()["data"]["grant"]["points"] == 100  # 许可已签发
    bal = await _balance(client, "u1")
    assert bal["purchased_balance"] == 300  # 预扣后

    # DF-007：确认消耗
    c = await client.post(
        "/v1/points/confirm",
        json={"exec_id": "e1"},
        headers={"Authorization": "Bearer u1"},
    )
    assert c.status_code == 200
    assert c.json()["data"]["status"] == "confirmed"

    # DF-007：退还（另一次预扣）
    await client.post(
        "/v1/points/reserve",
        json={"exec_id": "e2", "stage": "solve", "points": 50},
        headers={"Authorization": "Bearer u1"},
    )
    ref = await client.post(
        "/v1/points/refund",
        json={"exec_id": "e2"},
        headers={"Authorization": "Bearer u1"},
    )
    assert ref.status_code == 200
    bal = await _balance(client, "u1")
    # 400(买包) - 100(预扣e1已确认消耗) - 50(预扣e2) + 50(e2退还) = 300
    assert bal["purchased_balance"] == 300

    # 对账一致性：余额 = 流水净额（refunded 的 e2 已返还不计入）
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    async with UnitOfWork(factory) as uow:
        source = SQLAlchemyAccountLedgerSource(uow.session)
        net = await source.ledger_net("u1")
    assert net == 300  # 流水净额等于余额，对账一致


# ----------------------------------------------------------------------
# DF-003 + DF-008：订阅 → 月赠
# ----------------------------------------------------------------------
async def test_df003_008_subscription_monthly_grant(client) -> None:
    """订阅 → 月赠：订阅激活后触发月赠，月度积分 +400。"""
    await _seed_products(client)

    # DF-003：购买订阅
    await _buy_and_pay(client, "u1", "sub_monthly", "k-sub")
    s = await client.get(
        "/v1/billing/subscription", headers={"Authorization": "Bearer u1"}
    )
    assert s.json()["data"]["status"] == "active"

    # DF-008：月赠（通过调度接口或直接触发）
    m = await client.post(
        "/v1/billing/subscription/monthly-grant", headers={"Authorization": "Bearer u1"}
    )
    assert m.status_code == 200
    assert m.json()["data"]["granted_points"] == 400

    bal = await _balance(client, "u1")
    assert bal["monthly_balance"] == 400  # 月度积分到账


# ----------------------------------------------------------------------
# DF-005：离线对账补扣
# ----------------------------------------------------------------------
async def test_df005_offline_reconcile(client) -> None:
    """离线消耗批量对账：余额充足扣减，不足逐条回执。"""
    await _seed_products(client)
    await _buy_and_pay(client, "u1", "pack_400", "k-pack")

    resp = await client.post(
        "/v1/points/offline-sync",
        json={
            "items": [
                {"exec_id": "off-1", "stage": "analysis", "points": 100},
                {"exec_id": "off-2", "stage": "solve", "points": 500},  # 余额不足
            ]
        },
        headers={"Authorization": "Bearer u1"},
    )
    data = resp.json()["data"]
    assert data["applied"] == 1  # off-1 成功扣减
    assert data["insufficient"] == 1  # off-2 余额不足逐条回执
    bal = await _balance(client, "u1")
    assert bal["purchased_balance"] == 300  # 400 - 100


# ----------------------------------------------------------------------
# DF-003 权益快照：订阅+积分 → 快照签发
# ----------------------------------------------------------------------
async def test_entitlement_snapshot_end_to_end(client) -> None:
    """权益快照：购买积分包后签发快照，快照含正确积分状态。"""
    await _seed_products(client)
    await _buy_and_pay(client, "u1", "pack_400", "k-pack")

    r = await client.get(
        "/v1/entitlements/snapshot", headers={"Authorization": "Bearer u1"}
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["payload"]["purchased_balance"] == 400
    assert data["signature"]  # 有签名
    assert data["key_version"]
