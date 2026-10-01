"""监测采样器（后台循环）：快照 -> SSE 推送 -> 分钟落库 -> 告警投影。

循环编排（单实例进程内）：
1. 每 monitoring_interval_seconds 采样一次（采集器窗口 + 系统资源 + 连接池）；
2. 快照发布 monitoring.snapshot 主题（看板实时 KPI/趋势）；
3. 告警评估：状态转换投影为 monitor.alert 事件（落库 dashboard_events + EVENTS_TOPIC），
   分钟切换时把快照 upsert 进 monitoring_minute_snapshots 并按保留期裁剪。
"""

import asyncio
import logging
from datetime import datetime, timedelta

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.clock import utc_now
from app.core.config import Settings
from app.core.topics import EVENTS_TOPIC, MONITORING_TOPIC
from app.domain.monitoring.ports import (
    AlertThresholds,
    AlertTransition,
    MonitoringSample,
    display_values,
)
from app.domain.monitoring.service import AlertEvaluator
from app.infra.events import EventBroker
from app.infra.monitoring import MonitoringCollector
from app.repository.events import SQLAlchemyDashboardEventRepository
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
    evaluator = AlertEvaluator(thresholds)
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
    state.monitoring_alerts = _active_alert_view(
        evaluator, AlertThresholds.from_settings(settings)
    )
    minute = _minute_of(now)
    should_flush = last_minute is None or minute != last_minute
    if should_flush or transitions:
        await _persist(
            session_factory=state.session_factory,
            minute=minute,
            sample=sample,
            transitions=transitions,
            retention_days=settings.monitoring_retention_days,
            prune=should_flush,
        )
    for transition in transitions:
        await broker.publish(
            EVENTS_TOPIC,
            {
                "type": "monitor.alert",
                "severity": "warning" if transition.state == "triggered" else "info",
                "actor_id": "system",
                "occurred_at": transition.occurred_at.isoformat(),
                "payload": {
                    "metric": transition.metric,
                    "state": transition.state,
                    "value": transition.value,
                    "threshold": transition.threshold,
                    "message": transition.message,
                },
            },
        )
    return minute


def _active_alert_view(evaluator: AlertEvaluator, thresholds: AlertThresholds) -> dict[str, str]:
    """活跃告警视图（metric -> 文案），供 overview 即时读取。"""
    return {
        metric: f"超过阈值 {thresholds.value_of(metric)}"
        for metric in evaluator.active
    }


async def _persist(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    minute: datetime,
    sample: MonitoringSample,
    transitions: list[AlertTransition],
    retention_days: int,
    prune: bool,
) -> None:
    """快照与告警事件同事务落库（投影与事实一致）；裁剪随分钟切换顺带执行。"""
    async with UnitOfWork(session_factory) as uow:
        await SQLAlchemyMonitoringTrendRepository(uow.session).upsert_minute(minute, sample)
        if prune:
            await SQLAlchemyMonitoringTrendRepository(uow.session).prune_before(
                minute - timedelta(days=retention_days)
            )
        events = SQLAlchemyDashboardEventRepository(uow.session)
        for transition in transitions:
            await events.append(
                occurred_at=transition.occurred_at,
                type="monitor.alert",
                severity="warning" if transition.state == "triggered" else "info",
                actor_id="system",
                payload={
                    "metric": transition.metric,
                    "state": transition.state,
                    "value": transition.value,
                    "threshold": transition.threshold,
                    "message": transition.message,
                },
                dedup_key=f"monitor:{transition.metric}:{transition.state}:"
                f"{minute.isoformat()}",
            )


def _minute_of(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)