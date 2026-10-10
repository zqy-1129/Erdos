"""计费域装配：把仓储 + 积分服务组装成 BillingService（接口层与查单兜底扫描共用）。

放在 infra 而不是 api/v1/billing.py 里，是因为查单兜底后台扫描也要构造同一个服务；
让装配根只有一处，避免两个入口的参数以后各自漂移。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.billing.service import BillingService
from app.domain.points.service import LicenseSigner, PointsService
from app.repository.billing import (
    SQLAlchemyCallbackRepository,
    SQLAlchemyOrderRepository,
    SQLAlchemyProductRepository,
    SQLAlchemySubscriptionRepository,
)
from app.repository.points import (
    SQLAlchemyGrantRepository,
    SQLAlchemyLedgerRepository,
    SQLAlchemyPointAccountRepository,
)


def build_billing_service(
    session: AsyncSession, settings: Settings, signer: LicenseSigner
) -> BillingService:
    """构造计费订阅服务（积分入账依赖同一会话，保证单事务提交语义）。"""
    points = PointsService(
        SQLAlchemyPointAccountRepository(session),
        SQLAlchemyLedgerRepository(session),
        SQLAlchemyGrantRepository(session),
        signer,
        settings,
    )
    return BillingService(
        SQLAlchemyProductRepository(session),
        SQLAlchemyOrderRepository(session),
        SQLAlchemySubscriptionRepository(session),
        SQLAlchemyCallbackRepository(session),
        points,
        settings,
    )
