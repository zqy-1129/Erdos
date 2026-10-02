"""计费订阅域服务（SP2-5 资金域核心）：下单 / 回调入账 / 退款规则 / 月赠调度。

资金域红线（对齐《数据模型设计》与执行计划 SP2-5）：
- 金额一律整数分（禁止浮点数）；
- 订单状态机 created → paid / closed / refunded，终态不可迁移；
- 下单以 idempotency_key 幂等；回调以 payment_no 唯一约束幂等；
- 重复回调不重复发放权益；
- 月赠按 last_monthly_grant_at 幂等（每人每月只入账一次）；
- 退款规则（PRD F-005）：订阅 7 天内未使用任何权益可退，积分包不退。
"""

from datetime import datetime, timedelta

from app.core.config import Settings
from app.core.errors import CONFLICT, NOT_FOUND, AppError
from app.domain.billing.ports import (
    CallbackRecord,
    CallbackRepository,
    CallbackResult,
    CreateOrderRequest,
    CreateOrderResult,
    MonthlyGrantResult,
    OrderRecord,
    OrderRepository,
    OrderStatus,
    ProductRecord,
    ProductRepository,
    ProductType,
    RefundResult,
    SubscriptionRecord,
    SubscriptionRepository,
    SubscriptionStatus,
)
from app.domain.points.ports import BalanceType
from app.domain.points.service import PointsService


class BillingService:
    """计费订阅用例编排：下单 / 支付回调入账 / 退款 / 月赠。"""

    def __init__(
        self,
        products: ProductRepository,
        orders: OrderRepository,
        subscriptions: SubscriptionRepository,
        callbacks: CallbackRepository,
        points: PointsService,
        settings: Settings,
    ) -> None:
        self._products = products
        self._orders = orders
        self._subscriptions = subscriptions
        self._callbacks = callbacks
        self._points = points
        self._settings = settings

    # ------------------------------------------------------------------
    # 商品
    # ------------------------------------------------------------------
    async def list_products(self) -> list[ProductRecord]:
        return await self._products.list_active()

    async def get_order(self, order_id: str, now: datetime) -> OrderRecord | None:
        """查询订单（客户端轮询兜底补账）；created 且过期时主动关单。"""
        order = await self._orders.get(order_id)
        if order is None:
            return None
        if order.status == OrderStatus.CREATED.value and now > order.expires_at:
            await self._orders.transition(
                order.id, OrderStatus.CREATED.value, OrderStatus.CLOSED.value, now
            )
            return await self._orders.get(order_id)
        return order

    # ------------------------------------------------------------------
    # 下单：幂等 + 30 分钟关单
    # ------------------------------------------------------------------
    async def create_order(self, req: CreateOrderRequest, now: datetime) -> CreateOrderResult:
        """下单：以 idempotency_key 幂等，30 分钟未支付自动关单。"""
        product = await self._products.get_by_code(req.product_code)
        if product is None or not product.active:
            raise AppError(NOT_FOUND, detail="商品不存在或已下架")

        # 幂等：同幂等键已下过单 -> 返回既有订单
        existing = await self._orders.find_by_idempotency_key(req.idempotency_key)
        if existing is not None:
            return CreateOrderResult(
                order=existing, product=product, already_exists=True
            )

        expires_at = now + timedelta(minutes=self._settings.order_close_minutes)
        created = await self._orders.create(
            OrderRecord(
                id="",
                user_id=req.user_id,
                product_id=product.id,
                price_cents=product.price_cents,
                channel=req.channel,
                status=OrderStatus.CREATED.value,
                idempotency_key=req.idempotency_key,
                expires_at=expires_at,
                paid_at=None,
                refunded_at=None,
                created_at=now,
            )
        )
        if created is None:
            # 并发竞态：同幂等键已被创建，重读返回
            existing = await self._orders.find_by_idempotency_key(req.idempotency_key)
            if existing is None:
                raise AppError(CONFLICT, detail="下单并发冲突")
            return CreateOrderResult(
                order=existing, product=product, already_exists=True
            )
        return CreateOrderResult(order=created, product=product, already_exists=False)

    # ------------------------------------------------------------------
    # 支付回调：验签后入账（payment_no 幂等）
    # ------------------------------------------------------------------
    async def handle_callback(
        self, user_id: str, callback: CallbackRecord, now: datetime
    ) -> CallbackResult:
        """支付回调入账：payment_no 唯一约束幂等，重复回调不重复发放权益。

        入账规则：
        - 积分包：向积分账户购买余额入账（exec_id = order:{order_id}）；
        - 订阅：创建/续费订阅（叠加周期），并触发首期月赠。
        """
        # 幂等：payment_no 已处理过 -> 跳过
        existing_cb = await self._callbacks.find_by_payment_no(callback.payment_no)
        if existing_cb is not None:
            order = await self._orders.get(callback.order_id)
            if order is None:
                raise AppError(NOT_FOUND, detail="订单不存在")
            return CallbackResult(order=order, applied=False)

        order = await self._orders.get(callback.order_id)
        if order is None:
            raise AppError(NOT_FOUND, detail="订单不存在")

        # 记录回调（payment_no 唯一约束兜底并发重复）
        cb_record = await self._callbacks.append(
            CallbackRecord(
                id="",
                payment_no=callback.payment_no,
                order_id=callback.order_id,
                raw_digest=callback.raw_digest,
                received_at=now,
                processed=False,
            )
        )
        if cb_record is None:
            # 并发下撞唯一约束：另一请求已处理，幂等返回
            latest = await self._orders.get(callback.order_id)
            if latest is None:
                raise AppError(NOT_FOUND, detail="订单不存在")
            return CallbackResult(order=latest, applied=False)

        # 订单状态迁移：created -> paid
        if order.status != OrderStatus.CREATED.value:
            # 已关单/已退款/已支付：不重复入账
            return CallbackResult(order=order, applied=False)

        # 关单检查：订单已超过支付时限（30 分钟），先关单再拒绝入账
        if now > order.expires_at:
            await self._orders.transition(
                order.id, OrderStatus.CREATED.value, OrderStatus.CLOSED.value, now
            )
            raise AppError(CONFLICT, detail="订单已超时关单，无法入账")

        paid_order = await self._orders.transition(
            order.id, OrderStatus.CREATED.value, OrderStatus.PAID.value, now
        )
        if paid_order is None:
            return CallbackResult(order=order, applied=False)

        # 发放权益
        product = await self._products.get(order.product_id)
        if product is None:
            raise AppError(NOT_FOUND, detail="商品不存在")
        await self._grant_entitlement(order.user_id, product, paid_order.id, now)

        return CallbackResult(order=paid_order, applied=True)

    async def _grant_entitlement(
        self, user_id: str, product: ProductRecord, order_id: str, now: datetime
    ) -> None:
        """发放支付后的权益：积分包入账 / 订阅激活。"""
        if product.type == ProductType.POINTS_PACK.value:
            # 积分包：购买余额入账（永不过期）
            await self._points.grant_points(
                user_id,
                product.points,
                exec_id=f"order:{order_id}",
                balance_type=BalanceType.PURCHASED,
                source="points_pack",
                now=now,
            )
        else:
            # 订阅：创建/续费（叠加周期）
            plan = "monthly" if product.duration_days <= 31 else "yearly"
            existing = await self._subscriptions.get(user_id, plan)
            if existing is not None and existing.status == SubscriptionStatus.ACTIVE.value:
                # 续费：从现有 end_at 叠加周期
                start_at = existing.end_at
            else:
                start_at = now
            end_at = start_at + timedelta(days=product.duration_days)
            await self._subscriptions.upsert(
                SubscriptionRecord(
                    id="",
                    user_id=user_id,
                    plan=plan,
                    status=SubscriptionStatus.ACTIVE.value,
                    start_at=start_at if existing is None or existing.status != SubscriptionStatus.ACTIVE.value else existing.start_at,
                    end_at=end_at,
                    last_monthly_grant_at=existing.last_monthly_grant_at if existing is not None else None,
                )
            )

    # ------------------------------------------------------------------
    # 退款：订阅 7 天未用可退，积分包不退
    # ------------------------------------------------------------------
    async def refund(self, user_id: str, order_id: str, now: datetime) -> RefundResult:
        """退款（PRD F-005）：订阅 7 天内未使用权益可退；积分包不退。"""
        order = await self._orders.get(order_id)
        if order is None:
            raise AppError(NOT_FOUND, detail="订单不存在")
        if order.user_id != user_id:
            raise AppError(CONFLICT, detail="无权操作该订单")
        if order.status != OrderStatus.PAID.value:
            raise AppError(CONFLICT, detail="订单未支付，无法退款")

        product = await self._products.get(order.product_id)
        if product is None:
            raise AppError(NOT_FOUND, detail="商品不存在")

        # 积分包不退（虚拟消耗品）
        if product.type == ProductType.POINTS_PACK.value:
            raise AppError(CONFLICT, detail="积分包为虚拟消耗品，不支持退款")

        # 订阅：7 天宽限期（从支付时间起算）
        if order.paid_at is None:
            raise AppError(CONFLICT, detail="支付时间缺失")
        grace_deadline = order.paid_at + timedelta(days=self._settings.refund_grace_days)
        if now > grace_deadline:
            raise AppError(CONFLICT, detail="已超过 7 天退款宽限期")

        refunded = await self._orders.transition(
            order.id, OrderStatus.PAID.value, OrderStatus.REFUNDED.value, now
        )
        if refunded is None:
            raise AppError(CONFLICT, detail="订单状态已变化，退款失败")
        return RefundResult(order=refunded, refunded=True)

    # ------------------------------------------------------------------
    # 月赠调度：订阅每月赠积分（幂等批次键）
    # ------------------------------------------------------------------
    async def grant_monthly(
        self, user_id: str, plan: str, now: datetime
    ) -> MonthlyGrantResult:
        """订阅月赠：每人每月只入账一次（last_monthly_grant_at 幂等）。

        幂等依据：本月内已发放则跳过；否则发放月度积分并更新时间戳。
        """
        sub = await self._subscriptions.get(user_id, plan)
        if sub is None or sub.status != SubscriptionStatus.ACTIVE.value:
            raise AppError(NOT_FOUND, detail="无有效订阅")

        # 幂等：本月已赠过
        if sub.last_monthly_grant_at is not None:
            last = sub.last_monthly_grant_at
            if last.year == now.year and last.month == now.month:
                return MonthlyGrantResult(
                    user_id=user_id,
                    plan=plan,
                    granted_points=0,
                    already_granted=True,
                )

        # 发放月度积分（月底清零）
        points = self._settings.subscription_monthly_grant_points
        await self._points.grant_points(
            user_id,
            points,
            exec_id=f"monthly:{user_id}:{plan}:{now.year}{now.month:02d}",
            balance_type=BalanceType.MONTHLY,
            source="subscription_monthly",
            now=now,
        )
        await self._subscriptions.set_monthly_grant_at(user_id, plan, now)
        return MonthlyGrantResult(
            user_id=user_id, plan=plan, granted_points=points, already_granted=False
        )
