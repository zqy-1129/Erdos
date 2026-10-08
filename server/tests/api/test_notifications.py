"""通知与调度接口集成测试（SP2-7）：验证码发送 + 调度任务触发 + 对账差异告警闭环。

test 环境用 DevTokenIntrospector；admin 角色通过 admin_client 夹具。
"""

from app.repository.models import PointAccount
from app.repository.uow import UnitOfWork


async def _inject_ledger_drift(app, user_id: str, balance: int) -> None:
    """造一条"余额与流水净额不一致"的账户（有余额、无流水）——对账必须全量发现它。"""
    async with UnitOfWork(app.state.session_factory) as uow:
        uow.session.add(
            PointAccount(
                user_id=user_id,
                purchased_balance=balance,
                monthly_balance=0,
                frozen=False,
                version=0,
            )
        )


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


async def test_reconcile_difference_reaches_alert_channel(admin_client, admin_app) -> None:
    """SP2-7 验收"构造差异流水对账全量发现并告警"的端到端版：

    差异不仅体现在接口响应的 alerted 字段里，还必须落成可回查的告警事件——
    此前告警分级机（P2 对账差异→消息提醒）在运行链路里零调用方，没人轮询接口就等于没告警。
    """
    await _inject_ledger_drift(admin_app, "drift-user", 100)

    resp = await admin_client.post(
        "/v1/scheduler/reconcile", headers={"Authorization": "Bearer admin"}
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["alerted"] is True
    assert [d["user_id"] for d in data["differences"]] == ["drift-user"]
    assert data["differences"][0] == {"user_id": "drift-user", "balance": 100, "ledger_net": 0}

    events = await admin_client.get(
        "/v1/admin/dashboard/events?types=monitor.alert",
        headers={"Authorization": "Bearer admin"},
    )
    body = events.json()["data"]
    assert body["total"] == 1, body
    payload = body["items"][0]["payload"]
    assert payload["metric"] == "reconcile_diff"
    assert payload["level"] == "P2"
    assert "drift-user" not in str(payload), "告警短句不落用户明细"
