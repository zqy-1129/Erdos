"""调度任务的用例装配（月赠 / 到期冻结 / 对账 / SLO 预算燃尽）——管理端接口与后台循环共用。

为什么单独一层：这两个入口过去各自拼一遍 SchedulerService 的依赖，参数一改就漏改一处；
更关键的是"对账差异要告警"这条规则得只有一份实现——告警落库开自己的事务，必须留在
对账事务提交之后，这个顺序在任何一处写错都会让 SQLite 单写锁自堵。
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.domain.alerts.ports import AlertOutlet, BusinessAlert
from app.domain.alerts.severity import (
    SLO_BURN_METRIC,
    SLO_COVERAGE_METRIC,
    Severity,
)
from app.domain.alerts.slo import (
    SLO_WINDOW_DAYS,
    coverage_of,
    evaluate_burn,
)
from app.domain.scheduler.ports import ReconcileResult
from app.domain.scheduler.service import SchedulerService, reconcile_alert
from app.infra.scheduler_runners import (
    SQLAlchemyExpireSubscriptionRunner,
    SQLAlchemyMonthlyGrantRunner,
)
from app.repository.monitoring import SQLAlchemyMonitoringTrendRepository
from app.repository.scheduler import (
    SQLAlchemyAccountLedgerSource,
    SQLAlchemySchedulerRunRepository,
)
from app.repository.uow import UnitOfWork


def _service(session: AsyncSession, settings: Settings) -> SchedulerService:
    return SchedulerService(
        SQLAlchemySchedulerRunRepository(session),
        SQLAlchemyAccountLedgerSource(session),
        SQLAlchemyMonthlyGrantRunner(session, settings.subscription_monthly_grant_points),
        SQLAlchemyExpireSubscriptionRunner(session),
        stale_after_seconds=settings.scheduler_stale_after_seconds,
    )


async def run_monthly_grant_task(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings, now: datetime
) -> str:
    """月赠任务：批次键=年月，重复触发只发放一次。"""
    async with UnitOfWork(session_factory) as uow:
        result = await _service(uow.session, settings).run_monthly_grant(now)
    return result


async def run_expire_subscriptions_task(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings, now: datetime
) -> str:
    """订阅到期冻结：批次键=日期，重复触发只执行一次。"""
    async with UnitOfWork(session_factory) as uow:
        result = await _service(uow.session, settings).run_expire_subscriptions(now)
    return result


async def run_reconcile_task(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    alerts: AlertOutlet,
    now: datetime,
) -> ReconcileResult:
    """每日对账：差异全量发现 + 事务提交后外发 P2 + 顺带裁剪批次账本。"""
    prune_before = now - timedelta(days=settings.scheduler_runs_retention_days)
    async with UnitOfWork(session_factory) as uow:
        result = await _service(uow.session, settings).run_reconcile(
            now, prune_batches_older_than=prune_before
        )

    alert = reconcile_alert(result)
    if alert is not None:
        await alerts.emit(alert, now)
    return result


@dataclass
class SloBurnState:
    """最近一次燃尽/覆盖率判级（进程内）。

    只用于"档位变化才播报"：持续违规时不必每小时重复叫醒同一件事，而恢复必须说话。
    重启后字典为空、下一次检查会重新播报一次——宁可重复一次，也不要因为状态丢失而静默。
    """

    active: dict[str, Severity] = field(default_factory=dict)


def _transition(
    state: SloBurnState,
    key: str,
    new_severity: Severity | None,
    message: str,
    recovered_message: str,
    value: float,
    threshold: float,
) -> BusinessAlert | None:
    """判级变化 -> 待播报告警；无变化返回 None（同档持续不重复播报）。"""
    previous = state.active.get(key)
    if new_severity is None:
        if previous is None:
            return None
        state.active.pop(key)
        return BusinessAlert(
            key=key, severity=previous, message=recovered_message,
            value=value, threshold=threshold, state="recovered",
        )
    if previous == new_severity:
        return None
    state.active[key] = new_severity
    return BusinessAlert(
        key=key, severity=new_severity, message=message, value=value, threshold=threshold
    )


async def run_slo_burn_task(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    alerts: AlertOutlet,
    now: datetime,
    state: SloBurnState,
) -> str:
    """SLO 预算燃尽检查：读日窗口的分钟聚合，判"是否正在按超预算速率烧月度可用性"。

    聚合只读、独立短事务；告警一律在事务之后外发（同对账那条 SQLite 单写锁纪律）。
    """
    window_start = now - timedelta(days=SLO_WINDOW_DAYS)
    async with UnitOfWork(session_factory) as uow:
        observed, error_rate_sum = await SQLAlchemyMonitoringTrendRepository(
            uow.session
        ).window_error_minutes(window_start, now)

    coverage = coverage_of(observed, SLO_WINDOW_DAYS)
    burn = evaluate_burn(
        error_rate_sum,
        observed,
        window_days=SLO_WINDOW_DAYS,
        page_ratio=settings.slo_burn_page_ratio,
        warn_ratio=settings.slo_burn_warn_ratio,
    )

    if burn is None:
        triggered_message = (
            f"近 {SLO_WINDOW_DAYS} 日观测分钟 {observed} 不足，燃尽不判定（覆盖率 {coverage:.1%}）"
        )
        cleared_message = (
            f"SLO 燃尽暂停判定后重新可比（观测分钟 {observed}，覆盖率 {coverage:.1%}）"
        )
        burn_value, burn_threshold, burn_severity = coverage, 0.0, None
    else:
        triggered_message = (
            f"近 {SLO_WINDOW_DAYS} 日不可用 {burn.downtime_seconds:.0f}s / 预算份额 "
            f"{burn.budget_seconds:.0f}s（燃尽 {burn.burn_ratio:.0%}，窗口可用性 "
            f"{burn.availability:.3%}）——按此速率本月会违反 99.5% 可用性承诺"
        )
        cleared_message = (
            f"SLO 预算燃尽回到允许速率内（近 {SLO_WINDOW_DAYS} 日窗口可用性 "
            f"{burn.availability:.3%}，燃尽 {burn.burn_ratio:.0%}）"
        )
        burn_value = burn.burn_ratio
        burn_threshold = settings.slo_burn_page_ratio
        burn_severity = burn.severity

    alert = _transition(
        state,
        SLO_BURN_METRIC,
        burn_severity,
        triggered_message,
        cleared_message,
        burn_value,
        burn_threshold,
    )
    if alert is not None:
        await alerts.emit(alert, now)

    coverage_severity = Severity.P1 if coverage < settings.slo_coverage_floor else None
    coverage_alert = _transition(
        state,
        SLO_COVERAGE_METRIC,
        coverage_severity,
        f"近 {SLO_WINDOW_DAYS} 日监测覆盖率 {coverage:.1%}（低于 "
        f"{settings.slo_coverage_floor:.0%}），SLO 无法判定——无数据不等于健康",
        f"监测覆盖率已恢复到 {coverage:.1%}（阈值 {settings.slo_coverage_floor:.0%}）",
        coverage,
        settings.slo_coverage_floor,
    )
    if coverage_alert is not None:
        await alerts.emit(coverage_alert, now)

    ratio_text = "insufficient-data" if burn is None else f"{burn.burn_ratio:.4f}"
    return f"burn:{ratio_text} coverage:{coverage:.4f}"
