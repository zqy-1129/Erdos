"""积分域接口集成测试（SP2-4）：余额/账单/预扣/确认/退还 + 注册赠分接入。

test 环境用 DevTokenIntrospector：Bearer <subject> 透传为请求主体，
故用 Bearer u1 代表用户 u1（与单元测试账户口径一致）。
"""

from datetime import UTC, datetime

from app.domain.points.ports import BalanceType
from app.repository.points import SQLAlchemyPointAccountRepository
from app.repository.uow import UnitOfWork


async def _seed(client, user_id: str, purchased: int = 0, monthly: int = 0) -> None:
    """通过应用会话工厂预置账户余额（绕过服务层，直接落库）。"""
    factory = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    async with UnitOfWork(factory) as uow:
        repo = SQLAlchemyPointAccountRepository(uow.session)
        await repo.get_or_create(user_id, datetime.now(UTC))
        if purchased:
            await repo.credit(user_id, BalanceType.PURCHASED, purchased, datetime.now(UTC))
        if monthly:
            await repo.credit(user_id, BalanceType.MONTHLY, monthly, datetime.now(UTC))


async def test_balance_empty_account(client) -> None:
    """未入账账户：余额接口返回零余额并创建账户。"""
    resp = await client.get("/v1/points/balance", headers={"Authorization": "Bearer u1"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["purchased_balance"] == 0
    assert data["monthly_balance"] == 0
    assert data["frozen"] is False


async def test_reserve_confirm_refund_flow(client) -> None:
    """完整预扣-确认-退还链路（API 级）。"""
    await _seed(client, "u1", purchased=100)

    # 预扣 30
    r = await client.post(
        "/v1/points/reserve",
        json={"exec_id": "exec-1", "task_id": "task-1", "stage": "analysis", "points": 30},
        headers={"Authorization": "Bearer u1"},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["balance"]["purchased_balance"] == 70
    assert data["grant"]["signature"]  # 许可已签发
    assert data["grant"]["points"] == 30
    assert data["already_reserved"] is False

    # 重复预扣幂等
    r2 = await client.post(
        "/v1/points/reserve",
        json={"exec_id": "exec-1", "task_id": "task-1", "stage": "analysis", "points": 30},
        headers={"Authorization": "Bearer u1"},
    )
    assert r2.status_code == 200
    assert r2.json()["data"]["already_reserved"] is True
    assert r2.json()["data"]["balance"]["purchased_balance"] == 70  # 未二次扣减

    # 确认
    c = await client.post(
        "/v1/points/confirm",
        json={"exec_id": "exec-1"},
        headers={"Authorization": "Bearer u1"},
    )
    assert c.status_code == 200
    assert c.json()["data"]["status"] == "confirmed"
    assert c.json()["data"]["balance"]["purchased_balance"] == 70

    # 退还已确认的 -> 409
    ref = await client.post(
        "/v1/points/refund",
        json={"exec_id": "exec-1"},
        headers={"Authorization": "Bearer u1"},
    )
    assert ref.status_code == 409


async def test_refund_restores_balance(client) -> None:
    """退还返还余额。"""
    await _seed(client, "u1", purchased=100)
    await client.post(
        "/v1/points/reserve",
        json={"exec_id": "e1", "stage": "solve", "points": 20},
        headers={"Authorization": "Bearer u1"},
    )
    ref = await client.post(
        "/v1/points/refund",
        json={"exec_id": "e1"},
        headers={"Authorization": "Bearer u1"},
    )
    assert ref.status_code == 200
    assert ref.json()["data"]["status"] == "refunded"
    assert ref.json()["data"]["balance"]["purchased_balance"] == 100


async def test_reserve_insufficient(client) -> None:
    """余额不足：预扣返回 409。"""
    await _seed(client, "u1", purchased=50)
    resp = await client.post(
        "/v1/points/reserve",
        json={"exec_id": "e1", "stage": "analysis", "points": 100},
        headers={"Authorization": "Bearer u1"},
    )
    assert resp.status_code == 409


async def test_ledger_listing(client) -> None:
    """流水账单：分页返回，倒序。"""
    await _seed(client, "u1", purchased=100)
    await client.post(
        "/v1/points/reserve",
        json={"exec_id": "e1", "stage": "analysis", "points": 10},
        headers={"Authorization": "Bearer u1"},
    )
    await client.post(
        "/v1/points/reserve",
        json={"exec_id": "e2", "stage": "solve", "points": 20},
        headers={"Authorization": "Bearer u1"},
    )
    resp = await client.get(
        "/v1/points/ledger", headers={"Authorization": "Bearer u1"}
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 2
    assert len(data["items"]) == 2


async def test_points_endpoints_require_auth(client) -> None:
    """积分接口需登录态：无凭证返回 401。"""
    assert (await client.get("/v1/points/balance")).status_code == 401
    assert (
        await client.post(
            "/v1/points/reserve", json={"exec_id": "e", "stage": "s", "points": 10}
        )
    ).status_code == 401
