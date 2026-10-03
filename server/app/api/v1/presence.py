"""在线状态接口（看板 FR-1/M1/M2）：心跳登记、分钟聚合与上线事件投影。

分钟桶写入自 2026-10-03 起改为进程内聚合（infra/presence_aggregator）+ 后台
周期落库：事务内仅 touch 会话与事件投影，消除同分钟桶单行漏斗（SP2-8 优化项 1）。
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_event_broker, get_session_factory, require_principal
from app.core.clock import utc_now
from app.core.envelope import Envelope, ok
from app.core.logging import request_id_var
from app.core.topics import EVENTS_TOPIC, PRESENCE_TOPIC
from app.domain.presence.ports import Heartbeat
from app.domain.presence.service import PresenceService
from app.infra.auth import Principal
from app.infra.events import EventBroker
from app.repository.events import SQLAlchemyDashboardEventRepository
from app.repository.presence import SQLAlchemyPresenceRepository
from app.repository.uow import UnitOfWork

router = APIRouter(tags=["presence"])

# 看板推送约束（合规）：聚合主题只推聚合值；事件主题面向已鉴权管理端


class HeartbeatCreate(BaseModel):
    """心跳请求体。"""

    device_id: str | None = Field(default=None, max_length=64)
    app_version: str | None = Field(default=None, max_length=32)


class HeartbeatView(BaseModel):
    """心跳响应视图。"""

    online: bool
    server_time: datetime


@router.post("/presence/heartbeat", response_model=Envelope[HeartbeatView], summary="心跳上报")
async def heartbeat(
    payload: HeartbeatCreate,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    broker: Annotated[EventBroker, Depends(get_event_broker)],
) -> Envelope[HeartbeatView]:
    """客户端每 30s 一次心跳（幂等）。

    副作用：① 更新在线会话；② 新会话写 user.online 事件（同事务投影）。
    完成后推送聚合与事件主题；分钟桶经进程内聚合器异步落库（不阻塞心跳）。
    """
    now = utc_now()
    event = Heartbeat(
        user_id=principal.subject,
        device_id=payload.device_id,
        client_ip=_client_ip(request),
        occurred_at=now,
    )
    async with UnitOfWork(session_factory) as uow:
        service = PresenceService(
            SQLAlchemyPresenceRepository(uow.session),
            request.app.state.settings.presence_online_window_seconds,
        )
        result = await service.heartbeat(event)
        if result.session_created:
            await SQLAlchemyDashboardEventRepository(uow.session).append(
                occurred_at=now,
                type="user.online",
                severity="info",
                actor_id=principal.subject,
                payload={"device_id": payload.device_id},
            )
    stats = result.stats
    # 分钟桶：进程内聚合 + 后台周期落库（见 infra/presence_aggregator），
    # 心跳事务不再直写 presence_minute_agg（消除单行漏斗）。
    await request.app.state.minute_aggregator.record(_truncate_minute(now), stats.current_online)
    await broker.publish(
        PRESENCE_TOPIC,
        {
            "current_online": stats.current_online,
            "window_seconds": stats.window_seconds,
            "server_time": stats.server_time.isoformat(),
        },
    )
    if result.session_created:
        await broker.publish(
            EVENTS_TOPIC,
            {
                "type": "user.online",
                "severity": "info",
                "actor_id": principal.subject,
                "occurred_at": now.isoformat(),
            },
        )
    return ok(
        HeartbeatView(online=True, server_time=now),
        request_id_var.get(),
    )


def _truncate_minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None