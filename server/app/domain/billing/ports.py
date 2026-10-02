"""计费订阅域端口与数据载体（SP2-5，与《数据模型设计》计费订阅域对齐）。

资金域红线：金额一律整数分；订单状态机 created → paid/closed/refunded；
回调 payment_no 唯一约束幂等；月赠按批次键幂等。
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class ProductType(StrEnum):
    """商品类型。"""

    SUBSCRIPTION = "subscription"
    POINTS_PACK = "points_pack"


class OrderStatus(StrEnum):
    """订单状态机：created → paid / closed / refunded（终态）。"""

    CREATED = "created"
    PAID = "paid"
    CLOSED = "closed"
    REFUNDED = "refunded"


class SubscriptionStatus(StrEnum):
    """订阅状态。"""

    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


# 订单终态集合
_TERMINAL_ORDER_STATUSES = frozenset(
    {OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.REFUNDED}
)


def is_terminal_order(status: str) -> bool:
    """订单状态是否终态（paid/closed/refunded）。"""
    return status in _TERMINAL_ORDER_STATUSES


@dataclass(frozen=True, slots=True)
class ProductRecord:
    """商品视图。"""

    id: str
    code: str
    type: str
    name: str
    price_cents: int
    points: int
    duration_days: int
    active: bool


@dataclass(frozen=True, slots=True)
class OrderRecord:
    """订单视图。"""

    id: str
    user_id: str
    product_id: str
    price_cents: int
    channel: str
    status: str
    idempotency_key: str
    expires_at: datetime
    paid_at: datetime | None
    refunded_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SubscriptionRecord:
    """订阅视图。"""

    id: str
    user_id: str
    plan: str
    status: str
    start_at: datetime
    end_at: datetime
    last_monthly_grant_at: datetime | None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CallbackRecord:
    """支付回调视图。"""

    id: str
    payment_no: str
    order_id: str
    raw_digest: str
    received_at: datetime
    processed: bool


class ProductRepository(Protocol):
    """商品仓储端口。"""

    async def list_active(self) -> list[ProductRecord]:
        """列出启用中的商品。"""
        ...

    async def get(self, product_id: str) -> ProductRecord | None:
        """按 id 读取商品。"""
        ...

    async def get_by_code(self, code: str) -> ProductRecord | None:
        """按编码读取商品。"""
        ...


class OrderRepository(Protocol):
    """订单仓储端口。"""

    async def create(self, record: OrderRecord) -> OrderRecord | None:
        """创建订单；幂等键冲突时返回 None（并发兜底）。"""
        ...

    async def find_by_idempotency_key(self, key: str) -> OrderRecord | None:
        """按幂等键查订单。"""
        ...

    async def get(self, order_id: str) -> OrderRecord | None:
        """按 id 查订单。"""
        ...

    async def transition(
        self, order_id: str, from_status: str, to_status: str, now: datetime
    ) -> OrderRecord | None:
        """订单状态原子迁移；不满足前置状态返回 None。"""
        ...


class SubscriptionRepository(Protocol):
    """订阅仓储端口。"""

    async def get(self, user_id: str, plan: str) -> SubscriptionRecord | None:
        """按用户与计划读订阅。"""
        ...

    async def upsert(self, record: SubscriptionRecord) -> SubscriptionRecord:
        """创建或更新订阅（续费叠加周期）。"""
        ...

    async def set_monthly_grant_at(
        self, user_id: str, plan: str, at: datetime
    ) -> bool:
        """更新月赠时间戳（幂等依据）；订阅不存在返回 False。"""
        ...


class CallbackRepository(Protocol):
    """支付回调仓储端口。"""

    async def append(self, record: CallbackRecord) -> CallbackRecord | None:
        """记录回调；payment_no 冲突返回 None（重复回调）。"""
        ...

    async def find_by_payment_no(self, payment_no: str) -> CallbackRecord | None:
        """按支付流水号查回调。"""
        ...


@dataclass(frozen=True, slots=True)
class CreateOrderRequest:
    """下单请求。"""

    user_id: str
    product_code: str
    channel: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CreateOrderResult:
    """下单结果：订单 + 支付参数（mock 通道返回占位）。"""

    order: OrderRecord
    product: ProductRecord
    already_exists: bool = field(default=False)


@dataclass(frozen=True, slots=True)
class PaymentCallbackRequest:
    """支付回调请求（验签后）。"""

    payment_no: str
    order_id: str
    raw_digest: str


@dataclass(frozen=True, slots=True)
class CallbackResult:
    """回调处理结果。"""

    order: OrderRecord
    applied: bool  # True=本次入账；False=重复回调幂等跳过


@dataclass(frozen=True, slots=True)
class RefundResult:
    """退款结果。"""

    order: OrderRecord
    refunded: bool = field(default=False)
    detail: str = field(default="")


@dataclass(frozen=True, slots=True)
class MonthlyGrantResult:
    """月赠发放结果。"""

    user_id: str
    plan: str
    granted_points: int
    already_granted: bool = field(default=False)
