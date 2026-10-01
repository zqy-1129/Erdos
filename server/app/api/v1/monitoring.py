"""服务端运行监测接口（看板运维域）：实时概览、指标趋势与告警事件。

访问控制：与看板一致，强制凭证 + dashboard_admin_roles 角色（FR-6）。
口径说明：
- overview 读取采样器缓存的「最新一次采样」（不查库）；
- trend 自 monitoring_minute_snapshots 读取并按下采样口径聚合；
  计数/比率类取 avg、延迟类取 max（与 METRICS 定义一致），
  展示值做了单位换算（错误率/连接池 *100）。
"""

from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session, require_dashboard_admin
from app.core.clock import utc_now
from app.core.config import Settings
from app.core.envelope import Envelope, ok
from app.core.errors import BAD_REQUEST, AppError
from app.core.logging import request_id_var
from app.domain.monitoring.ports import (
    METRIC_BY_KEY,
    AlertThresholds,
    MonitoringSample,
    display_thresholds,
    display_values,
)
from app.domain.monitoring.service import MonitoringTrendService
from app.infra.auth import Principal
from app.repository.events import SQLAlchemyDashboardEventRepository
from app.repository.monitoring import SQLAlchemyMonitoringTrendRepository

router = APIRouter(prefix="/admin/monitoring", tags=["monitoring"])

ALERT_EVENT_TYPE = "monitor.alert"


class PathRowView(BaseModel):
    """窗口内接口调用统计行。"""

    path: str
    requests: int


class AlertItemView(BaseModel):
    """活跃告警视图（metric -> 展示文案）。"""

    metric: str
    label: str
    message: str


class MonitoringOverviewView(BaseModel):
    """监测概览：最新采样 + 阈值 + 活跃告警 + 热接口。"""

    sampled_at: datetime | None
    values: dict[str, float] | None
    thresholds: dict[str, float]
    active_alerts: list[AlertItemView]
    hot_paths: list[PathRowView]
    uptime_seconds: int
    window_seconds: float


class MonitoringTrendPointView(BaseModel):
    """单指标趋势点（展示口径）。"""

    ts: datetime
    value: float


class MonitoringTrendView(BaseModel):
    """单指标趋势序列。"""

    metric: str
    unit: str
    granularity: str
    start: datetime
    end: datetime
    points: list[MonitoringTrendPointView]


class AlertRowView(BaseModel):
    """告警事件行。"""

    id: int
    occurred_at: datetime
    severity: str
    payload: dict[str, Any] | None


class AlertsView(BaseModel):
    """告警事件查询结果。"""

    items: list[AlertRowView]
    total: int


@router.get("/overview", response_model=Envelope[MonitoringOverviewView], summary="运行监测概览")
async def monitoring_overview(
    request: Request,
    admin: Annotated[Principal, Depends(require_dashboard_admin)],
) -> Envelope[MonitoringOverviewView]:
    """最新采样快照 + 阈值 + 活跃告警 + 窗口内热接口（实时口径，不查库）。"""
    state = request.app.state
    settings: Settings = state.settings
    thresholds = AlertThresholds.from_settings(settings)
    sample: MonitoringSample | None = state.monitoring_last
    alert_map: dict[str, str] = getattr(state, "monitoring_alerts", {}) or {}
    alerts = [
        AlertItemView(
            metric=metric,
            label=METRIC_BY_KEY[metric].label if metric in METRIC_BY_KEY else metric,
            message=alert_map[metric],
        )
        for metric in sorted(alert_map)
    ]
    uptime = int((utc_now() - state.started_at).total_seconds())
    return ok(
        MonitoringOverviewView(
            sampled_at=sample.sampled_at if sample else None,
            values=display_values(sample) if sample else None,
            thresholds=display_thresholds(thresholds),
            active_alerts=alerts,
            hot_paths=[
                PathRowView(path=p.path, requests=p.requests)
                for p in (sample.paths if sample else ())
            ],
            uptime_seconds=uptime,
            window_seconds=sample.window_seconds if sample else settings.monitoring_rate_window_seconds,
        ),
        request_id_var.get(),
    )


@router.get("/trend", response_model=Envelope[MonitoringTrendView], summary="单指标趋势")
async def monitoring_trend(
    admin: Annotated[Principal, Depends(require_dashboard_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    metric: str = Query(..., description="指标键，见 METRICS 定义"),
    granularity: str = Query("5m"),
    from_ts: Annotated[datetime | None, Query(alias="from")] = None,
    to_ts: Annotated[datetime | None, Query(alias="to")] = None,
) -> Envelope[MonitoringTrendView]:
    """单指标分钟序列下采样（avg/max 口径随指标定义）。"""
    spec = METRIC_BY_KEY.get(metric)
    if spec is None:
        raise AppError(BAD_REQUEST, detail=f"metric 仅支持：{'/'.join(METRIC_BY_KEY)}")
    if granularity not in MonitoringTrendService.GRANULARITIES:
        raise AppError(BAD_REQUEST, detail="granularity 仅支持 1m/5m/1h")
    end = to_ts or utc_now()
    start = from_ts or (end - timedelta(hours=6))
    if start > end:
        raise AppError(BAD_REQUEST, detail="from 不得晚于 to")
    raw = await SQLAlchemyMonitoringTrendRepository(session).list_between(metric, start, end)
    series = MonitoringTrendService().build(
        raw, granularity, start, end, spec.agg, spec.factor
    )
    return ok(
        MonitoringTrendView(
            metric=metric,
            unit=spec.unit,
            granularity=series.granularity,
            start=series.start,
            end=series.end,
            points=[
                MonitoringTrendPointView(ts=p.ts, value=p.value) for p in series.points
            ],
        ),
        request_id_var.get(),
    )


@router.get("/alerts", response_model=Envelope[AlertsView], summary="告警事件查询")
async def monitoring_alerts(
    admin: Annotated[Principal, Depends(require_dashboard_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    from_ts: Annotated[datetime | None, Query(alias="from")] = None,
    to_ts: Annotated[datetime | None, Query(alias="to")] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Envelope[AlertsView]:
    """查询 monitor.alert 事件（按时间倒序）。"""
    items, total = await SQLAlchemyDashboardEventRepository(session).query(
        types={ALERT_EVENT_TYPE},
        start=from_ts,
        end=to_ts,
        limit=limit,
        offset=offset,
    )
    return ok(
        AlertsView(
            items=[
                AlertRowView(
                    id=e.id,
                    occurred_at=e.occurred_at,
                    severity=e.severity,
                    payload=e.payload,
                )
                for e in items
            ],
            total=total,
        ),
        request_id_var.get(),
    )


@router.get("/metrics-spec", response_model=Envelope[dict[str, Any]], summary="指标定义")
async def monitoring_metrics_spec(
    admin: Annotated[Principal, Depends(require_dashboard_admin)],
) -> Envelope[dict[str, Any]]:
    """指标定义（键/名称/单位），供看板动态渲染选择器与单位。"""
    spec = {
        m.key: {"label": m.label, "unit": m.unit}
        for m in METRIC_BY_KEY.values()
    }
    return ok(spec, request_id_var.get())