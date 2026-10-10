"""遥测摄入接口（SP4-1）：/v1/telemetry/events。

隐私红线：事件经 schema 校验 + props 白名单过滤，违禁字段拦截丢弃并告警。

匿名可达是**既有跨端契约**，不是漏洞：客户端 telemetry SDK 明确按"事件端点公开、登录时附带
Bearer"实现（client/main/telemetry/sdk.ts），而注册漏斗、首题等事件恰恰发生在登录前，只能用
设备级 distinct_id 归因。伪造风险的治理口径（批签名 / 共享上报密钥 / 采样校验）归数据平台裁决
（DEC-028），不在服务端单方面收紧——本轮曾把它改成 require_principal，因会静默打断客户端
outbox 补报而回退。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_session_factory
from app.core.envelope import Envelope, ok
from app.core.logging import request_id_var
from app.domain.telemetry.ports import TelemetryEvent
from app.domain.telemetry.service import TelemetryService
from app.repository.telemetry import SQLAlchemyTelemetryRepository
from app.repository.uow import UnitOfWork

router = APIRouter(prefix="/telemetry", tags=["telemetry"])


class TelemetryEventBody(BaseModel):
    """单条遥测事件。"""

    event_name: str = Field(min_length=1, max_length=32)
    distinct_id: str = Field(min_length=1, max_length=128)
    props: dict = {}
    app_version: str = ""
    os: str = ""
    channel: str = "stable"


class TelemetryBatchBody(BaseModel):
    """批量遥测事件（SDK 批上报）。"""

    events: list[TelemetryEventBody] = Field(min_length=1, max_length=1000)


class IngestView(BaseModel):
    """摄入结果。"""

    accepted: int
    rejected: int
    reasons: list[str]


@router.post("/events", response_model=Envelope[IngestView], summary="遥测事件批量摄入")
async def ingest_events(
    request: Request,
    payload: TelemetryBatchBody,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[IngestView]:
    """摄入遥测事件（批 1000 行）：schema 校验 + 白名单过滤 + 违禁拦截。"""
    events = [
        TelemetryEvent(
            event_name=e.event_name,
            distinct_id=e.distinct_id,
            props=e.props,
            app_version=e.app_version,
            os=e.os,
            channel=e.channel,
        )
        for e in payload.events
    ]
    async with UnitOfWork(session_factory) as uow:
        service = TelemetryService(SQLAlchemyTelemetryRepository(uow.session))
        result = await service.ingest(events)
        view = IngestView(accepted=result.accepted, rejected=result.rejected, reasons=result.reasons)
    return ok(view, request_id_var.get())
