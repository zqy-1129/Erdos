"""看板管理端接口（FR-2/FR-5/M1/M2）：统计、趋势、事件查询与 SSE 实时推送。

访问控制：全部接口强制凭证 + dashboard_admin_roles 角色（FR-6）。
实时通道：SSE，事件 id=version 作为客户端游标；断连重连携带 Last-Event-ID
即可从缓冲追赶（FR-5）。
"""

import json
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_event_broker,
    get_session,
    require_dashboard_admin,
)
from app.core.clock import utc_now
from app.core.config import Settings
from app.core.envelope import Envelope, ok
from app.core.errors import BAD_REQUEST, AppError
from app.core.logging import request_id_var
from app.core.topics import EVENTS_TOPIC, MONITORING_TOPIC, PRESENCE_TOPIC
from app.domain.events.ports import EventData
from app.domain.metrics.service import TrendService
from app.domain.presence.service import PresenceService
from app.infra.auth import Principal
from app.infra.events import EventBroker
from app.repository.events import SQLAlchemyDashboardEventRepository
from app.repository.metrics import SQLAlchemyOnlineTrendRepository, SQLAlchemyUsersTrendRepository
from app.repository.presence import SQLAlchemyPresenceRepository

router = APIRouter(prefix="/admin/dashboard", tags=["dashboard"])

STREAM_TOPICS = {PRESENCE_TOPIC, EVENTS_TOPIC, MONITORING_TOPIC}


class OnlineView(BaseModel):
    """在线统计视图。"""

    current_online: int
    window_seconds: float
    server_time: datetime


class UsersTotalRow(BaseModel):
    """用户总量日快照行。"""

    stat_date: date
    total_users: int
    new_users: int
    active_users: int


class UsersTotalView(BaseModel):
    """用户总量序列视图。"""

    series: list[UsersTotalRow]


class TrendPointView(BaseModel):
    """趋势采样点视图。"""

    ts: datetime
    value: int


class OnlineTrendView(BaseModel):
    """在线趋势序列视图。"""

    granularity: str
    start: datetime
    end: datetime
    points: list[TrendPointView]


class EventRow(BaseModel):
    """看板事件行视图。"""

    id: int
    occurred_at: datetime
    type: str
    severity: str
    actor_id: str | None
    payload: dict[str, Any] | None


class EventsView(BaseModel):
    """事件查询结果视图。"""

    items: list[EventRow]
    total: int


@router.get("/users/online", response_model=Envelope[OnlineView], summary="当前在线统计")
async def users_online(
    request: Request,
    admin: Annotated[Principal, Depends(require_dashboard_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Envelope[OnlineView]:
    """当前在线用户数（TTL 滑动窗口判定，口径同 FR-1）。"""
    settings: Settings = request.app.state.settings
    service = PresenceService(
        SQLAlchemyPresenceRepository(session), settings.presence_online_window_seconds
    )
    stats = await service.stats(now=utc_now())
    return ok(
        OnlineView(
            current_online=stats.current_online,
            window_seconds=stats.window_seconds,
            server_time=stats.server_time,
        ),
        request_id_var.get(),
    )


@router.get("/users/total", response_model=Envelope[UsersTotalView], summary="用户总量统计")
async def users_total(
    admin: Annotated[Principal, Depends(require_dashboard_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Envelope[UsersTotalView]:
    """用户总量/新增/活跃日序列（快照由账号域事件驱动写入，SP2-3 接入）。"""
    rows = await SQLAlchemyUsersTrendRepository(session).list_all()
    series = [
        UsersTotalRow(
            stat_date=row.stat_date,
            total_users=row.total_users,
            new_users=row.new_users,
            active_users=row.active_users,
        )
        for row in rows
    ]
    return ok(UsersTotalView(series=series), request_id_var.get())


@router.get("/users/trend", response_model=Envelope[UsersTotalView], summary="用户总量趋势（日级）")
async def users_trend(
    admin: Annotated[Principal, Depends(require_dashboard_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
) -> Envelope[UsersTotalView]:
    """用户总量/新增/活跃日序列（支持时间范围选择）。"""
    end = to_date or date.today()
    start = from_date or (end - timedelta(days=30))
    if start > end:
        raise AppError(BAD_REQUEST, detail="from 不得晚于 to")
    points = await SQLAlchemyUsersTrendRepository(session).list_between(start, end)
    return ok(
        UsersTotalView(
            series=[
                UsersTotalRow(
                    stat_date=p.stat_date,
                    total_users=p.total_users,
                    new_users=p.new_users,
                    active_users=p.active_users,
                )
                for p in points
            ]
        ),
        request_id_var.get(),
    )


@router.get("/online/trend", response_model=Envelope[OnlineTrendView], summary="在线趋势（分钟级）")
async def online_trend(
    admin: Annotated[Principal, Depends(require_dashboard_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    granularity: str = Query("5m"),
    from_ts: Annotated[datetime | None, Query(alias="from")] = None,
    to_ts: Annotated[datetime | None, Query(alias="to")] = None,
) -> Envelope[OnlineTrendView]:
    """在线数趋势序列：1m/5m/1h 粒度下采样（桶内 max=并发峰值口径）。"""
    if granularity not in TrendService.GRANULARITIES:
        raise AppError(BAD_REQUEST, detail="granularity 仅支持 1m/5m/1h")
    end = to_ts or utc_now()
    start = from_ts or (end - timedelta(hours=6))
    if start > end:
        raise AppError(BAD_REQUEST, detail="from 不得晚于 to")
    raw = await SQLAlchemyOnlineTrendRepository(session).list_between(start, end)
    series = TrendService().build(raw, granularity, start, end)
    return ok(
        OnlineTrendView(
            granularity=series.granularity,
            start=series.start,
            end=series.end,
            points=[TrendPointView(ts=p.ts, value=p.value) for p in series.points],
        ),
        request_id_var.get(),
    )


@router.get("/events", response_model=Envelope[EventsView], summary="看板事件查询（时间关联）")
async def dashboard_events(
    admin: Annotated[Principal, Depends(require_dashboard_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    types: str | None = Query(None, description="类型过滤，逗号分隔（精确匹配）"),
    from_ts: Annotated[datetime | None, Query(alias="from")] = None,
    to_ts: Annotated[datetime | None, Query(alias="to")] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Envelope[EventsView]:
    """查询看板事件（与走势图同一时间轴）：按时间倒序，支持类型筛选与分页。"""
    type_set = {t.strip() for t in types.split(",") if t.strip()} if types else None
    items, total = await SQLAlchemyDashboardEventRepository(session).query(
        types=type_set,
        start=from_ts,
        end=to_ts,
        limit=limit,
        offset=offset,
    )
    return ok(
        EventsView(
            items=[_event_row(e) for e in items],
            total=total,
        ),
        request_id_var.get(),
    )


def _event_row(event: EventData) -> EventRow:
    return EventRow(
        id=event.id,
        occurred_at=event.occurred_at,
        type=event.type,
        severity=event.severity,
        actor_id=event.actor_id,
        payload=event.payload,
    )


@router.get("/stream", summary="实时指标流（SSE）")
async def dashboard_stream(
    request: Request,
    admin: Annotated[Principal, Depends(require_dashboard_admin)],
    broker: Annotated[EventBroker, Depends(get_event_broker)],
) -> StreamingResponse:
    """SSE 聚合指标流；Last-Event-ID（或 ?since=）作为游标断点追赶。"""
    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        _event_stream(broker, _parse_since(request)),
        media_type="text/event-stream",
        headers=headers,
    )


def _parse_since(request: Request) -> int:
    last_event_id = request.headers.get("Last-Event-ID", "")
    query_since = request.query_params.get("since", "")
    for raw in (last_event_id, query_since):
        if raw.isdigit():
            return int(raw)
    return 0


async def _event_stream(broker: EventBroker, since: int) -> AsyncIterator[str]:
    """先补发缓冲积压，再实时推送；连接断开自动退订。

    跨服务重启空窗协商：若客户端游标已失效（版本回卷或积压溢出），
    先下发 ``stream.reset`` 复位事件，客户端据此回退到全量快照拉取，
    避免因版本号重置而永久静默。
    """
    if broker.reset_needed(since):
        payload = json.dumps(
            {"reason": "cursor_stale", "since": since, "latest": broker.latest_version},
            ensure_ascii=False,
        )
        yield (
            f"id: {broker.latest_version}\n"
            f"event: stream.reset\n"
            f"data: {payload}\n\n"
        )
    async for envelope in broker.subscribe(STREAM_TOPICS, since=since):
        yield (
            f"id: {envelope.version}\n"
            f"event: {envelope.topic}\n"
            f"data: {json.dumps(dict(envelope.payload), ensure_ascii=False)}\n\n"
        )