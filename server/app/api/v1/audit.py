"""审计事件写入接口：仅运营/管理员可写（审计记录不得由外部自由伪造）。"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_event_broker, get_session_factory, require_roles
from app.core.envelope import Envelope, ok
from app.core.logging import request_id_var
from app.core.topics import EVENTS_TOPIC
from app.domain.audit.ports import AuditEvent
from app.domain.audit.service import AuditService
from app.infra.auth import Principal
from app.infra.events import EventBroker
from app.repository.audit import SQLAlchemyAuditLogRepository
from app.repository.events import SQLAlchemyDashboardEventRepository
from app.repository.uow import UnitOfWork

router = APIRouter(tags=["audit"])
admin_dep = Annotated[Principal, Depends(require_roles("admin", "operator"))]


class AuditEventCreate(BaseModel):
    """审计事件写入请求体（与契约 AuditEventCreate 对齐，字段级校验在 API 边界完成）。"""

    model_config = ConfigDict(extra="forbid")

    actor_type: str = Field(min_length=1, max_length=32)
    actor_id: str = Field(min_length=1, max_length=64)
    action: str = Field(min_length=1, max_length=64)
    resource_type: str = Field(min_length=1, max_length=32)
    resource_id: str | None = Field(default=None, max_length=64)
    detail: dict[str, Any] | None = None
    client_ip: str | None = Field(default=None, max_length=45)


class AuditEventViewResp(BaseModel):
    """审计事件响应 DTO。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    actor_type: str
    actor_id: str
    action: str
    resource_type: str
    resource_id: str | None
    detail: dict[str, Any] | None
    client_ip: str | None
    created_at: datetime


@router.post("/audit/events", response_model=Envelope[AuditEventViewResp], summary="写入审计事件")
async def create_audit_event(
    payload: AuditEventCreate,
    request: Request,
    admin: admin_dep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=64)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    broker: Annotated[EventBroker, Depends(get_event_broker)],
) -> Envelope[AuditEventViewResp]:
    """幂等写接口：同 Idempotency-Key 只入账一次（重复返回 40901）。

    同事务投影到看板事件（dedup_key=幂等键），供走势图时间轴关联展示。

    角色限制是审计完整性的前提：actor_type/actor_id 全部来自请求体，无鉴权即等于任何人
    都能以他人身份写入审计记录（PRD 安全项"关键操作全审计"的可信度依赖这一点）。
    """
    event = AuditEvent(
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        action=payload.action,
        resource_type=payload.resource_type,
        resource_id=payload.resource_id,
        detail=payload.detail,
        client_ip=payload.client_ip or _client_ip(request),
        request_key=idempotency_key,
    )
    async with UnitOfWork(session_factory) as uow:
        service = AuditService(SQLAlchemyAuditLogRepository(uow.session))
        view = await service.record(event)
        await SQLAlchemyDashboardEventRepository(uow.session).append(
            occurred_at=view.created_at,
            type=f"audit.{event.action}",
            severity="info",
            actor_id=event.actor_id,
            payload={
                "actor_type": event.actor_type,
                "resource_type": event.resource_type,
                "resource_id": event.resource_id,
            },
            dedup_key=f"audit:{event.request_key}",
        )
    await broker.publish(
        EVENTS_TOPIC,
        {
            "type": f"audit.{event.action}",
            "severity": "info",
            "actor_id": event.actor_id,
            "occurred_at": view.created_at.isoformat(),
        },
    )
    return ok(
        AuditEventViewResp.model_validate(view),
        request_id_var.get(),
    )


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None