"""遥测保留期（SP4-1 的后半段）：《数据模型设计》"原始 90 天"必须真有人执行。

发现形状：`SQLAlchemyTelemetryRepository.purge_expired` 有实现、有单测，但**生产零调用方**
——摄入接口只写不删，所以这张表只增不减，而文档口径把它当成已实行的保留周期。
接线落在每日对账任务上（与 scheduler_runs 批次账本同一个顺带裁剪点），不另起一个任务。

钉住三件事：
① 每日对账真的会删过期事件（不只是"函数能跑"）；
② 保留天数取自配置项——写死 90 的实现会让 settings 上的同名配置形同虚设；
③ 仓储不许自带默认保留期，否则配置与实现各有一份数字。
"""

import inspect
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.config import Settings
from app.repository.models import TelemetryEventRecord
from app.repository.telemetry import SQLAlchemyTelemetryRepository
from app.repository.uow import UnitOfWork


class _NoopOutlet:
    def __init__(self) -> None:
        self.emitted: list[Any] = []

    async def emit(self, alert, now, dedupe=True) -> bool:  # noqa: ANN001
        self.emitted.append(alert)
        return True


async def _insert_event(session_factory, event_ts: datetime, name: str) -> None:
    async with UnitOfWork(session_factory) as uow:
        uow.session.add(
            TelemetryEventRecord(
                event_name=name,
                distinct_id="d1",
                props={},
                app_version="1.0",
                os="windows",
                channel="stable",
                event_ts=event_ts,
            )
        )


async def _names(session_factory) -> list[str]:
    async with UnitOfWork(session_factory) as uow:
        rows = (await uow.session.execute(select(TelemetryEventRecord))).scalars().all()
    return sorted(row.event_name for row in rows)


async def test_daily_reconcile_purges_expired_events(session_factory, settings) -> None:
    """过期事件在对账任务里被清掉，保留期内的不动。"""
    from app.infra import scheduler_tasks

    now = datetime.now(UTC)
    await _insert_event(session_factory, now - timedelta(days=120), "page_view")
    await _insert_event(session_factory, now - timedelta(days=1), "stage_start")

    await scheduler_tasks.run_reconcile_task(session_factory, settings, _NoopOutlet(), now)

    assert await _names(session_factory) == ["stage_start"], "90 天前的事件应被清理"


async def test_retention_days_come_from_settings(session_factory) -> None:
    """保留天数是配置项而不是实现里写死的 90：配 7 天就该清掉 30 天前的行。"""
    from app.infra import scheduler_tasks

    tuned = Settings(env="test", telemetry_retention_days=7)
    now = datetime.now(UTC)
    await _insert_event(session_factory, now - timedelta(days=30), "app_error")

    await scheduler_tasks.run_reconcile_task(
        session_factory, tuned, _NoopOutlet(), now
    )

    assert await _names(session_factory) == [], "配置改成 7 天，30 天前的行必须消失"


def test_repository_takes_no_default_retention() -> None:
    """仓储不许自带默认保留期：数字只能有一个家（Settings）。"""
    parameter = inspect.signature(SQLAlchemyTelemetryRepository.__init__).parameters[
        "retention_days"
    ]
    assert parameter.default is inspect.Parameter.empty, (
        "给了默认值就会和 telemetry_retention_days 各写一遍"
    )
