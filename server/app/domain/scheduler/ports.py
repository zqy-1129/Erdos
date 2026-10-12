"""调度器域端口与数据载体（SP2-7）：月赠 / 订阅到期冻结 / 续费提醒 / 每日对账。

关键约束：
- 调度任务带锁与幂等批次键（重复触发只执行一次）；
- 对账差异全量发现并触发告警（禁止静默忽略）；
- 续费提醒的"只提醒一次"落在通知的 message_id 幂等上，不靠任务侧的记忆。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SchedulerRunRecord:
    """调度批次记录。"""

    id: str
    task_name: str
    batch_key: str
    status: str
    result: str | None
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReconcileDifference:
    """单条对账差异。"""

    user_id: str
    balance: int
    ledger_net: int


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """对账结果：差异列表 + 是否存在需告警的差异。"""

    differences: tuple[ReconcileDifference, ...]
    alerted: bool


@dataclass(frozen=True, slots=True)
class GrantOutcome:
    """月赠发放结果：成功数、失败数与失败的 exec_id（值班据此回查积分流水）。

    `failures` 有上限（只留前若干条）：账本与告警载荷不该随故障规模无界增长，
    失败总数在 `failed` 上如实计数，明细看日志。
    """

    granted: int
    failed: int
    failures: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MonthlyGrantRun:
    """月赠批次执行结果。

    `outcome` 为 None 表示本批次已被占用（already_ran），本轮没有执行发放，
    上层据此决定要不要告警——不能把"没跑"和"跑完零失败"混成一个值。
    """

    result: str
    outcome: GrantOutcome | None


class SchedulerRunRepository(Protocol):
    """调度批次仓储端口（task_name + batch_key 幂等）。"""

    async def claim(
        self, task_name: str, batch_key: str, now: datetime, *, stale_after_seconds: int = 0
    ) -> bool:
        """尝试认领批次（唯一约束兜底）；返回 False 表示本批次已被占用。

        stale_after_seconds>0 时允许重占"running 但已卡死"的批次（上次执行崩在半路），
        否则崩溃会让当月月赠永久不再发放；重复发放的安全性在积分流水 exec_id 上。
        """
        ...

    async def mark_done(self, task_name: str, batch_key: str, result: str, now: datetime) -> None:
        """标记批次完成。"""
        ...

    async def prune_before(self, cutoff: datetime) -> int:
        """删除 started_at < cutoff 的批次行，返回删除行数（账本必须有尽头）。"""
        ...


class AccountLedgerSource(Protocol):
    """对账数据源：账户余额 + 流水净额。"""

    async def list_accounts(self) -> list[tuple[str, int]]:
        """返回 [(user_id, 账户总余额)]。"""
        ...

    async def ledger_net(self, user_id: str) -> int:
        """返回用户流水净额（有效流水 delta 之和）。"""
        ...


class MonthlyGrantRunner(Protocol):
    """月赠执行器端口（适配积分域 grant_points 逻辑）。"""

    async def grant_all_active(self, now: datetime) -> GrantOutcome:
        """为全部活跃订阅发放月赠，返回成功/失败计数（失败必须可见，不得静默跳过）。"""
        ...


class ExpireSubscriptionRunner(Protocol):
    """订阅到期冻结执行器端口。"""

    async def expire_overdue(self, now: datetime) -> int:
        """到期订阅标记 expired 并冻结积分账户，返回冻结人数。"""
        ...


@dataclass(frozen=True, slots=True)
class RenewalReminderTarget:
    """一条到期前需提醒的订阅。

    `email` 可为 None：注销前未填邮箱或仅手机注册的用户确实存在，提醒发不出去。
    端口把"发不出去"这件事如实交给调用方计数与落日志，不在仓储层静默丢弃。
    """

    user_id: str
    email: str | None
    end_at: datetime


class RenewalReminderRunner(Protocol):
    """续费提醒候选集端口（《服务端架构》调度器："到期前 3 天进入提醒队列"）。"""

    async def list_due(self, now: datetime, within_days: int) -> list[RenewalReminderTarget]:
        """返回 end_at 落在 (now, now + within_days] 的活跃订阅提醒目标。"""
        ...
