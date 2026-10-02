"""报表域端口与数据载体（看板 P3 积分分析 + P4 资产明细，SP2-4/SP2-5 收尾）。

聚合口径（与积分/计费域流水语义对齐）：
- 发放 = kind=grant；消耗 = kind=reserve 且 status=confirmed；退还 = kind=reserve 且 status=refunded。
- GMV = 已支付订单（paid/refunded）金额合计；退款单列 refunded 金额。
- 时间聚合按 UTC 日，金额一律整数分，前端负责展示单位换算。
"""

from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PointsSummary:
    """积分域总览：双余额总量/挂起预扣/冻结账户/离线补报计数。"""

    total_purchased: int
    total_monthly: int
    account_count: int
    frozen_count: int
    reserved_points: int
    ledger_count: int
    offline_sync_last_24h: int

    @property
    def total_balance(self) -> int:
        return self.total_purchased + self.total_monthly


@dataclass(frozen=True, slots=True)
class PointsDailyPoint:
    """积分日聚合点。"""

    stat_date: date
    granted: int
    consumed: int
    refunded: int


@dataclass(frozen=True, slots=True)
class LedgerKindCount:
    """流水类型×状态分布计数。"""

    kind: str
    status: str
    count: int


@dataclass(frozen=True, slots=True)
class BillingSummary:
    """计费域总览：GMV/退款/订单与订阅状态计数，金额整数分。"""

    gmv_cents: int
    refunded_cents: int
    paid_order_count: int
    refunded_order_count: int
    closed_order_count: int
    active_subscriptions: int
    expired_subscriptions: int


@dataclass(frozen=True, slots=True)
class RevenueDailyPoint:
    """收入日聚合点：收入按 paid_at 计日（退款单列，同日轴）。"""

    stat_date: date
    revenue_cents: int
    refund_cents: int


@dataclass(frozen=True, slots=True)
class ProductSales:
    """商品销售分布（已支付口径：paid/refunded）。"""

    product_code: str
    product_name: str
    order_count: int
    amount_cents: int


class PointsReportingRepository(Protocol):
    """积分分析报表仓储端口（只读聚合）。"""

    async def summary(self) -> PointsSummary: ...
    async def daily_series(self, start: date, end: date) -> list[PointsDailyPoint]: ...
    async def kind_distribution(self) -> list[LedgerKindCount]: ...


class BillingReportingRepository(Protocol):
    """资产明细报表仓储端口（只读聚合）。"""

    async def summary(self) -> BillingSummary: ...
    async def daily_revenue(self, start: date, end: date) -> list[RevenueDailyPoint]: ...
    async def product_sales(self) -> list[ProductSales]: ...