"""积分域端口与数据载体（SP2-4，与《数据模型设计》积分与计费域对齐）。

资金域红线：流水追加式、终态不可迁移、余额扣减原子且防超扣、execId 幂等。
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class BalanceType(StrEnum):
    """余额类型：购买积分（永不过期）与月度积分（月底清零）。"""

    PURCHASED = "purchased"
    MONTHLY = "monthly"


class LedgerKind(StrEnum):
    """流水业务类型。"""

    GRANT = "grant"  # 赠分（注册/订阅月赠），直接终态
    RESERVE = "reserve"  # 预扣（阶段许可），初始态
    CONFIRM = "confirm"  # 确认（阶段完成），终态
    REFUND = "refund"  # 退还（阶段取消），终态
    OFFLINE_SYNC = "offline_sync"  # 离线补扣，终态


class LedgerStatus(StrEnum):
    """流水状态机：reserved → confirmed / refunded（终态）。"""

    RESERVED = "reserved"
    CONFIRMED = "confirmed"
    REFUNDED = "refunded"


# 终态集合（状态机迁移判定用）
_TERMINAL_STATUSES = frozenset({LedgerStatus.CONFIRMED, LedgerStatus.REFUNDED})


def is_terminal(status: str) -> bool:
    """某状态是否终态（confirmed/refunded）。"""
    return status in _TERMINAL_STATUSES


@dataclass(frozen=True, slots=True)
class AccountBalance:
    """积分账户视图（双余额 + 冻结标记 + 乐观锁版本）。"""

    user_id: str
    purchased_balance: int
    monthly_balance: int
    frozen: bool
    version: int

    @property
    def total(self) -> int:
        return self.purchased_balance + self.monthly_balance


@dataclass(frozen=True, slots=True)
class LedgerRecord:
    """积分流水视图。"""

    id: str
    user_id: str
    exec_id: str
    delta: int
    balance_type: str
    kind: str
    status: str
    source: str
    task_id: str | None
    stage: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class GrantRecord:
    """阶段许可视图。"""

    exec_id: str
    user_id: str
    task_id: str | None
    stage: str
    points: int
    signature: str
    key_version: str
    issued_at: datetime
    expires_at: datetime
    status: str


class PointAccountRepository(Protocol):
    """积分账户仓储端口（双余额 + 乐观锁原子扣减）。"""

    async def get_or_create(self, user_id: str, now: datetime) -> AccountBalance:
        """读取账户，不存在则创建（零余额）。"""
        ...

    async def get(self, user_id: str) -> AccountBalance | None:
        """按用户读取账户，不存在返回 None。"""
        ...

    async def credit(
        self,
        user_id: str,
        balance_type: BalanceType,
        amount: int,
        now: datetime,
    ) -> AccountBalance:
        """入账（赠分/月赠）；原子 +amount。"""
        ...

    async def debit(
        self,
        user_id: str,
        balance_type: BalanceType,
        amount: int,
        now: datetime,
    ) -> AccountBalance | None:
        """原子扣减 `WHERE balance ≥ amount`；余额不足返回 None（不抛异常，由服务层判定）。"""
        ...

    async def set_frozen(self, user_id: str, frozen: bool, now: datetime) -> bool:
        """设置欠费冻结标记；账户不存在返回 False。"""
        ...


class LedgerRepository(Protocol):
    """积分流水仓储端口（追加式，终态不可迁移）。"""

    async def find(self, user_id: str, exec_id: str, kind: str) -> LedgerRecord | None:
        """按幂等键（user_id, exec_id, kind）查流水。"""
        ...

    async def append(self, record: LedgerRecord) -> LedgerRecord | None:
        """追加流水；幂等键冲突时返回 None（并发兜底，供服务层重读已有）。"""
        ...

    async def transition(
        self,
        user_id: str,
        exec_id: str,
        kind: str,
        to_status: str,
    ) -> LedgerRecord | None:
        """状态迁移（reserved → confirmed/refunded）；终态或不存在返回 None。"""
        ...

    async def list_by_user(
        self, user_id: str, limit: int, offset: int
    ) -> tuple[list[LedgerRecord], int]:
        """分页账单（时间倒序），返回 (items, total)。"""
        ...


class GrantRepository(Protocol):
    """阶段许可仓储端口。"""

    async def find(self, exec_id: str) -> GrantRecord | None:
        """按许可幂等键读取。"""
        ...

    async def append(self, record: GrantRecord) -> GrantRecord:
        """签发许可（exec_id 幂等）。"""
        ...


@dataclass(frozen=True, slots=True)
class ReserveRequest:
    """一次阶段许可预扣请求。"""

    exec_id: str
    user_id: str
    task_id: str | None
    stage: str
    points: int


@dataclass(frozen=True, slots=True)
class ReserveResult:
    """预扣结果：余额视图 + 预扣流水 + 许可。"""

    balance: AccountBalance
    ledger: LedgerRecord
    grant: GrantRecord
    already_reserved: bool = field(default=False)


@dataclass(frozen=True, slots=True)
class SettleResult:
    """确认/退还结果：余额视图 + 流水。"""

    balance: AccountBalance
    ledger: LedgerRecord


@dataclass(frozen=True, slots=True)
class OfflineItem:
    """一条离线消耗上报记录（客户端联网后批量对账）。"""

    exec_id: str
    task_id: str | None
    stage: str
    points: int


@dataclass(frozen=True, slots=True)
class OfflineResult:
    """单条离线上报的处理结果。"""

    exec_id: str
    status: str  # "applied" | "duplicate" | "insufficient"
    balance: AccountBalance | None
    detail: str


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """批量对账汇总结果。"""

    applied: int
    duplicate: int
    insufficient: int
    frozen: bool
    balance: AccountBalance | None
    items: tuple[OfflineResult, ...]
