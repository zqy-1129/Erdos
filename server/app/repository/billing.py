"""计费订阅域仓储落库实现（products / orders / subscriptions / payment_callbacks）。

资金域关键：金额一律整数分；订单状态机原子迁移；幂等键与回调流水号唯一约束兜底。
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.billing.ports import (
    CallbackRecord,
    CallbackRepository,
    OrderRecord,
    OrderRepository,
    ProductRecord,
    ProductRepository,
    SubscriptionRecord,
    SubscriptionRepository,
)
from app.repository.models import (
    Order,
    PaymentCallback,
    Product,
    Subscription,
)


def _new_id() -> str:
    return str(uuid.uuid4())


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _utc_required(value: datetime | None) -> datetime:
    if value is None:
        raise ValueError("非空时间列读取为 NULL")
    return _ensure_utc(value)  # type: ignore[return-value]


def _product(row: Product) -> ProductRecord:
    return ProductRecord(
        id=row.id,
        code=row.code,
        type=row.type,
        name=row.name,
        price_cents=int(row.price_cents),
        points=int(row.points),
        duration_days=int(row.duration_days),
        active=bool(row.active),
    )


def _order(row: Order) -> OrderRecord:
    return OrderRecord(
        id=row.id,
        user_id=row.user_id,
        product_id=row.product_id,
        price_cents=int(row.price_cents),
        channel=row.channel,
        status=row.status,
        idempotency_key=row.idempotency_key,
        expires_at=_utc_required(row.expires_at),
        paid_at=_ensure_utc(row.paid_at),
        refunded_at=_ensure_utc(row.refunded_at),
        created_at=_utc_required(row.created_at),
    )


def _subscription(row: Subscription) -> SubscriptionRecord:
    return SubscriptionRecord(
        id=row.id,
        user_id=row.user_id,
        plan=row.plan,
        status=row.status,
        start_at=_utc_required(row.start_at),
        end_at=_utc_required(row.end_at),
        last_monthly_grant_at=_ensure_utc(row.last_monthly_grant_at),
        created_at=_utc_required(row.created_at),
    )


def _callback(row: PaymentCallback) -> CallbackRecord:
    return CallbackRecord(
        id=row.id,
        payment_no=row.payment_no,
        order_id=row.order_id,
        raw_digest=row.raw_digest,
        received_at=_utc_required(row.received_at),
        processed=bool(row.processed),
    )


class SQLAlchemyProductRepository(ProductRepository):
    """products 表实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active(self) -> list[ProductRecord]:
        rows = (
            (
                await self._session.execute(
                    select(Product).where(Product.active.is_(True)).order_by(Product.price_cents)
                )
            )
            .scalars()
            .all()
        )
        return [_product(r) for r in rows]

    async def get(self, product_id: str) -> ProductRecord | None:
        row = (
            await self._session.execute(
                select(Product).where(Product.id == product_id)
            )
        ).scalar_one_or_none()
        return _product(row) if row is not None else None

    async def get_by_code(self, code: str) -> ProductRecord | None:
        row = (
            await self._session.execute(
                select(Product).where(Product.code == code)
            )
        ).scalar_one_or_none()
        return _product(row) if row is not None else None


class SQLAlchemyOrderRepository(OrderRepository):
    """orders 表实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, record: OrderRecord) -> OrderRecord | None:
        row = Order(
            id=_new_id() if not record.id else record.id,
            user_id=record.user_id,
            product_id=record.product_id,
            price_cents=record.price_cents,
            channel=record.channel,
            status=record.status,
            idempotency_key=record.idempotency_key,
            expires_at=record.expires_at,
            paid_at=record.paid_at,
            refunded_at=record.refunded_at,
            created_at=record.created_at,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError:
            await self._session.rollback()
            return None
        return _order(row)

    async def find_by_idempotency_key(self, key: str) -> OrderRecord | None:
        row = (
            await self._session.execute(
                select(Order).where(Order.idempotency_key == key)
            )
        ).scalar_one_or_none()
        return _order(row) if row is not None else None

    async def get(self, order_id: str) -> OrderRecord | None:
        row = (
            await self._session.execute(select(Order).where(Order.id == order_id))
        ).scalar_one_or_none()
        return _order(row) if row is not None else None

    async def transition(
        self, order_id: str, from_status: str, to_status: str, now: datetime
    ) -> OrderRecord | None:
        values: dict = {"status": to_status, "updated_at": now}
        if to_status == "paid":
            values["paid_at"] = now
        elif to_status == "refunded":
            values["refunded_at"] = now
        result = await self._session.execute(
            update(Order)
            .where(Order.id == order_id, Order.status == from_status)
            .values(**values)
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            return None
        return await self.get(order_id)

    async def list_reconcilable(
        self,
        *,
        created_before: datetime,
        closed_before: datetime,
        closed_after: datetime,
        limit: int,
    ) -> list[OrderRecord]:
        """待兜底订单：到点未支付（查单补账/关单）+ 复核窗口内已关单（收款复核）。"""
        rows = (
            (
                await self._session.execute(
                    select(Order)
                    .where(
                        or_(
                            and_(
                                Order.status == "created",
                                Order.created_at <= created_before,
                            ),
                            and_(
                                Order.status == "closed",
                                Order.updated_at <= closed_before,
                                Order.updated_at >= closed_after,
                            ),
                        )
                    )
                    .order_by(Order.created_at)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_order(row) for row in rows]


class SQLAlchemySubscriptionRepository(SubscriptionRepository):
    """subscriptions 表实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: str, plan: str) -> SubscriptionRecord | None:
        row = (
            await self._session.execute(
                select(Subscription).where(
                    Subscription.user_id == user_id, Subscription.plan == plan
                )
            )
        ).scalar_one_or_none()
        return _subscription(row) if row is not None else None

    async def upsert(self, record: SubscriptionRecord) -> SubscriptionRecord:
        existing = await self.get(record.user_id, record.plan)
        ts = record.created_at or datetime.now(UTC)
        if existing is not None:
            await self._session.execute(
                update(Subscription)
                .where(Subscription.id == existing.id)
                .values(
                    status=record.status,
                    start_at=record.start_at,
                    end_at=record.end_at,
                    last_monthly_grant_at=record.last_monthly_grant_at,
                    updated_at=ts,
                )
            )
            return await self.get(record.user_id, record.plan)  # type: ignore[return-value]
        row = Subscription(
            id=_new_id() if not record.id else record.id,
            user_id=record.user_id,
            plan=record.plan,
            status=record.status,
            start_at=record.start_at,
            end_at=record.end_at,
            last_monthly_grant_at=record.last_monthly_grant_at,
            created_at=ts,
        )
        self._session.add(row)
        await self._session.flush()
        return _subscription(row)

    async def set_monthly_grant_at(self, user_id: str, plan: str, at: datetime) -> bool:
        result = await self._session.execute(
            update(Subscription)
            .where(Subscription.user_id == user_id, Subscription.plan == plan)
            .values(last_monthly_grant_at=at, updated_at=at)
        )
        return int(getattr(result, "rowcount", 0) or 0) == 1


class SQLAlchemyCallbackRepository(CallbackRepository):
    """payment_callbacks 表实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, record: CallbackRecord) -> CallbackRecord | None:
        row = PaymentCallback(
            id=_new_id() if not record.id else record.id,
            payment_no=record.payment_no,
            order_id=record.order_id,
            raw_digest=record.raw_digest,
            received_at=record.received_at,
            processed=record.processed,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError:
            await self._session.rollback()
            return None
        return _callback(row)

    async def find_by_payment_no(self, payment_no: str) -> CallbackRecord | None:
        row = (
            await self._session.execute(
                select(PaymentCallback).where(PaymentCallback.payment_no == payment_no)
            )
        ).scalar_one_or_none()
        return _callback(row) if row is not None else None
