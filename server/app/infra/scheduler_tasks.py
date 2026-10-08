"""三大调度任务的用例装配（月赠 / 到期冻结 / 对账）——管理端接口与后台循环共用。

为什么单独一层：这两个入口过去各自拼一遍 SchedulerService 的依赖，参数一改就漏改一处；
更关键的是"对账差异要告警"这条规则得只有一份实现——告警落库开自己的事务，必须留在
对账事务提交之后，这个顺序在任何一处写错都会让 SQLite 单写锁自堵。
"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.domain.alerts.ports import AlertOutlet
from app.domain.scheduler.ports import ReconcileResult
from app.domain.scheduler.service import SchedulerService, reconcile_alert
from app.infra.scheduler_runners import (
    SQLAlchemyExpireSubscriptionRunner,
    SQLAlchemyMonthlyGrantRunner,
)
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
    """每日对账：差异全量发现，事务提交后经告警出口外发 P2（禁止静默）。"""
    async with UnitOfWork(session_factory) as uow:
        result = await _service(uow.session, settings).run_reconcile(now)

    alert = reconcile_alert(result)
    if alert is not None:
        await alerts.emit(alert, now)
    return result
