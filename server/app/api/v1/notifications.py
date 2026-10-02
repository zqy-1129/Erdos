"""通知与调度接口（SP2-7）：验证码发送（限流）+ 调度任务触发。

验证码限流 60s/次 + 日 10 次；调度任务（月赠/到期冻结/对账）需 admin 角色，幂等批次。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_session_factory, require_roles
from app.core.clock import utc_now
from app.core.config import Settings
from app.core.envelope import Envelope, ok
from app.core.logging import request_id_var
from app.domain.notification.service import NotificationService
from app.domain.scheduler.service import SchedulerService
from app.infra.auth import Principal
from app.repository.notification import SQLAlchemyNotificationLogRepository
from app.repository.scheduler import (
    SQLAlchemyAccountLedgerSource,
    SQLAlchemySchedulerRunRepository,
)
from app.repository.uow import UnitOfWork

router = APIRouter(tags=["notifications"])


class VerificationCodeBody(BaseModel):
    target: str = Field(min_length=1, max_length=128)


class VerificationCodeView(BaseModel):
    code: str
    target: str


def _notification_service(request: Request, session: AsyncSession) -> NotificationService:
    settings: Settings = request.app.state.settings
    return NotificationService(
        SQLAlchemyNotificationLogRepository(session),
        request.app.state.notification_sender,
        request.app.state.code_limiter,
        settings,
    )


@router.post(
    "/notifications/verification-code",
    response_model=Envelope[VerificationCodeView],
    summary="发送验证码（限流）",
)
async def send_verification_code(
    request: Request,
    payload: VerificationCodeBody,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[VerificationCodeView]:
    """发送验证码：60s/次 + 日 10 次限流，防刷。"""
    now = utc_now()
    async with UnitOfWork(session_factory) as uow:
        code = await _notification_service(request, uow.session).send_verification_code(
            payload.target, now
        )
    return ok(VerificationCodeView(code=code, target=payload.target), request_id_var.get())


# ----------------------------------------------------------------------
# 调度任务触发（admin）
# ----------------------------------------------------------------------
admin_dep = Annotated[Principal, Depends(require_roles("admin"))]


class SchedulerView(BaseModel):
    result: str


def _scheduler_service(request: Request, session: AsyncSession) -> SchedulerService:
    from app.domain.scheduler.service import SchedulerService as Svc
    from app.infra.scheduler_runners import (
        SQLAlchemyExpireSubscriptionRunner,
        SQLAlchemyMonthlyGrantRunner,
    )

    settings: Settings = request.app.state.settings
    monthly_grant = SQLAlchemyMonthlyGrantRunner(
        session, settings.subscription_monthly_grant_points
    )
    expire_sub = SQLAlchemyExpireSubscriptionRunner(session)
    return Svc(
        SQLAlchemySchedulerRunRepository(session),
        SQLAlchemyAccountLedgerSource(session),
        monthly_grant,
        expire_sub,
    )


@router.post(
    "/scheduler/monthly-grant",
    response_model=Envelope[SchedulerView],
    summary="触发月赠任务（admin）",
)
async def trigger_monthly_grant(
    request: Request,
    principal: admin_dep,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[SchedulerView]:
    async with UnitOfWork(session_factory) as uow:
        result = await _scheduler_service(request, uow.session).run_monthly_grant(utc_now())
    return ok(SchedulerView(result=result), request_id_var.get())


@router.post(
    "/scheduler/expire-subscriptions",
    response_model=Envelope[SchedulerView],
    summary="触发订阅到期冻结任务（admin）",
)
async def trigger_expire_subscriptions(
    request: Request,
    principal: admin_dep,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[SchedulerView]:
    async with UnitOfWork(session_factory) as uow:
        result = await _scheduler_service(request, uow.session).run_expire_subscriptions(utc_now())
    return ok(SchedulerView(result=result), request_id_var.get())


@router.post(
    "/scheduler/reconcile",
    response_model=Envelope[dict],
    summary="触发每日对账任务（admin）",
)
async def trigger_reconcile(
    request: Request,
    principal: admin_dep,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[dict]:
    async with UnitOfWork(session_factory) as uow:
        result = await _scheduler_service(request, uow.session).run_reconcile(utc_now())
        view = {
            "alerted": result.alerted,
            "differences": [
                {"user_id": d.user_id, "balance": d.balance, "ledger_net": d.ledger_net}
                for d in result.differences
            ],
        }
    return ok(view, request_id_var.get())
