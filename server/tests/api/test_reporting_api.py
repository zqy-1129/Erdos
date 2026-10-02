"""看板 P3/P4 报表接口集成测试（SP2-4/SP2-5 收尾）。

覆盖：RBAC 拦截（无 admin 角色 403）、空库零值视图、种子数据口径、
days 参数校验（统一信封 40006/400）、零值填充序列。
"""

from datetime import UTC, datetime, timedelta

from app.repository.models import (
    Account,
    Order,
    PointAccount,
    PointLedger,
    Product,
    Subscription,
)

ADMIN = {"Authorization": "Bearer admin-token"}

REPORT_ENDPOINTS = [
    "/v1/admin/dashboard/points/summary",
    "/v1/admin/dashboard/points/trend",
    "/v1/admin/dashboard/points/distribution",
    "/v1/admin/dashboard/billing/summary",
    "/v1/admin/dashboard/billing/revenue",
    "/v1/admin/dashboard/billing/products",
]


async def _seed(client) -> None:
    """直接落库种子：积分流水分日 + 订单/订阅跨状态。"""
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)
    async with factory() as session:
        session.add(Account(id="u1", email="u1@example.com", password_hash="x", role="user"))
        session.add_all(
            [
                PointAccount(user_id="u1", purchased_balance=70, monthly_balance=0,
                             frozen=False, version=0),
                PointLedger(user_id="u1", exec_id="g1", delta=100, balance_type="purchased",
                            kind="grant", status="confirmed", source="register_gift",
                            created_at=now),
                PointLedger(user_id="u1", exec_id="r1", delta=-30, balance_type="purchased",
                        kind="reserve", status="confirmed", source="stage",
                        created_at=now),
            PointLedger(user_id="u1", exec_id="r2", delta=-20, balance_type="purchased",
                        kind="reserve", status="reserved", source="stage",
                        created_at=now),
            ]
        )
        session.add_all(
            [
                Product(id="p-sub", code="sub_monthly", type="subscription",
                        name="月度订阅", price_cents=2900, points=500, duration_days=30,
                        active=True),
                Order(id="o1", user_id="u1", product_id="p-sub", price_cents=2900,
                      channel="mock", status="paid", idempotency_key="k1",
                      expires_at=now + timedelta(minutes=30), paid_at=now,
                      created_at=yesterday),
                Subscription(id="s1", user_id="u1", plan="monthly", status="active",
                             start_at=now, end_at=now + timedelta(days=30), created_at=now),
            ]
        )
        await session.commit()


async def test_reporting_requires_admin_role(client) -> None:
    """非管理角色访问全部报表接口：403。"""
    for path in REPORT_ENDPOINTS:
        resp = await client.get(path, headers=ADMIN)
        assert resp.status_code == 403, f"{path} 应返回 403"


async def test_reporting_empty_views(admin_client) -> None:
    """空库返回零值视图，趋势序列按窗口补零。"""
    resp = await admin_client.get("/v1/admin/dashboard/points/summary", headers=ADMIN)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total_balance"] == 0 and data["account_count"] == 0
    assert data["frozen_count"] == 0 and data["reserved_points"] == 0

    resp = await admin_client.get("/v1/admin/dashboard/points/trend?days=7", headers=ADMIN)
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["days"] == 7 and len(data["series"]) == 7
    assert {p["granted"] for p in data["series"]} == {0}

    resp = await admin_client.get("/v1/admin/dashboard/billing/summary", headers=ADMIN)
    data = resp.json()["data"]
    assert data["gmv_cents"] == 0 and data["active_subscriptions"] == 0

    resp = await admin_client.get("/v1/admin/dashboard/points/distribution", headers=ADMIN)
    assert resp.json()["data"]["items"] == []
    resp = await admin_client.get("/v1/admin/dashboard/billing/products", headers=ADMIN)
    assert resp.json()["data"]["items"] == []


async def test_reporting_with_data(admin_client) -> None:
    """种子数据口径：总览/趋势/分布/商品聚合与落库一致。"""
    await _seed(admin_client)

    resp = await admin_client.get("/v1/admin/dashboard/points/summary", headers=ADMIN)
    data = resp.json()["data"]
    assert data["total_balance"] == 70
    assert data["total_purchased"] == 70
    assert data["account_count"] == 1
    assert data["reserved_points"] == 20
    assert data["ledger_count"] == 3

    resp = await admin_client.get("/v1/admin/dashboard/points/trend?days=3", headers=ADMIN)
    series = resp.json()["data"]["series"]
    assert len(series) == 3
    last = series[-1]  # 今日（种子数据落今天）
    assert (last["granted"], last["consumed"], last["refunded"]) == (100, 30, 0)
    assert series[0]["granted"] == 0  # 窗口前端补零

    resp = await admin_client.get("/v1/admin/dashboard/points/distribution", headers=ADMIN)
    by_pair = {(i["kind"], i["status"]): i["count"] for i in resp.json()["data"]["items"]}
    assert by_pair[("reserve", "reserved")] == 1
    assert by_pair[("grant", "confirmed")] == 1

    resp = await admin_client.get("/v1/admin/dashboard/billing/summary", headers=ADMIN)
    data = resp.json()["data"]
    assert data["gmv_cents"] == 2900
    assert data["paid_order_count"] == 1
    assert data["active_subscriptions"] == 1

    resp = await admin_client.get("/v1/admin/dashboard/billing/revenue?days=3", headers=ADMIN)
    series = resp.json()["data"]["series"]
    assert len(series) == 3 and series[-1]["revenue_cents"] == 2900

    resp = await admin_client.get("/v1/admin/dashboard/billing/products", headers=ADMIN)
    items = resp.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["product_code"] == "sub_monthly"
    assert items[0]["amount_cents"] == 2900


async def test_reporting_days_validation(admin_client) -> None:
    """days 越界：统一信封 400。"""
    for bad in ("0", "91", "-3"):
        resp = await admin_client.get(f"/v1/admin/dashboard/points/trend?days={bad}", headers=ADMIN)
        assert resp.status_code == 400
        assert resp.json()["code"] != 0
    resp = await admin_client.get("/v1/admin/dashboard/billing/revenue?days=400", headers=ADMIN)
    assert resp.status_code == 400