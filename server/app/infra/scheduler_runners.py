"""调度执行器（SP2-7）：月赠 / 订阅到期冻结 / 续费提醒候选集。

直接操作订阅与积分表，供 SchedulerService 编排调用。
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.points.ports import BalanceType
from app.domain.points.service import PointsService
from app.domain.scheduler.ports import (
    ExpireSubscriptionRunner,
    MonthlyGrantRunner,
    RenewalReminderRunner,
    RenewalReminderTarget,
)
from app.repository.models import Account, Subscription
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


class SQLAlchemyRenewalReminderRunner(RenewalReminderRunner):
    """续费提醒候选集：到期窗口内的活跃订阅，左连用户表取邮箱。

    左连而不是内连：没有账号行或没填邮箱的订阅必须"现身"并被调用方计数，
    内连会把它们静默吞掉——那正是《服务端架构》承诺的提醒队列最容易漏的一类人。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_due(self, now: datetime, within_days: int) -> list[RenewalReminderTarget]:
        horizon = now + timedelta(days=within_days)
        stmt = (
            select(Subscription.user_id, Account.email, Subscription.end_at)
            .outerjoin(Account, Account.id == Subscription.user_id)
            .where(
                Subscription.status == "active",
                Subscription.end_at > now,
                Subscription.end_at <= horizon,
            )
            .order_by(Subscription.end_at)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            RenewalReminderTarget(
                user_id=row[0],
                email=row[1],
                # SQLite 落的是去掉偏移的 UTC 墙钟，读回来是 naive；补回 UTC 才能与 now 做算术
                end_at=row[2] if row[2].tzinfo else row[2].replace(tzinfo=UTC),
            )
            for row in rows
        ]
