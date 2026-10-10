"""计费订阅域服务测试（SP2-5 资金域核心）：下单幂等 / 回调入账 / 退款规则 / 月赠防重。

覆盖：
- 下单幂等（idempotency_key）
- 回调 payment_no 幂等（重复回调不重复入账）
- 积分包入账（购买余额）
- 订阅激活与续费
- 退款规则（订阅 7 天未用可退 / 积分包不退 / 超期不退）
- 月赠防重（每人每月一次）
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.core.errors import CONFLICT, NOT_FOUND, AppError
from app.domain.billing.ports import (
    CallbackRecord,
    ChannelPayment,
    CreateOrderRequest,
)
from app.domain.billing.service import BillingService
from app.domain.points.service import PointsService
from app.repository.billing import (
    SQLAlchemyCallbackRepository,
    SQLAlchemyOrderRepository,
    SQLAlchemyProductRepository,
    SQLAlchemySubscriptionRepository,
)
from app.repository.models import Product
from app.repository.points import (
    SQLAlchemyGrantRepository,
    SQLAlchemyLedgerRepository,
    SQLAlchemyPointAccountRepository,
)
from app.repository.uow import UnitOfWork


class FakeSigner:
    def sign(self, payload: bytes) -> tuple[str, str]:
        return "deadbeef" * 8, "test-kid"


def _svc(session, settings: Settings) -> BillingService:
    points = PointsService(
        SQLAlchemyPointAccountRepository(session),
        SQLAlchemyLedgerRepository(session),
        SQLAlchemyGrantRepository(session),
        FakeSigner(),
        settings,
    )
    return BillingService(
        SQLAlchemyProductRepository(session),
        SQLAlchemyOrderRepository(session),
        SQLAlchemySubscriptionRepository(session),
        SQLAlchemyCallbackRepository(session),
        points,
        settings,
    )


async def _seed_products(session) -> None:
    """预置商品：月订阅 + 积分包。"""
    session.add(
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
    session.add(
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
    await session.flush()


async def _paid_order(session, settings: Settings, user_id: str, code: str) -> str:
    """下单并支付，返回订单 id。"""
    svc = _svc(session, settings)
    now = datetime.now(UTC)
    result = await svc.create_order(
        CreateOrderRequest(user_id, code, "mock", f"key-{code}-{user_id}"), now
    )
    order_id = result.order.id
    await svc.handle_callback(
        user_id,
        CallbackRecord(
            id="", payment_no=f"pay-{order_id}", order_id=order_id, raw_digest="x", received_at=now, processed=False
        ),
        now,
    )
    return order_id


async def test_create_order_idempotent(session_factory, settings) -> None:
    """下单幂等：同 idempotency_key 只生成一单。"""
    async with UnitOfWork(session_factory) as uow:
        await _seed_products(uow.session)
        svc = _svc(uow.session, settings)
        req = CreateOrderRequest("u1", "pack_400", "mock", "key-1")
        first = await svc.create_order(req, datetime.now(UTC))
        second = await svc.create_order(req, datetime.now(UTC))
        assert first.already_exists is False
        assert second.already_exists is True
        assert first.order.id == second.order.id


async def test_callback_points_pack_credits_balance(session_factory, settings) -> None:
    """积分包支付回调：购买余额入账 400 分。"""
    async with UnitOfWork(session_factory) as uow:
        await _seed_products(uow.session)
        svc = _svc(uow.session, settings)
        now = datetime.now(UTC)
        order = await svc.create_order(
            CreateOrderRequest("u1", "pack_400", "mock", "key-1"), now
        )
        result = await svc.handle_callback(
            "u1",
            CallbackRecord(id="", payment_no="pay-1", order_id=order.order.id, raw_digest="x", received_at=now, processed=False),
            now,
        )
        assert result.applied is True
        bal = await SQLAlchemyPointAccountRepository(uow.session).get("u1")
        assert bal.purchased_balance == 400


async def test_callback_idempotent_replay(session_factory, settings) -> None:
    """回调重放 10 次：权益只入账一次。"""
    async with UnitOfWork(session_factory) as uow:
        await _seed_products(uow.session)
        svc = _svc(uow.session, settings)
        now = datetime.now(UTC)
        order = await svc.create_order(
            CreateOrderRequest("u1", "pack_400", "mock", "key-1"), now
        )
        cb = CallbackRecord(id="", payment_no="pay-1", order_id=order.order.id, raw_digest="x", received_at=now, processed=False)
        for _ in range(10):
            await svc.handle_callback("u1", cb, now)
        bal = await SQLAlchemyPointAccountRepository(uow.session).get("u1")
        assert bal.purchased_balance == 400  # 只入账一次


async def test_callback_subscription_activates(session_factory, settings) -> None:
    """订阅支付回调：激活订阅。"""
    async with UnitOfWork(session_factory) as uow:
        await _seed_products(uow.session)
        await _paid_order(uow.session, settings, "u1", "sub_monthly")
        sub = await SQLAlchemySubscriptionRepository(uow.session).get("u1", "monthly")
        assert sub is not None
        assert sub.status == "active"
        assert sub.end_at > sub.start_at


async def test_refund_subscription_within_grace(session_factory, settings) -> None:
    """订阅 7 天内退款：成功，订单变 refunded。"""
    async with UnitOfWork(session_factory) as uow:
        await _seed_products(uow.session)
        svc = _svc(uow.session, settings)
        order_id = await _paid_order(uow.session, settings, "u1", "sub_monthly")
        result = await svc.refund("u1", order_id, datetime.now(UTC))
        assert result.refunded is True
        assert result.order.status == "refunded"


async def test_refund_points_pack_rejected(session_factory, settings) -> None:
    """积分包不退：退款返回 409。"""
    async with UnitOfWork(session_factory) as uow:
        await _seed_products(uow.session)
        svc = _svc(uow.session, settings)
        order_id = await _paid_order(uow.session, settings, "u1", "pack_400")
        with pytest.raises(AppError) as ei:
            await svc.refund("u1", order_id, datetime.now(UTC))
        assert ei.value.spec is CONFLICT


async def test_refund_subscription_after_grace_rejected(session_factory, settings) -> None:
    """订阅超 7 天退款：拒绝。"""
    async with UnitOfWork(session_factory) as uow:
        await _seed_products(uow.session)
        svc = _svc(uow.session, settings)
        order_id = await _paid_order(uow.session, settings, "u1", "sub_monthly")
        # 支付时间 8 天前
        order = await SQLAlchemyOrderRepository(uow.session).get(order_id)
        eight_days_later = order.paid_at + timedelta(days=8)
        with pytest.raises(AppError) as ei:
            await svc.refund("u1", order_id, eight_days_later)
        assert ei.value.spec is CONFLICT


async def test_monthly_grant_idempotent(session_factory, settings) -> None:
    """月赠防重：同月第二次触发只入账一次。"""
    async with UnitOfWork(session_factory) as uow:
        await _seed_products(uow.session)
        svc = _svc(uow.session, settings)
        await _paid_order(uow.session, settings, "u1", "sub_monthly")
        now = datetime.now(UTC)
        first = await svc.grant_monthly("u1", "monthly", now)
        assert first.granted_points == 400
        second = await svc.grant_monthly("u1", "monthly", now)
        assert second.already_granted is True
        assert second.granted_points == 0
        bal = await SQLAlchemyPointAccountRepository(uow.session).get("u1")
        assert bal.monthly_balance == 400  # 只入账一次


async def test_monthly_grant_no_subscription(session_factory, settings) -> None:
    """无订阅：月赠返回 404。"""
    async with UnitOfWork(session_factory) as uow:
        await _seed_products(uow.session)
        svc = _svc(uow.session, settings)
        with pytest.raises(AppError) as ei:
            await svc.grant_monthly("u1", "monthly", datetime.now(UTC))
        assert ei.value.spec is NOT_FOUND


async def test_create_order_unknown_product(session_factory, settings) -> None:
    """未知商品：下单返回 404。"""
    async with UnitOfWork(session_factory) as uow:
        await _seed_products(uow.session)
        svc = _svc(uow.session, settings)
        with pytest.raises(AppError) as ei:
            await svc.create_order(
                CreateOrderRequest("u1", "nope", "mock", "key-1"), datetime.now(UTC)
            )
        assert ei.value.spec is NOT_FOUND


# ----------------------------------------------------------------------
# 边界 / 异常补充（关单、重复退款、退款未支付、回调订单不存在）
# ----------------------------------------------------------------------
async def test_callback_settles_expired_but_open_order(session_factory, settings) -> None:
    """EC-N7 回归（吞款修复）：有效签名的迟到回调必须补账，不因过期被吞。

    旧实现先占用 payment_no 唯一约束、再抛 CONFLICT 并关单，重放只走幂等跳过分支，
    结果是"钱收了、权益没发、也没人知道"。回调能进到这里说明验签已过，即渠道确认收款。
    """
    async with UnitOfWork(session_factory) as uow:
        await _seed_products(uow.session)
        svc = _svc(uow.session, settings)
        now = datetime.now(UTC)
        order = await svc.create_order(
            CreateOrderRequest("u1", "pack_400", "mock", "key-1"), now
        )
        # 31 分钟后回调（已超过 30 分钟支付时限，但订单仍是 created）
        late = now + timedelta(minutes=31)
        result = await svc.handle_callback(
            "u1",
            CallbackRecord(id="", payment_no="pay-1", order_id=order.order.id, raw_digest="x",
                           received_at=late, processed=False),
            late,
        )
        assert result.applied is True
        assert result.order.status == "paid"
        assert result.difference is None
        balance = await SQLAlchemyPointAccountRepository(uow.session).get("u1")
        assert balance is not None and balance.purchased_balance == 400


async def test_callback_on_closed_order_reports_difference(session_factory, settings) -> None:
    """已关单（终态）却收到有效回调：不入账、不迁移终态，但必须返回差异交调用方记账告警。"""
    async with UnitOfWork(session_factory) as uow:
        await _seed_products(uow.session)
        svc = _svc(uow.session, settings)
        now = datetime.now(UTC)
        order = await svc.create_order(
            CreateOrderRequest("u1", "pack_400", "mock", "key-1"), now
        )
        await SQLAlchemyOrderRepository(uow.session).transition(
            order.order.id, "created", "closed", now
        )
        result = await svc.handle_callback(
            "u1",
            CallbackRecord(id="", payment_no="pay-late", order_id=order.order.id,
                           raw_digest="x", received_at=now, processed=False),
            now,
        )
        assert result.applied is False
        assert result.difference is not None and "已关单" in result.difference
        # closed 保持终态（口径：差异走人工退款，不开 closed→paid 例外）
        assert result.order.status == "closed"


async def test_settle_from_channel_requires_amount_match(session_factory, settings) -> None:
    """查单补账金额红线：渠道回包金额 ≠ 订单价一律不入账，返回差异待人工核。"""
    async with UnitOfWork(session_factory) as uow:
        await _seed_products(uow.session)
        svc = _svc(uow.session, settings)
        now = datetime.now(UTC)
        order = await svc.create_order(
            CreateOrderRequest("u1", "pack_400", "mock", "key-1"), now
        )
        result = await svc.settle_from_channel(
            order.order.id,
            ChannelPayment(payment_no="wx-1", amount_cents=1),  # 应为 1800 分
            "wechat",
            now,
        )
        assert result.applied is False
        assert result.difference is not None and "金额" in result.difference
        latest = await SQLAlchemyOrderRepository(uow.session).get(order.order.id)
        assert latest.status == "created"
        assert await SQLAlchemyPointAccountRepository(uow.session).get("u1") is None


async def test_settle_from_channel_is_idempotent(session_factory, settings) -> None:
    """查单补账走同一条 payment_no 幂等链：同一笔收款重复查单只入账一次。"""
    async with UnitOfWork(session_factory) as uow:
        await _seed_products(uow.session)
        svc = _svc(uow.session, settings)
        now = datetime.now(UTC)
        order = await svc.create_order(
            CreateOrderRequest("u1", "pack_400", "mock", "key-1"), now
        )
        payment = ChannelPayment(payment_no="wx-9", amount_cents=1800)
        first = await svc.settle_from_channel(order.order.id, payment, "wechat", now)
        second = await svc.settle_from_channel(order.order.id, payment, "wechat", now)
        assert first.applied is True and second.applied is False
        balance = await SQLAlchemyPointAccountRepository(uow.session).get("u1")
        assert balance is not None and balance.purchased_balance == 400


async def test_get_order_does_not_close_without_evidence(session_factory, settings) -> None:
    """只读查询不再盲关单：过期不等于没收款，关单必须有渠道"未收款"证据。"""
    async with UnitOfWork(session_factory) as uow:
        await _seed_products(uow.session)
        svc = _svc(uow.session, settings)
        now = datetime.now(UTC)
        order = await svc.create_order(
            CreateOrderRequest("u1", "pack_400", "mock", "key-1"), now
        )
        fetched = await svc.get_order(order.order.id)
        assert fetched is not None and fetched.status == "created"

        # 未过期 → 即便渠道确认未收款也不关单
        still_open = await svc.close_unpaid(order.order.id, now)
        assert still_open is not None and still_open.status == "created"
        # 已过期 + 渠道确认未收款 → 关单
        closed = await svc.close_unpaid(order.order.id, now + timedelta(minutes=31))
        assert closed is not None and closed.status == "closed"


async def test_refund_unpaid_order_rejected(session_factory, settings) -> None:
    """退款未支付订单：拒绝。"""
    async with UnitOfWork(session_factory) as uow:
        await _seed_products(uow.session)
        svc = _svc(uow.session, settings)
        order = await svc.create_order(
            CreateOrderRequest("u1", "sub_monthly", "mock", "key-1"), datetime.now(UTC)
        )
        with pytest.raises(AppError) as ei:
            await svc.refund("u1", order.order.id, datetime.now(UTC))
        assert ei.value.spec is CONFLICT


async def test_refund_already_refunded_rejected(session_factory, settings) -> None:
    """重复退款：已退款订单再次退款被拒。"""
    async with UnitOfWork(session_factory) as uow:
        await _seed_products(uow.session)
        svc = _svc(uow.session, settings)
        order_id = await _paid_order(uow.session, settings, "u1", "sub_monthly")
        await svc.refund("u1", order_id, datetime.now(UTC))
        with pytest.raises(AppError) as ei:
            await svc.refund("u1", order_id, datetime.now(UTC))
        assert ei.value.spec is CONFLICT


async def test_callback_unknown_order(session_factory, settings) -> None:
    """回调订单不存在：返回 404。"""
    async with UnitOfWork(session_factory) as uow:
        await _seed_products(uow.session)
        svc = _svc(uow.session, settings)
        with pytest.raises(AppError) as ei:
            await svc.handle_callback(
                "u1",
                CallbackRecord(id="", payment_no="pay-x", order_id="nope", raw_digest="x", received_at=datetime.now(UTC), processed=False),
                datetime.now(UTC),
            )
        assert ei.value.spec is NOT_FOUND


async def test_refund_other_users_order_rejected(session_factory, settings) -> None:
    """退款他人订单：越权拒绝。"""
    async with UnitOfWork(session_factory) as uow:
        await _seed_products(uow.session)
        svc = _svc(uow.session, settings)
        order_id = await _paid_order(uow.session, settings, "u1", "sub_monthly")
        with pytest.raises(AppError) as ei:
            await svc.refund("u2", order_id, datetime.now(UTC))
        assert ei.value.spec is CONFLICT
