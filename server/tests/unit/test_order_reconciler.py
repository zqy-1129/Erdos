"""查单兜底编排测试（PRD DF-003 异常处理 / EC-N7 / DEC-022）。

要钉住的行为（资金域，逐条都是"做错了就吞钱或重复入账"）：
- T+5 分钟前不打渠道；同窗口内轮询去重；
- PAID → 走 payment_no 幂等链补账；NOT_PAID 且过期 → 关单；NOT_PAID 未过期 → 不动；
- UNKNOWN / 渠道未注册 / 查单抛异常 → 一律不改订单状态（fail-closed，宁可留在 created）；
- 渠道已收款但订单已 closed → 差异审计台账 + P2 告警，closed 保持终态，且不重复记账；
- 已关单复核按天分桶、只回溯 7 天：既不天天打渠道，也不让陈旧关单挤掉待补账的新单；
- 后台循环周期 >0 真的扫、=0 立即退出（生产兜底不能只挂在客户端轮询上）。
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select, update

from app.core.config import Settings
from app.domain.billing.ports import (
    ChannelPayment,
    ChannelQueryResult,
    ChannelQueryStatus,
    CreateOrderRequest,
    ReconcileAction,
)
from app.infra.billing_deps import build_billing_service
from app.infra.payment_channels import MockPaymentChannel
from app.infra.payment_reconcile import (
    OrderReconciler,
    ReconcileSummary,
    run_order_reconcile_loop,
)
from app.repository.billing import SQLAlchemyOrderRepository
from app.repository.models import AuditLog, Order, Product
from app.repository.points import SQLAlchemyPointAccountRepository
from app.repository.uow import UnitOfWork


class StubChannel:
    """可编程渠道：记录调用次数并按脚本返回三态之一。"""

    name = "stub"

    def __init__(
        self, result: ChannelQueryResult | None = None, *, raises: bool = False
    ) -> None:
        self._result = result
        self._raises = raises
        self.calls: list[str] = []

    async def query_order(self, order_id: str) -> ChannelQueryResult:
        self.calls.append(order_id)
        if self._raises:
            raise RuntimeError("渠道网关 502")
        assert self._result is not None
        return self._result


class RecordingOutlet:
    """告警出口桩。"""

    def __init__(self) -> None:
        self.emitted: list[tuple[object, datetime]] = []

    async def emit(self, alert, now) -> bool:
        self.emitted.append((alert, now))
        return True


class StubSigner:
    def sign(self, payload: bytes) -> tuple[str, str]:
        return "deadbeef" * 8, "test-kid"


class CountingReconciler:
    """只做计数的扫描器替身（验证后台循环的启停语义）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def sweep(self, now: datetime, limit: int | None = None) -> ReconcileSummary:
        self.calls += 1
        return ReconcileSummary(scanned=0)


def _reconciler(
    session_factory, settings: Settings, channels: dict, outlet: RecordingOutlet
) -> OrderReconciler:
    return OrderReconciler(
        session_factory, channels, settings, StubSigner(), outlet  # type: ignore[arg-type]
    )


async def _seed_order(
    session_factory,
    settings: Settings,
    *,
    age_minutes: int,
    user: str = "u1",
    key: str = "key-1",
    channel: str = "stub",
    code: str = "pack_400",
) -> str:
    """下单并把 created_at 往前推 age_minutes 分钟（模拟"回调迟迟没来"）。"""
    now = datetime.now(UTC)
    async with UnitOfWork(session_factory) as uow:
        if (
            await uow.session.execute(select(Product).where(Product.code == code))
        ).scalar_one_or_none() is None:
            uow.session.add(
                Product(
                    code=code,
                    type="points_pack",
                    name="积分包 400",
                    price_cents=1800,
                    points=400,
                    duration_days=0,
                    active=True,
                )
            )
            await uow.session.flush()
        service = build_billing_service(uow.session, settings, StubSigner())  # type: ignore[arg-type]
        order = await service.create_order(
            CreateOrderRequest(user, code, channel, key),
            now - timedelta(minutes=age_minutes),
        )
        return order.order.id


async def _order_status(session_factory, order_id: str) -> str:
    async with UnitOfWork(session_factory) as uow:
        order = await SQLAlchemyOrderRepository(uow.session).get(order_id)
    assert order is not None
    return order.status


async def _balance(session_factory, user_id: str) -> int:
    async with UnitOfWork(session_factory) as uow:
        account = await SQLAlchemyPointAccountRepository(uow.session).get(user_id)
    return 0 if account is None else account.purchased_balance


def _paid(amount_cents: int = 1800, payment_no: str = "wx-1") -> ChannelQueryResult:
    return ChannelQueryResult(
        status=ChannelQueryStatus.PAID,
        payment=ChannelPayment(payment_no=payment_no, amount_cents=amount_cents),
    )


NOT_PAID = ChannelQueryResult(status=ChannelQueryStatus.NOT_PAID, reason="渠道无此收款")
UNKNOWN = ChannelQueryResult(status=ChannelQueryStatus.UNKNOWN, reason="渠道超时")


async def test_before_query_window_channel_is_not_called(session_factory, settings) -> None:
    """未到 T+5 分钟：不打渠道，订单原样保持 created。"""
    order_id = await _seed_order(session_factory, settings, age_minutes=1)
    channel = StubChannel(_paid())
    outlet = RecordingOutlet()
    reconciler = _reconciler(session_factory, settings, {"stub": channel}, outlet)

    action = await reconciler.reconcile_order(order_id, datetime.now(UTC))

    assert action is ReconcileAction.PENDING
    assert channel.calls == []
    assert await _order_status(session_factory, order_id) == "created"


async def test_paid_query_settles_order(session_factory, settings) -> None:
    """渠道确认收款：补账入账，订单转 paid，积分到账（回调丢失不再吞钱）。"""
    order_id = await _seed_order(session_factory, settings, age_minutes=10)
    channel = StubChannel(_paid())
    reconciler = _reconciler(session_factory, settings, {"stub": channel}, RecordingOutlet())

    action = await reconciler.reconcile_order(order_id, datetime.now(UTC))

    assert action is ReconcileAction.SETTLED
    assert await _order_status(session_factory, order_id) == "paid"
    assert await _balance(session_factory, "u1") == 400


async def test_not_paid_closes_only_after_expiry(session_factory, settings) -> None:
    """只有"渠道确认未收款"才允许关单，且要过了支付时限。"""
    open_order = await _seed_order(session_factory, settings, age_minutes=10, key="k-open")
    expired_order = await _seed_order(
        session_factory, settings, age_minutes=40, user="u2", key="k-expired"
    )
    channel = StubChannel(NOT_PAID)
    reconciler = _reconciler(session_factory, settings, {"stub": channel}, RecordingOutlet())
    now = datetime.now(UTC)

    assert await reconciler.reconcile_order(
        open_order, now
    ) is ReconcileAction.PENDING
    assert await reconciler.reconcile_order(
        expired_order, now
    ) is ReconcileAction.CLOSED
    assert await _order_status(session_factory, expired_order) == "closed"
    assert await _order_status(session_factory, open_order) == "created"
    assert len(channel.calls) == 2


async def test_unknown_and_unregistered_never_change_status(session_factory, settings) -> None:
    """UNKNOWN / 渠道未注册 / 查单异常：订单必须原地等待，绝不被"不确定"推着走。"""
    order_id = await _seed_order(session_factory, settings, age_minutes=40)  # 已过期
    outlet = RecordingOutlet()
    now = datetime.now(UTC)

    unknown = StubChannel(UNKNOWN)
    assert await _reconciler(
        session_factory, settings, {"stub": unknown}, outlet
    ).reconcile_order(order_id, now) is ReconcileAction.UNAVAILABLE
    assert await _order_status(session_factory, order_id) == "created"

    assert await _reconciler(session_factory, settings, {}, outlet).reconcile_order(
        order_id, now
    ) is ReconcileAction.UNAVAILABLE
    assert await _order_status(session_factory, order_id) == "created"

    boom = StubChannel(raises=True)
    assert await _reconciler(
        session_factory, settings, {"stub": boom}, outlet
    ).reconcile_order(order_id, now) is ReconcileAction.UNAVAILABLE
    assert await _order_status(session_factory, order_id) == "created"
    # 已过期但渠道只回答了 UNKNOWN/失败 —— 关单从未发生，钱还有路可走
    assert boom.calls == [order_id]


async def test_poll_throttle_queries_channel_once_per_window(session_factory, settings) -> None:
    """轮询去重：客户端每 2 秒轮一次，也只每个查单窗口打渠道一次（防打爆渠道）。"""
    order_id = await _seed_order(session_factory, settings, age_minutes=10)
    channel = StubChannel(NOT_PAID)
    reconciler = _reconciler(session_factory, settings, {"stub": channel}, RecordingOutlet())
    # 取当前查单桶起点，保证两次调用同桶（跨桶必然再查一次，测试会随机红）
    window = max(1, settings.order_query_window_minutes) * 60
    epoch = int(datetime.now(UTC).timestamp())
    base = datetime.fromtimestamp(epoch - epoch % window, UTC)

    assert (
        await reconciler.reconcile_order(order_id, base, throttle=True)
        is ReconcileAction.PENDING
    )
    assert (
        await reconciler.reconcile_order(order_id, base + timedelta(minutes=1), throttle=True)
        is ReconcileAction.THROTTLED
    )
    assert channel.calls == [order_id], "同窗口内第二次轮询不该再打渠道"


async def test_closed_but_paid_lands_difference_ledger(session_factory, settings) -> None:
    """已关单却查到收款：不入账、不迁移终态，落差异审计台账 + P2 告警，且记账幂等。"""
    order_id = await _seed_order(session_factory, settings, age_minutes=40)
    async with UnitOfWork(session_factory) as uow:
        await SQLAlchemyOrderRepository(uow.session).transition(
            order_id, "created", "closed", datetime.now(UTC)
        )

    outlet = RecordingOutlet()
    channel = StubChannel(_paid(payment_no="wx-7"))
    reconciler = _reconciler(session_factory, settings, {"stub": channel}, outlet)
    now = datetime.now(UTC)

    assert await reconciler.reconcile_order(order_id, now) is ReconcileAction.DIFFERENCE
    assert await _order_status(session_factory, order_id) == "closed"
    assert await _balance(session_factory, "u1") == 0, "差异订单不得自动发放权益"

    async with UnitOfWork(session_factory) as uow:
        rows = (await uow.session.execute(select(AuditLog))).scalars().all()
    diffs = [r for r in rows if r.action == "billing.payment_difference"]
    assert len(diffs) == 1
    assert diffs[0].request_key == f"payment-diff:{order_id}:wx-7"
    assert diffs[0].detail["evidence"] == "closed_recheck"
    assert diffs[0].actor_type == "system"
    assert len(outlet.emitted) == 1
    assert outlet.emitted[0][0].key == "payment_difference"  # type: ignore[union-attr]

    # 同一天桶内的二次复核被去重挡下（closed 复核按天分桶，不天天打渠道）
    assert (
        await reconciler.reconcile_order(order_id, now) is ReconcileAction.THROTTLED
    )
    assert channel.calls == [order_id], "已关单复核每天最多一次"
    assert len(outlet.emitted) == 1


async def test_sweep_tallies_actions_and_survives_failures(session_factory, settings) -> None:
    """批量扫描：逐单计数，单渠道异常不中断整轮。"""
    paid_order = await _seed_order(session_factory, settings, age_minutes=10, key="k-a")
    boom_order = await _seed_order(
        session_factory, settings, age_minutes=12, user="u2", key="k-b", channel="boom"
    )

    good = StubChannel(_paid())
    good.name = "stub"
    exploding = StubChannel(raises=True)
    exploding.name = "boom"
    reconciler = _reconciler(
        session_factory, settings, {"stub": good, "boom": exploding}, RecordingOutlet()
    )

    summary = await reconciler.sweep(datetime.now(UTC))

    assert summary.scanned == 2
    assert summary.settled == 1 and summary.unavailable == 1
    assert summary.as_dict()["settled"] == 1
    assert await _order_status(session_factory, paid_order) == "paid"
    assert await _order_status(session_factory, boom_order) == "created"


async def test_mock_channel_requires_explicit_evidence() -> None:
    """mock 渠道不得凭空判定已收款：未登记即 NOT_PAID（替身不伪装成真实通道）。"""
    channel = MockPaymentChannel()
    assert (await channel.query_order("o1")).status is ChannelQueryStatus.NOT_PAID

    channel.mark_paid("o1", "mock-1", 1800)
    result = await channel.query_order("o1")
    assert result.status is ChannelQueryStatus.PAID
    assert result.payment is not None and result.payment.amount_cents == 1800


async def test_amount_mismatch_records_difference_not_settlement(session_factory, settings) -> None:
    """PAID 但金额不符：不入账、不关单，落差异台账并告警（防"付 A 单的钱记到 B 单"）。"""
    order_id = await _seed_order(session_factory, settings, age_minutes=10)
    outlet = RecordingOutlet()
    channel = StubChannel(_paid(amount_cents=1, payment_no="wx-bad"))
    reconciler = _reconciler(session_factory, settings, {"stub": channel}, outlet)

    assert (
        await reconciler.reconcile_order(order_id, datetime.now(UTC))
        is ReconcileAction.DIFFERENCE
    )
    assert await _order_status(session_factory, order_id) == "created"
    assert await _balance(session_factory, "u1") == 0

    async with UnitOfWork(session_factory) as uow:
        rows = (await uow.session.execute(select(AuditLog))).scalars().all()
    diffs = [r for r in rows if r.action == "billing.payment_difference"]
    assert len(diffs) == 1
    assert diffs[0].detail["evidence"] == "channel_query"
    assert diffs[0].detail["channel_amount_cents"] == 1
    assert len(outlet.emitted) == 1


async def test_difference_ledger_is_idempotent_per_payment(session_factory, settings) -> None:
    """同一笔收款的差异只进一次台账：重复触发不追加记录、不重复告警。"""
    order_id = await _seed_order(session_factory, settings, age_minutes=10)
    outlet = RecordingOutlet()
    reconciler = _reconciler(session_factory, settings, {}, outlet)
    now = datetime.now(UTC)
    kwargs = {
        "order_id": order_id,
        "payment_no": "wx-1",
        "reason": "订单已关单但渠道确认已收款",
        "detail": {"evidence": "channel_query"},
    }

    await reconciler.record_difference(**kwargs, now=now)
    await reconciler.record_difference(**kwargs, now=now)

    async with UnitOfWork(session_factory) as uow:
        rows = (await uow.session.execute(select(AuditLog))).scalars().all()
    assert len([r for r in rows if r.action == "billing.payment_difference"]) == 1
    assert len(outlet.emitted) == 1


async def test_difference_alerts_even_if_ledger_write_fails(session_factory, settings) -> None:
    """台账写不进去也必须告警：资金差异宁后可复现地重复报警，也不静默。"""

    class _Broken:
        def __call__(self) -> None:
            raise RuntimeError("库不可用")

    outlet = RecordingOutlet()
    reconciler = OrderReconciler(
        _Broken(),  # type: ignore[arg-type]
        {},
        settings,
        StubSigner(),  # type: ignore[arg-type]
        outlet,
    )

    await reconciler.record_difference(
        order_id="o1",
        payment_no="wx-9",
        reason="金额不符",
        detail={},
        now=datetime.now(UTC),
    )

    assert len(outlet.emitted) == 1


async def test_reconcile_loop_sweeps_and_zero_disables(settings) -> None:
    """后台循环是生产兜底入口：周期>0 真的扫，=0 直接不启动（不能只靠客户端轮询）。"""
    stub = CountingReconciler()
    fast = settings.model_copy(update={"order_reconcile_interval_seconds": 1})
    app = SimpleNamespace(state=SimpleNamespace(settings=fast))
    app.state.order_reconciler = stub

    task = asyncio.create_task(run_order_reconcile_loop(app))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert stub.calls >= 1, "周期为正时后台扫描真的跑起来"

    off = settings.model_copy(update={"order_reconcile_interval_seconds": 0})
    idle = CountingReconciler()
    app_off = SimpleNamespace(state=SimpleNamespace(settings=off, order_reconciler=idle))
    await run_order_reconcile_loop(app_off)  # type: ignore[arg-type]
    assert idle.calls == 0, "周期为 0 时循环立即退出，不做任何扫描"


async def test_stale_closed_orders_cannot_starve_new_settlements(
    session_factory, settings
) -> None:
    """复核窗口必须有下界：历史关单单不得因"最旧优先"永远占满批次、饿死待补账的新单。"""
    stale = await _seed_order(session_factory, settings, age_minutes=40, key="k-stale")
    async with UnitOfWork(session_factory) as uow:
        await SQLAlchemyOrderRepository(uow.session).transition(
            stale, "created", "closed", datetime.now(UTC)
        )
        # 关单时间拨到 30 天前（超出 7 天复核窗口）
        await uow.session.execute(
            update(Order)
            .where(Order.id == stale)
            .values(updated_at=datetime.now(UTC) - timedelta(days=30))
        )
    fresh = await _seed_order(session_factory, settings, age_minutes=8, user="u2", key="k-fresh")

    good = StubChannel(_paid())
    good.name = "stub"
    reconciler = _reconciler(session_factory, settings, {"stub": good}, RecordingOutlet())
    summary = await reconciler.sweep(datetime.now(UTC))

    assert summary.scanned == 1, "陈旧关单不该再进复核批次"
    assert summary.settled == 1
    assert await _order_status(session_factory, fresh) == "paid"
    assert await _order_status(session_factory, stale) == "closed"
