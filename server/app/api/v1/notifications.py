"""通知与调度接口（SP2-7）：验证码发送（限流）+ 调度任务触发。

验证码限流 60s/次 + 日 10 次；调度任务（月赠/到期冻结/对账）需 admin 角色，幂等批次。
三个任务本体在 app/infra/scheduler_tasks.py，与后台循环（infra/scheduler_loop.py）共用同一实现。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_session_factory, record_audit, require_roles
from app.core.clock import utc_now
from app.core.config import Settings
from app.core.envelope import Envelope, ok
from app.core.logging import request_id_var
from app.domain.notification.service import NotificationService
from app.infra import scheduler_tasks
from app.infra.auth import Principal
from app.infra.notification_service import build_notification_service
from app.repository.uow import UnitOfWork

router = APIRouter(tags=["notifications"])


class VerificationCodeBody(BaseModel):
    target: str = Field(min_length=1, max_length=128)


class VerificationCodeView(BaseModel):
    code: str
    target: str


def _notification_service(request: Request, session: AsyncSession) -> NotificationService:
    """与调度任务共用同一装配（避免两处各拼一遍依赖，参数一改就漏改）。"""
    return build_notification_service(
        session,
        request.app.state.notification_sender,
        request.app.state.code_limiter,
        request.app.state.settings,
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
    """手动触发月赠（与后台循环同一实现，批次键幂等）。"""
    settings: Settings = request.app.state.settings
    now = utc_now()
    result = await scheduler_tasks.run_monthly_grant_task(session_factory, settings, now)
    await record_audit(
        request,
        action="admin.scheduler_trigger",
        actor_type="admin",
        actor_id=principal.subject,
        resource_type="scheduler",
        resource_id="monthly_grant",
        detail={"result": result},
        now=now,
    )
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
    """手动触发到期冻结（与后台循环同一实现，批次键幂等）。"""
    settings: Settings = request.app.state.settings
    now = utc_now()
    result = await scheduler_tasks.run_expire_subscriptions_task(
        session_factory, settings, now
    )
    await record_audit(
        request,
        action="admin.scheduler_trigger",
        actor_type="admin",
        actor_id=principal.subject,
        resource_type="scheduler",
        resource_id="expire_subscriptions",
        detail={"result": result},
        now=now,
    )
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
    """手动触发对账（与后台循环同一实现）：差异全量发现 + 事务提交后外发 P2 告警。"""
    settings: Settings = request.app.state.settings
    now = utc_now()
    result = await scheduler_tasks.run_reconcile_task(
        session_factory, settings, request.app.state.alert_outlet, now
    )
    view = {
        "alerted": result.alerted,
        "differences": [
            {"user_id": d.user_id, "balance": d.balance, "ledger_net": d.ledger_net}
            for d in result.differences
        ],
    }
    await record_audit(
        request,
        action="admin.scheduler_trigger",
        actor_type="admin",
        actor_id=principal.subject,
        resource_type="scheduler",
        resource_id="reconcile",
        detail={"differences": len(result.differences), "alerted": result.alerted},
        now=now,
    )
    return ok(view, request_id_var.get())
