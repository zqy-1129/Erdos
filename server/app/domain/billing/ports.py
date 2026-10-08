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


class ChannelQueryStatus(StrEnum):
    """渠道查单结果三态（EC-N7 兜底补账的判定输入）。"""

    PAID = "paid"  # 渠道确认已收款（驱动内部已完成验签）
    NOT_PAID = "not_paid"  # 渠道明确未收款——只有这个状态才允许关单
    UNKNOWN = "unknown"  # 查询失败或渠道未接入：既不关单也不入账（fail-closed）


class ReconcileAction(StrEnum):
    """一次查单兜底对订单做的事，供调度汇总与压测断言用。"""

    SETTLED = "settled"  # 渠道已收款，本次补账入账
    CLOSED = "closed"  # 渠道确认未收款且已过支付时限，关单
    DIFFERENCE = "difference"  # 已收款但订单终态/金额不符——落差异台账并告警，绝不静默
    PENDING = "pending"  # 本次不动：未到查单时点、已终态、或渠道确认未收款且未过期
    THROTTLED = "throttled"  # 同一订单在本查单窗口内已查过（轮询去重）
    UNAVAILABLE = "unavailable"  # 渠道未注册或查单失败（不据此改变订单状态）
    NOT_FOUND = "not_found"  # 订单不存在


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


@dataclass(frozen=True, slots=True)
class ChannelPayment:
    """渠道确认的收款凭据：真实驱动须先完成渠道侧验签再返回本结构。"""

    payment_no: str
    amount_cents: int


@dataclass(frozen=True, slots=True)
class ChannelQueryResult:
    """一次查单结果；UNKNOWN 必须带 reason（诊断码或渠道回包摘要，不含密钥）。"""

    status: ChannelQueryStatus
    payment: ChannelPayment | None = None
    reason: str = ""


class PaymentChannel(Protocol):
    """支付渠道查询端口（DEC-022 查单兜底）。

    只管"查"，不管"入账"——入账一律走 payment_no 幂等回调链，避免两套发放逻辑。
    """

    name: str

    async def query_order(self, order_id: str) -> ChannelQueryResult:
        """查询订单在渠道侧的收款状态。"""
        ...


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

    async def list_reconcilable(
        self,
        *,
        created_before: datetime,
        closed_before: datetime,
        closed_after: datetime,
        limit: int,
    ) -> list[OrderRecord]:
        """列出待查单兜底的订单（最旧优先，上限 limit）。

        两类：created_at <= created_before 的未支付订单（到点查单补账/关单），
        以及 closed_before >= updated_at >= closed_after 的已关单订单（收款复核）。
        复核窗口必须有下界：否则历史关单单会因"最旧优先"永远占满批次，新单补账被饿死。
        """
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
    applied: bool  # True=本次入账；False=重复回调/终态订单幂等跳过
    difference: str | None = None  # 非空=需要落差异台账并告警的资金异常短句


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
