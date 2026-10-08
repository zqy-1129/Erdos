"""调度器服务（SP2-7）：月赠 / 订阅到期冻结 / 每日对账。

关键红线（对齐 SP2-7 提示词）：
- 调度任务带锁与幂等批次键（同批次重复触发只执行一次）；
- 对账差异全量发现并触发告警（禁止静默忽略）。
"""

from datetime import datetime

from app.domain.alerts.ports import BusinessAlert
from app.domain.alerts.severity import Severity, classify_reconcile_diff
from app.domain.scheduler.ports import (
    AccountLedgerSource,
    ExpireSubscriptionRunner,
    MonthlyGrantRunner,
    ReconcileDifference,
    ReconcileResult,
    SchedulerRunRepository,
)


def reconcile_alert(result: ReconcileResult) -> BusinessAlert | None:
    """对账差异 -> P2 告警短句；无差异返回 None（不外发）。

    短句只带差异计数，不落 user_id 明细（明细走接口响应给值班人员）。
    """
    severity = classify_reconcile_diff(len(result.differences))
    if severity is None:
        return None
    return BusinessAlert(
        key="reconcile_diff",
        severity=Severity.P2,
        message=f"积分对账差异 {len(result.differences)} 个账户（余额与流水净额不一致）",
        value=float(len(result.differences)),
    )


class SchedulerService:
    """调度用例：月赠 / 到期冻结 / 对账，均带幂等批次。"""

    def __init__(
        self,
        runs: SchedulerRunRepository,
        ledger_source: AccountLedgerSource,
        monthly_grant: MonthlyGrantRunner,
        expire_sub: ExpireSubscriptionRunner,
    ) -> None:
        self._runs = runs
        self._ledger_source = ledger_source
        self._monthly_grant = monthly_grant
        self._expire_sub = expire_sub

    # ------------------------------------------------------------------
    # 月赠（幂等批次）
    # ------------------------------------------------------------------
    async def run_monthly_grant(self, now: datetime) -> str:
        """月赠任务：批次键 = 年月，重复触发只执行一次。"""
        batch_key = f"{now.year}{now.month:02d}"
        if not await self._runs.claim("monthly_grant", batch_key, now):
            return "already_ran"
        granted = await self._monthly_grant.grant_all_active(now)
        result = f"granted:{granted}"
        await self._runs.mark_done("monthly_grant", batch_key, result, now)
        return result

    # ------------------------------------------------------------------
    # 订阅到期冻结（幂等批次）
    # ------------------------------------------------------------------
    async def run_expire_subscriptions(self, now: datetime) -> str:
        """订阅到期冻结：批次键 = 日期，重复触发只执行一次。"""
        batch_key = now.date().isoformat()
        if not await self._runs.claim("expire_subscriptions", batch_key, now):
            return "already_ran"
        expired = await self._expire_sub.expire_overdue(now)
        result = f"expired:{expired}"
        await self._runs.mark_done("expire_subscriptions", batch_key, result, now)
        return result

    # ------------------------------------------------------------------
    # 每日对账（差异发现 + 告警）
    # ------------------------------------------------------------------
    async def run_reconcile(self, now: datetime) -> ReconcileResult:
        """对账任务：对比账户余额与流水净额，差异全量发现。

        告警外发由调用方在事务提交后经 AlertOutlet 执行（reconcile_alert 负责分级与短句）：
        告警落库要开自己的事务，嵌在对账事务里会让 SQLite 单写锁自堵（database is locked）。
        """
        batch_key = now.date().isoformat()
        await self._runs.claim("reconcile", batch_key, now)

        differences: list[ReconcileDifference] = []
        for user_id, balance in await self._ledger_source.list_accounts():
            net = await self._ledger_source.ledger_net(user_id)
            if balance != net:
                differences.append(
                    ReconcileDifference(user_id=user_id, balance=balance, ledger_net=net)
                )

        result = ReconcileResult(
            differences=tuple(differences),
            alerted=len(differences) > 0,
        )
        await self._runs.mark_done(
            "reconcile", batch_key, f"diff:{len(differences)}", now
        )
        return result
