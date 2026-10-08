"""监测采样器（后台循环）：快照 -> SSE 推送 -> 分钟落库 -> 告警投影。

循环编排（单实例进程内）：
1. 每 monitoring_interval_seconds 采样一次（采集器窗口 + 系统资源 + 连接池）；
2. 快照发布 monitoring.snapshot 主题（看板实时 KPI/趋势）；
3. 告警评估：状态转换经 AlertOutlet 统一扇出（P0/P1/P2 定档 -> 事件落库 + SSE + Webhook），
   分钟切换时把快照 upsert 进 monitoring_minute_snapshots 并按保留期裁剪。
"""

import asyncio
import logging
from datetime import datetime, timedelta

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.clock import utc_now
from app.core.config import Settings
from app.core.topics import MONITORING_TOPIC
from app.domain.alerts.ports import AlertOutlet, BusinessAlert
from app.domain.alerts.severity import SLO_AVAILABILITY, severity_for_metric
from app.domain.monitoring.ports import AlertThresholds, MonitoringSample, display_values
from app.domain.monitoring.service import AlertEvaluator
from app.infra.events import EventBroker
from app.infra.monitoring import MonitoringCollector
from app.repository.monitoring import SQLAlchemyMonitoringTrendRepository
from app.repository.uow import UnitOfWork

logger = logging.getLogger("erdos.monitoring")


def _pool_usage(engine: AsyncEngine) -> float:
    """连接池占用率（checkedout/size）；池不提供容量时按 0 处理。"""
    pool = engine.pool
    try:
        size = int(pool.size())  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        return 0.0
    if size <= 0:
        return 0.0
    try:
        checkedout = int(pool.checkedout())  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        checkedout = 0
    return min(checkedout / size, 1.0)


async def run_monitoring_loop(app: FastAPI) -> None:
    """监测主循环：由 lifespan 启动、取消即退出（正常退出不视为异常）。"""
    state = app.state
    settings: Settings = state.settings
    broker: EventBroker = state.event_broker
    collector = state.monitoring
    thresholds = AlertThresholds.from_settings(settings)
    evaluator = AlertEvaluator(
        thresholds,
        availability_target=SLO_AVAILABILITY,
        availability_min_requests=settings.monitoring_availability_min_requests,
    )
    last_minute: datetime | None = None

    while True:
        try:
            last_minute = await _run_once(
                app=app,
                collector=collector,
                broker=broker,
                evaluator=evaluator,
                settings=settings,
                last_minute=last_minute,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # 采样/落库失败不中断监测循环（下周期重试），但必须留痕可查
            logger.exception("监测采样周期执行失败")
        await asyncio.sleep(settings.monitoring_interval_seconds)


async def _run_once(
    *,
    app: FastAPI,
    collector: MonitoringCollector,
    broker: EventBroker,
    evaluator: AlertEvaluator,
    settings: Settings,
    last_minute: datetime | None,
) -> datetime:
    state = app.state
    now = utc_now()
    raw = collector.snapshot(settings.monitoring_rate_window_seconds)
    cpu, mem, rss = collector.system_metrics()
    sample = MonitoringSample(
        sampled_at=now,
        qps=raw.qps,
        error_rate=raw.error_rate,
        p50_ms=raw.p50_ms,
        p95_ms=raw.p95_ms,
        cpu_percent=cpu,
        memory_percent=mem,
        rss_mb=rss,
        db_query_p95_ms=raw.db_query_p95_ms,
        db_pool_usage=_pool_usage(state.engine),
        paths=raw.paths,
        window_seconds=raw.window_seconds,
    )
    state.monitoring_last = sample
    await broker.publish(
        MONITORING_TOPIC,
        {"sampled_at": now.isoformat(), "values": display_values(sample)},
    )

    transitions = evaluator.evaluate(sample)
    state.monitoring_alerts = evaluator.active_view()
    minute = _minute_of(now)
    should_flush = last_minute is None or minute != last_minute
    if should_flush or transitions:
        await _persist(
            session_factory=state.session_factory,
            minute=minute,
            sample=sample,
            retention_days=settings.monitoring_retention_days,
            prune=should_flush,
        )
    # 告警扇出统一走 AlertOutlet：档位（P0/P1/P2）、事件落库、SSE 与 Webhook 只有一处实现。
    # dedupe=False 是必须的——状态机已保证只在翻转时产出，再套静默窗口会把
    # "触发→恢复→5 分钟内再触发"的 P0 吞掉。
    outlet: AlertOutlet = state.alert_outlet
    for transition in transitions:
        await outlet.emit(
            BusinessAlert(
                key=transition.metric,
                severity=severity_for_metric(transition.metric),
                message=transition.message,
                value=transition.value,
                threshold=transition.threshold,
                state=transition.state,
            ),
            transition.occurred_at,
            dedupe=False,
        )
    return minute


async def _persist(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    minute: datetime,
    sample: MonitoringSample,
    retention_days: int,
    prune: bool,
) -> None:
    """采样快照落库（裁剪随分钟切换顺带执行）；告警事件由 AlertOutlet 统一负责。"""
    async with UnitOfWork(session_factory) as uow:
        await SQLAlchemyMonitoringTrendRepository(uow.session).upsert_minute(minute, sample)
        if prune:
            await SQLAlchemyMonitoringTrendRepository(uow.session).prune_before(
                minute - timedelta(days=retention_days)
            )


def _minute_of(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)