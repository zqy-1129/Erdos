"""关键操作审计覆盖测试（《服务端架构》§9：登录/购买/权益变更/许可签发/离线对账/管理员操作）。

为什么单独一个文件：`audit_logs` 表此前唯一的写入方是外部提交的 POST /v1/audit/events，
服务端自己的关键操作一条都不落审计——"关键操作全审计"这条红线只有文档，没有证据。
这里逐个操作确认三件事：真的落库了、重放不重复记、异常信息不带密码。
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app.core.config import Settings
from app.domain.points.ports import BalanceType
from app.infra.audit_recorder import AuditRecorder
from app.infra.payment_channels import MockPaymentChannel
from app.main import create_app
from app.repository.models import AuditLog, Base, Order, Product
from app.repository.points import SQLAlchemyPointAccountRepository
from app.repository.uow import UnitOfWork

DEV_USERS = "alice:pw123:user"
GOOD_PW = "pw123"
BAD_PW = "wrong-secret"


def _app(client) -> object:
    return client._transport.app  # type: ignore[attr-defined]


async def _actions(client) -> list[AuditLog]:
    async with UnitOfWork(_app(client).state.session_factory) as uow:  # type: ignore[union-attr]
        return list((await uow.session.execute(select(AuditLog))).scalars().all())


async def _of(client, action: str) -> list[AuditLog]:
    return [row for row in await _actions(client) if row.action == action]


@pytest.fixture
async def login_env(tmp_path):
    """带 dev 种子账号的应用（登录成功/失败都要留痕）。"""
    settings = Settings(
        env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}",
        auth_dev_users=DEV_USERS,
        rate_limit_requests=1000,
    )
    application = create_app(settings)
    async with application.state.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as c:
        yield c, application
    await application.state.engine.dispose()


async def test_login_success_and_failure_are_audited(login_env) -> None:
    client, _ = login_env
    ok = await client.post(
        "/v1/auth/login", json={"username": "alice", "password": GOOD_PW, "device_id": "d1"}
    )
    assert ok.status_code == 200, ok.json()
    bad = await client.post(
        "/v1/auth/login",
        json={"username": "alice", "password": BAD_PW, "device_id": "d2"},
    )
    assert bad.status_code == 401

    success = await _of(client, "auth.login")
    denied = await _of(client, "auth.login_denied")
    assert len(success) == 1 and success[0].actor_id == "alice"
    assert len(denied) == 1 and denied[0].detail["code"] == 40103
    # 审计载荷里绝不能出现口令
    assert GOOD_PW not in str(success[0].detail) and BAD_PW not in str(denied[0].detail)
    assert "password" not in str(denied[0].detail)
    assert denied[0].client_ip


async def test_order_paid_is_audited_once_per_order(client) -> None:
    """购买入账留痕（回调路径）：重放同一回调不得重复记审计。"""
    secret = "test-callback-secret"
    from app.domain.billing.service import callback_digest

    async with UnitOfWork(_app(client).state.session_factory) as uow:  # type: ignore[union-attr]
        uow.session.add(
            Product(
                code="pack_400", type="points_pack", name="积分包", price_cents=1800,
                points=400, duration_days=0, active=True,
            )
        )
    order_id = (
        await client.post(
            "/v1/billing/orders",
            json={"product_code": "pack_400", "channel": "mock", "idempotency_key": "k-1"},
            headers={"Authorization": "Bearer u1"},
        )
    ).json()["data"]["order"]["id"]

    for _ in range(3):
        await client.post(
            "/v1/billing/callbacks/payment",
            json={
                "payment_no": "pay-1",
                "order_id": order_id,
                "raw_digest": callback_digest(secret, "pay-1", order_id),
            },
        )

    rows = await _of(client, "billing.order_paid")
    assert len(rows) == 1, "同订单的审计必须幂等（回调重放不叠加）"
    assert rows[0].detail["evidence"] == "signed_callback"
    assert rows[0].request_key == f"order-paid:{order_id}"


async def test_query_settlement_is_audited_with_channel_evidence(client) -> None:
    """查单补账同样留痕，且 evidence 区分渠道查询与签名回调。"""
    async with UnitOfWork(_app(client).state.session_factory) as uow:  # type: ignore[union-attr]
        uow.session.add(
            Product(
                code="pack_400", type="points_pack", name="积分包", price_cents=1800,
                points=400, duration_days=0, active=True,
            )
        )
    order_id = (
        await client.post(
            "/v1/billing/orders",
            json={"product_code": "pack_400", "channel": "mock", "idempotency_key": "k-q"},
            headers={"Authorization": "Bearer u1"},
        )
    ).json()["data"]["order"]["id"]
    factory = _app(client).state.session_factory  # type: ignore[union-attr]
    async with UnitOfWork(factory) as uow:
        await uow.session.execute(
            update(Order)
            .where(Order.id == order_id)
            .values(created_at=datetime.now(UTC) - timedelta(minutes=10))
        )
    channel = _app(client).state.payment_channels["mock"]  # type: ignore[union-attr]
    assert isinstance(channel, MockPaymentChannel)
    channel.mark_paid(order_id, "mock-pay-1", 1800)

    await client.get(f"/v1/billing/orders/{order_id}", headers={"Authorization": "Bearer u1"})

    rows = await _of(client, "billing.order_paid")
    assert len(rows) == 1
    assert rows[0].detail["evidence"] == "channel_query"
    assert rows[0].actor_type == "system"


async def test_license_issued_audited_once_per_execution(client) -> None:
    """许可签发留痕：重复预扣（幂等返回原许可）不叠加审计。"""
    factory = _app(client).state.session_factory  # type: ignore[union-attr]
    now = datetime.now(UTC)
    async with UnitOfWork(factory) as uow:
        repo = SQLAlchemyPointAccountRepository(uow.session)
        await repo.get_or_create("u1", now)
        await repo.credit("u1", BalanceType.PURCHASED, 100, now)

    body = {"exec_id": "exec-a", "task_id": "t1", "stage": "analysis", "points": 30}
    for _ in range(2):
        resp = await client.post(
            "/v1/points/reserve", json=body, headers={"Authorization": "Bearer u1"}
        )
        assert resp.status_code == 200

    rows = await _of(client, "points.license_issued")
    assert len(rows) == 1
    assert rows[0].detail["stage"] == "analysis"
    assert rows[0].resource_id == "exec-a"


async def test_offline_reconcile_is_audited_with_counts(client) -> None:
    """离线对账留痕：一次批量一条审计，载荷带计数而不是逐条。"""
    factory = _app(client).state.session_factory  # type: ignore[union-attr]
    now = datetime.now(UTC)
    async with UnitOfWork(factory) as uow:
        repo = SQLAlchemyPointAccountRepository(uow.session)
        await repo.get_or_create("u1", now)
        await repo.credit("u1", BalanceType.PURCHASED, 100, now)

    resp = await client.post(
        "/v1/points/offline-sync",
        json={
            "items": [
                {"exec_id": "off-1", "task_id": "t1", "stage": "solving", "points": 20},
                {"exec_id": "off-2", "task_id": "t1", "stage": "writing", "points": 15},
            ]
        },
        headers={"Authorization": "Bearer u1"},
    )
    assert resp.status_code == 200

    rows = await _of(client, "points.offline_reconciled")
    assert len(rows) == 1
    assert rows[0].detail["applied"] == 2
    assert rows[0].detail["submitted"] == 2


async def test_admin_operations_are_audited(admin_client) -> None:
    """管理员操作留痕：内容写入与调度触发都要落审计，actor 是操作人而不是 system。"""
    body = {
        "business_id": "cumcm-2024-C",
        "competition": "cumcm",
        "format": "latex",
        "oss_key": "templates/a.tex",
        "sha256": "0" * 64,
        "version": 1,
        "tier": "member_only",
    }
    r1 = await admin_client.post(
        "/v1/content/templates", json=body, headers={"Authorization": "Bearer admin"}
    )
    assert r1.status_code == 200
    r2 = await admin_client.post(
        "/v1/scheduler/reconcile", headers={"Authorization": "Bearer admin"}
    )
    assert r2.status_code == 200

    content = await _of(admin_client, "admin.content_upsert")
    triggers = await _of(admin_client, "admin.scheduler_trigger")
    assert len(content) == 1
    assert content[0].actor_type == "admin" and content[0].resource_id == "cumcm-2024-C"
    assert len(triggers) == 1 and triggers[0].resource_id == "reconcile"


async def test_audit_write_failure_does_not_break_business_or_stay_silent(
    client, monkeypatch
) -> None:
    """留痕写不进去时：业务照常成功，但必须发 P2 告警——"缺审计"这件事本身不能静默。

    走离线对账这条（提交后独立事务写的 keyed 事件）；许可签发是同事务写，失败会连着业务一起回滚。
    """
    app = _app(client)
    outlet = RecordingOutlet()
    monkeypatch.setattr(app.state, "alert_outlet", outlet)  # type: ignore[arg-type]

    class _Broken:
        def __call__(self) -> None:
            raise RuntimeError("库不可用")

    monkeypatch.setattr(
        app.state, "audit_recorder", AuditRecorder(_Broken())  # type: ignore[arg-type]
    )

    resp = await client.get("/v1/points/balance", headers={"Authorization": "Bearer u1"})
    assert resp.status_code == 200  # 业务不受影响

    factory = app.state.session_factory  # type: ignore[union-attr]
    now = datetime.now(UTC)
    async with UnitOfWork(factory) as uow:
        repo = SQLAlchemyPointAccountRepository(uow.session)
        await repo.get_or_create("u1", now)
        await repo.credit("u1", BalanceType.PURCHASED, 50, now)
    post = await client.post(
        "/v1/points/offline-sync",
        json={"items": [{"exec_id": "off-x", "task_id": "t", "stage": "analysis", "points": 10}]},
        headers={"Authorization": "Bearer u1"},
    )

    assert post.status_code == 200, post.json()
    assert [a.key for a in outlet.emitted] == ["audit_write_failed"]
    assert await _of(client, "points.offline_reconciled") == []


class RecordingOutlet:
    def __init__(self) -> None:
        self.emitted: list[SimpleNamespace] = []

    async def emit(self, alert, now) -> bool:
        self.emitted.append(alert)
        return True
