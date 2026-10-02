"""积分域服务测试（SP2-4 资金域核心）：状态机全路径 + 幂等 + 边界条件。

覆盖：
- 注册赠分入账（幂等）
- reserve/confirm/refund 状态机全路径
- 终态不可迁移
- 余额不足 / 冻结拒绝
- 双余额扣减顺序（月度优先）
- 许可签发内容正确
"""

from datetime import UTC, datetime

import pytest

from app.core.config import Settings
from app.core.errors import ACCOUNT_FROZEN, CONFLICT, NOT_FOUND, AppError
from app.domain.points.ports import (
    AccountBalance,
    BalanceType,
    ReserveRequest,
)
from app.domain.points.service import PointsService
from app.repository.points import (
    SQLAlchemyGrantRepository,
    SQLAlchemyLedgerRepository,
    SQLAlchemyPointAccountRepository,
)
from app.repository.uow import UnitOfWork


class FakeSigner:
    """测试签名器：返回确定性签名，不依赖真实密钥。"""

    def __init__(self) -> None:
        self.signed: list[bytes] = []

    def sign(self, payload: bytes) -> tuple[str, str]:
        self.signed.append(payload)
        return "deadbeef" * 8, "test-kid"


def _svc(session, settings: Settings) -> PointsService:
    return PointsService(
        SQLAlchemyPointAccountRepository(session),
        SQLAlchemyLedgerRepository(session),
        SQLAlchemyGrantRepository(session),
        FakeSigner(),
        settings,
    )


async def _balance(session, user_id: str) -> AccountBalance | None:
    return await SQLAlchemyPointAccountRepository(session).get(user_id)


async def test_grant_registration_idempotent(session_factory, settings) -> None:
    """注册赠分：同用户只入账一次，重复调用幂等。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _svc(uow.session, settings)
        await svc.grant_registration("u1", 100, datetime.now(UTC))
        await svc.grant_registration("u1", 100, datetime.now(UTC))  # 重复幂等
    async with session_factory() as s:
        bal = await _balance(s, "u1")
        assert bal is not None and bal.purchased_balance == 100
        # 流水只有一条 grant
        items, total = await SQLAlchemyLedgerRepository(s).list_by_user("u1", 10, 0)
        assert total == 1 and items[0].kind == "grant"


async def test_reserve_confirm_refund_happy_path(session_factory, settings) -> None:
    """完整状态机：赠分 -> 预扣 -> 确认 -> （另一笔）预扣 -> 退还。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _svc(uow.session, settings)
        await svc.grant_registration("u1", 100, datetime.now(UTC))
        # 预扣 30
        r = await svc.reserve(
            ReserveRequest("exec-1", "u1", "task-1", "analysis", 30),
            datetime.now(UTC),
        )
        assert r.balance.purchased_balance == 70
        assert r.ledger.status == "reserved"
        assert r.grant.signature == "deadbeef" * 8
        assert r.grant.key_version == "test-kid"
        assert r.grant.points == 30 and r.grant.stage == "analysis"
        # 确认
        c = await svc.confirm("u1", "exec-1", datetime.now(UTC))
        assert c.ledger.status == "confirmed"
        assert c.balance.purchased_balance == 70  # 确认不返还
        # 再预扣 20 并退还
        r2 = await svc.reserve(
            ReserveRequest("exec-2", "u1", "task-2", "solve", 20),
            datetime.now(UTC),
        )
        assert r2.balance.purchased_balance == 50
        ref = await svc.refund("u1", "exec-2", datetime.now(UTC))
        assert ref.ledger.status == "refunded"
        assert ref.balance.purchased_balance == 70  # 退还返还


async def test_reserve_idempotent(session_factory, settings) -> None:
    """重复预扣同 exec_id：不重复扣减，返回既有许可。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _svc(uow.session, settings)
        await svc.grant_registration("u1", 100, datetime.now(UTC))
        await svc.reserve(
            ReserveRequest("exec-1", "u1", "t", "analysis", 30), datetime.now(UTC)
        )
        r2 = await svc.reserve(
            ReserveRequest("exec-1", "u1", "t", "analysis", 30), datetime.now(UTC)
        )
        assert r2.already_reserved is True
        assert r2.grant.exec_id == "exec-1"
        assert r2.balance.purchased_balance == 70  # 未二次扣减


async def test_terminal_no_transition(session_factory, settings) -> None:
    """终态不可迁移：confirmed 后不可 refund；refunded 后不可 confirm。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _svc(uow.session, settings)
        await svc.grant_registration("u1", 100, datetime.now(UTC))
        await svc.reserve(ReserveRequest("e1", "u1", "t", "analysis", 30), datetime.now(UTC))
        await svc.confirm("u1", "e1", datetime.now(UTC))
        with pytest.raises(AppError) as ei:
            await svc.refund("u1", "e1", datetime.now(UTC))
        assert ei.value.spec is CONFLICT

    async with UnitOfWork(session_factory) as uow:
        svc = _svc(uow.session, settings)
        await svc.reserve(ReserveRequest("e2", "u1", "t", "solve", 20), datetime.now(UTC))
        await svc.refund("u1", "e2", datetime.now(UTC))
        with pytest.raises(AppError) as ei:
            await svc.confirm("u1", "e2", datetime.now(UTC))
        assert ei.value.spec is CONFLICT


async def test_insufficient_balance(session_factory, settings) -> None:
    """余额不足：reserve 抛 409，余额不变。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _svc(uow.session, settings)
        await svc.grant_registration("u1", 50, datetime.now(UTC))
        with pytest.raises(AppError) as ei:
            await svc.reserve(ReserveRequest("e1", "u1", "t", "analysis", 100), datetime.now(UTC))
        assert ei.value.spec is CONFLICT
        bal = await _balance(uow.session, "u1")
        assert bal.purchased_balance == 50


async def test_frozen_account_rejected(session_factory, settings) -> None:
    """欠费冻结：reserve 抛 403，不扣减。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _svc(uow.session, settings)
        await svc.grant_registration("u1", 100, datetime.now(UTC))
        await SQLAlchemyPointAccountRepository(uow.session).set_frozen(
            "u1", True, datetime.now(UTC)
        )
        with pytest.raises(AppError) as ei:
            await svc.reserve(ReserveRequest("e1", "u1", "t", "analysis", 10), datetime.now(UTC))
        assert ei.value.spec is ACCOUNT_FROZEN


async def test_monthly_balance_preferred(session_factory, settings) -> None:
    """双余额：月度优先扣减（月度足够时不动购买）。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _svc(uow.session, settings)
        await svc.grant_registration("u1", 100, datetime.now(UTC))  # 购买 +100
        # 手动加月度余额
        await SQLAlchemyPointAccountRepository(uow.session).credit(
            "u1", BalanceType.MONTHLY, 50, datetime.now(UTC)
        )
        r = await svc.reserve(ReserveRequest("e1", "u1", "t", "analysis", 30), datetime.now(UTC))
        assert r.ledger.balance_type == "monthly"  # 从月度扣
        bal = await _balance(uow.session, "u1")
        assert bal.monthly_balance == 20
        assert bal.purchased_balance == 100  # 购买未动


async def test_monthly_insufficient_falls_to_purchased(session_factory, settings) -> None:
    """双余额：月度不足时整笔从购买扣。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _svc(uow.session, settings)
        await svc.grant_registration("u1", 100, datetime.now(UTC))
        await SQLAlchemyPointAccountRepository(uow.session).credit(
            "u1", BalanceType.MONTHLY, 20, datetime.now(UTC)
        )
        r = await svc.reserve(ReserveRequest("e1", "u1", "t", "analysis", 30), datetime.now(UTC))
        assert r.ledger.balance_type == "purchased"  # 月度 20 不够 30，整笔从购买扣
        bal = await _balance(uow.session, "u1")
        assert bal.monthly_balance == 20  # 月度未动
        assert bal.purchased_balance == 70


async def test_confirm_refund_nonexistent(session_factory, settings) -> None:
    """不存在的流水：confirm/refund 抛 404。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _svc(uow.session, settings)
        with pytest.raises(AppError) as ei:
            await svc.confirm("u1", "nope", datetime.now(UTC))
        assert ei.value.spec is NOT_FOUND
        with pytest.raises(AppError) as ei:
            await svc.refund("u1", "nope", datetime.now(UTC))
        assert ei.value.spec is NOT_FOUND


async def test_confirm_refund_idempotent(session_factory, settings) -> None:
    """重复 confirm/refund 幂等返回终态，不重复返还/扣减。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _svc(uow.session, settings)
        await svc.grant_registration("u1", 100, datetime.now(UTC))
        await svc.reserve(ReserveRequest("e1", "u1", "t", "analysis", 30), datetime.now(UTC))
        await svc.refund("u1", "e1", datetime.now(UTC))
        bal_after_first = (await _balance(uow.session, "u1")).purchased_balance
        assert bal_after_first == 100
        # 重复 refund 幂等
        again = await svc.refund("u1", "e1", datetime.now(UTC))
        assert again.ledger.status == "refunded"
        bal_after_second = (await _balance(uow.session, "u1")).purchased_balance
        assert bal_after_second == 100  # 未二次返还


async def test_refund_restores_original_balance_type(session_factory, settings) -> None:
    """退还按原扣减类型返还（月度预扣退月度）。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _svc(uow.session, settings)
        await SQLAlchemyPointAccountRepository(uow.session).get_or_create(
            "u1", datetime.now(UTC)
        )
        await SQLAlchemyPointAccountRepository(uow.session).credit(
            "u1", BalanceType.MONTHLY, 50, datetime.now(UTC)
        )
        await svc.reserve(ReserveRequest("e1", "u1", "t", "analysis", 30), datetime.now(UTC))
        await svc.refund("u1", "e1", datetime.now(UTC))
        bal = await _balance(uow.session, "u1")
        assert bal.monthly_balance == 50  # 月度返还
