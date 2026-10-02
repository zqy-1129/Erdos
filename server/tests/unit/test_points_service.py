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


class AppendConflictLedgerRepository:
    """包装真实仓储：append 恒返回 None，模拟并发撞唯一约束（事务已回滚）。"""

    def __init__(self, session) -> None:
        self._inner = SQLAlchemyLedgerRepository(session)

    async def find(self, *args, **kwargs):
        return await self._inner.find(*args, **kwargs)

    async def append(self, record):
        return None

    async def transition(self, *args, **kwargs):
        return await self._inner.transition(*args, **kwargs)

    async def list_by_user(self, *args, **kwargs):
        return await self._inner.list_by_user(*args, **kwargs)


def _svc_conflict(session, settings: Settings) -> PointsService:
    """append 必撞唯一约束的积分服务（并发第二事务视角）。"""
    return PointsService(
        SQLAlchemyPointAccountRepository(session),
        AppendConflictLedgerRepository(session),
        SQLAlchemyGrantRepository(session),
        FakeSigner(),
        settings,
    )


async def test_grant_registration_append_conflict_no_double_credit(session_factory, settings) -> None:
    """并发撞唯一约束：append 返回 None 时不做 credit，避免双重入账。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _svc_conflict(uow.session, settings)
        await svc.grant_registration("u1", 100, datetime.now(UTC))  # 不抛错、静默幂等
    async with session_factory() as s:
        bal = await _balance(s, "u1")
        assert bal is None or bal.purchased_balance == 0  # 未 credit


async def test_grant_points_append_conflict_no_double_credit(session_factory, settings) -> None:
    """通用入账并发撞唯一约束：append 返回 None 时不再 credit。"""
    async with UnitOfWork(session_factory) as uow:
        await SQLAlchemyPointAccountRepository(uow.session).get_or_create(
            "u1", datetime.now(UTC)
        )
        svc = _svc_conflict(uow.session, settings)
        await svc.grant_points(
            "u1", 400, exec_id="monthly:u1:monthly:202610",
            balance_type=BalanceType.MONTHLY, source="subscription_monthly",
            now=datetime.now(UTC),
        )
    async with session_factory() as s:
        bal = await _balance(s, "u1")
        assert bal is not None and bal.monthly_balance == 0  # 未重复入账


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


# ----------------------------------------------------------------------
# 离线对账（reconcile）：幂等去重、余额扣减、欠费冻结
# ----------------------------------------------------------------------
from app.domain.points.ports import OfflineItem  # noqa: E402


def _offline(*pairs: tuple[str, int]) -> list[OfflineItem]:
    return [
        OfflineItem(exec_id=eid, task_id="t", stage="analysis", points=pts)
        for eid, pts in pairs
    ]


async def test_reconcile_applies_and_deducts(session_factory, settings) -> None:
    """离线对账：余额充足时逐条扣减入账，流水为终态 offline_sync。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _svc(uow.session, settings)
        await svc.grant_registration("u1", 100, datetime.now(UTC))
        result = await svc.reconcile(
            "u1", _offline(("off-1", 20), ("off-2", 30)), datetime.now(UTC)
        )
        assert result.applied == 2
        assert result.duplicate == 0
        assert result.insufficient == 0
        assert result.frozen is False
        bal = await _balance(uow.session, "u1")
        assert bal.purchased_balance == 50  # 100 - 20 - 30


async def test_reconcile_idempotent_dedup(session_factory, settings) -> None:
    """离线对账幂等：同 exec_id 重复上报只入账一次。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _svc(uow.session, settings)
        await svc.grant_registration("u1", 100, datetime.now(UTC))
        items = _offline(("off-1", 20))
        first = await svc.reconcile("u1", items, datetime.now(UTC))
        assert first.applied == 1
        second = await svc.reconcile("u1", items, datetime.now(UTC))
        assert second.duplicate == 1
        assert second.applied == 0
        bal = await _balance(uow.session, "u1")
        assert bal.purchased_balance == 80  # 未二次扣减


async def test_reconcile_insufficient_not_silent(session_factory, settings) -> None:
    """余额不足：不静默忽略，逐条回执 insufficient，不扣减。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _svc(uow.session, settings)
        await svc.grant_registration("u1", 100, datetime.now(UTC))
        result = await svc.reconcile(
            "u1", _offline(("off-1", 200)), datetime.now(UTC)
        )
        assert result.insufficient == 1
        assert result.applied == 0
        assert result.items[0].status == "insufficient"
        bal = await _balance(uow.session, "u1")
        assert bal.purchased_balance == 100  # 未扣减


async def test_reconcile_freeze_when_balance_exhausted(session_factory, settings) -> None:
    """余额耗尽后冻结账户：禁止新任务，历史可读。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _svc(uow.session, settings)
        await svc.grant_registration("u1", 100, datetime.now(UTC))
        result = await svc.reconcile(
            "u1", _offline(("off-1", 100)), datetime.now(UTC)
        )
        assert result.applied == 1
        assert result.frozen is True  # 总余额归零 → 冻结
        bal = await _balance(uow.session, "u1")
        assert bal.frozen is True
        # 冻结后 reserve 拒绝
        with pytest.raises(AppError) as ei:
            await svc.reserve(ReserveRequest("e2", "u1", "t", "analysis", 10), datetime.now(UTC))
        assert ei.value.spec is ACCOUNT_FROZEN


async def test_reconcile_empty_rejected(session_factory, settings) -> None:
    """空对账上报：抛 400。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _svc(uow.session, settings)
        with pytest.raises(AppError) as ei:
            await svc.reconcile("u1", [], datetime.now(UTC))
        assert ei.value.spec.code == 40001  # BAD_REQUEST


# ----------------------------------------------------------------------
# 边界 / 异常补充（负数/零积分、对账负数）
# ----------------------------------------------------------------------
async def test_reserve_non_positive_points_rejected(session_factory, settings) -> None:
    """预扣负数/零积分：抛 400。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _svc(uow.session, settings)
        await svc.grant_registration("u1", 100, datetime.now(UTC))
        for bad in (0, -10):
            with pytest.raises(AppError) as ei:
                await svc.reserve(
                    ReserveRequest(f"e-{bad}", "u1", "t", "analysis", bad),
                    datetime.now(UTC),
                )
            assert ei.value.spec.code == 40001  # BAD_REQUEST


async def test_reconcile_non_positive_points_rejected(session_factory, settings) -> None:
    """离线对账负数积分：抛 400。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _svc(uow.session, settings)
        await svc.grant_registration("u1", 100, datetime.now(UTC))
        with pytest.raises(AppError) as ei:
            await svc.reconcile(
                "u1", [OfflineItem("off-1", "t", "analysis", -5)], datetime.now(UTC)
            )
        assert ei.value.spec.code == 40001  # BAD_REQUEST


async def test_grant_points_non_positive_rejected(session_factory, settings) -> None:
    """通用入账负数积分：抛 ValueError。"""
    async with UnitOfWork(session_factory) as uow:
        svc = _svc(uow.session, settings)
        with pytest.raises(ValueError):
            await svc.grant_points(
                "u1", 0, "e1", BalanceType.PURCHASED, "test", datetime.now(UTC)
            )
