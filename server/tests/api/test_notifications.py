"""通知与调度接口集成测试（SP2-7）：验证码发送 + 调度任务触发。

test 环境用 DevTokenIntrospector；admin 角色通过 admin_client 夹具。
"""


async def test_verification_code_endpoint(client) -> None:
    """验证码发送接口：返回 6 位验证码。"""
    resp = await client.post(
        "/v1/notifications/verification-code",
        json={"target": "13800000000"},
        headers={"Authorization": "Bearer u1"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["target"] == "13800000000"
    assert len(data["code"]) == 6


async def test_scheduler_endpoints_require_admin(client, admin_client) -> None:
    """调度接口：普通用户 403，admin 成功。"""
    # 普通用户 403
    r_denied = await client.post(
        "/v1/scheduler/reconcile", headers={"Authorization": "Bearer u1"}
    )
    assert r_denied.status_code == 403

    # admin 成功
    r_ok = await admin_client.post(
        "/v1/scheduler/reconcile", headers={"Authorization": "Bearer admin"}
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["data"]["alerted"] is False  # 空库无差异


async def test_monthly_grant_endpoint(admin_client) -> None:
    """月赠调度接口：admin 触发成功（空库 granted:0）。"""
    r = await admin_client.post(
        "/v1/scheduler/monthly-grant", headers={"Authorization": "Bearer admin"}
    )
    assert r.status_code == 200
    assert r.json()["data"]["result"] == "granted:0"
