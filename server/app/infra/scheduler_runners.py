"""调度执行器（SP2-7）：月赠 / 订阅到期冻结。

直接操作订阅与积分表，供 SchedulerService 编排调用。
"""

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.points.ports import BalanceType
from app.domain.points.service import PointsService
from app.domain.scheduler.ports import ExpireSubscriptionRunner, MonthlyGrantRunner
from app.repository.models import Subscription
from app.repository.points import (
    SQLAlchemyGrantRepository,
    SQLAlchemyLedgerRepository,
    SQLAlchemyPointAccountRepository,
)


class SQLAlchemyMonthlyGrantRunner(MonthlyGrantRunner):
    """月赠执行器：遍历活跃订阅，逐个幂等发放月赠积分。"""

    def __init__(self, session: AsyncSession, monthly_points: int) -> None:
        self._session = session
        self._monthly_points = monthly_points

    async def grant_all_active(self, now: datetime) -> int:
        rows = (
            (
                await self._session.execute(
                    select(Subscription).where(Subscription.status == "active")
                )
            )
            .scalars()
            .all()
        )
        # 月赠入账幂等（exec_id 含年月，重复发放自动去重）
        points = PointsService(
            SQLAlchemyPointAccountRepository(self._session),
            SQLAlchemyLedgerRepository(self._session),
            SQLAlchemyGrantRepository(self._session),
            None,  # type: ignore[arg-type]  # 月赠无需许可签发
            None,  # type: ignore[arg-type]  # 无需 Settings
        )
        granted = 0
        for sub in rows:
            try:
                await points.grant_points(
                    sub.user_id,
                    self._monthly_points,
                    exec_id=f"monthly:{sub.user_id}:{sub.plan}:{now.year}{now.month:02d}",
                    balance_type=BalanceType.MONTHLY,
                    source="subscription_monthly",
                    now=now,
                )
                granted += 1
            except Exception:
                continue
        return granted


class SQLAlchemyExpireSubscriptionRunner(ExpireSubscriptionRunner):
    """订阅到期冻结执行器：到期订阅标记 expired 并冻结积分账户。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def expire_overdue(self, now: datetime) -> int:
        rows = (
            (
                await self._session.execute(
                    select(Subscription).where(
                        Subscription.status == "active",
                        Subscription.end_at < now,
                    )
                )
            )
            .scalars()
            .all()
        )
        account_repo = SQLAlchemyPointAccountRepository(self._session)
        for sub in rows:
            await self._session.execute(
                update(Subscription)
                .where(Subscription.id == sub.id)
                .values(status="expired")
            )
            # 冻结积分账户（禁止新任务，历史可读）
            await account_repo.set_frozen(sub.user_id, True, now)
        return len(rows)
