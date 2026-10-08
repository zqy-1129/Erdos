"""调度器后台循环：把《服务端架构》"调度器"章节的周期承诺真正接上触发器。

为什么存在：月赠 / 到期冻结 / 每日对账三个任务此前只有 admin HTTP 端点，没有任何自动触发——
也就是"每月 1 日 00:00（东八区）发放订阅月赠"这条承诺在无人值守时不会发生，用户订了订阅却
拿不到月赠。批次键幂等（scheduler_runs 唯一约束）本来就为"可能被重复触发"而设计，缺的只是触发。

周期口径取自设计文档，做成模块常量而非配置项：它们是产品承诺，不该按部署随意改。
时区用固定偏移（东八区无夏令时），避免为三处判断引入 tz 数据依赖。

多实例安全：进程内的"本轮已触发"标记只是省掉重复尝试；真正的去重在数据库批次键上，
所以两个实例同一时刻各跑一次也只会发放一次。
"""

import asyncio
import logging
from datetime import datetime, timedelta

from fastapi import FastAPI

from app.core.clock import utc_now
from app.core.config import Settings
from app.infra import scheduler_tasks

logger = logging.getLogger("erdos.scheduler")

# 设计文档《异步与中间件层·调度器》的周期承诺（东八区本地时间）
MONTHLY_GRANT_DAY = 1
MONTHLY_GRANT_LOCAL_HOUR = 0  # 每月 1 日 00:00
EXPIRE_LOCAL_HOUR = 2  # 每日 02:00 到期冻结
RECONCILE_LOCAL_HOUR = 3  # 每日凌晨 03:00 对账


def due_tasks(
    now_utc: datetime, settings: Settings, fired: set[tuple[str, str]]
) -> list[tuple[str, tuple[str, str]]]:
    """返回本 tick 应触发的 (任务名, 周期标记) 列表，并预占 fired（同周期同进程只尝试一次）。

    调用失败时循环会归还标记，下个 tick 重试——发放类任务不能因为一次瞬时故障就整月不再尝试。
    """
    local = now_utc + timedelta(hours=settings.scheduler_tz_offset_hours)
    day_key = local.date().isoformat()
    month_key = f"{local.year}-{local.month:02d}"

    candidates = (
        (
            "monthly_grant",
            ("monthly", month_key),
            local.day == MONTHLY_GRANT_DAY
            and local.hour == MONTHLY_GRANT_LOCAL_HOUR,
        ),
        ("expire_subscriptions", ("expire", day_key), local.hour == EXPIRE_LOCAL_HOUR),
        ("reconcile", ("reconcile", day_key), local.hour == RECONCILE_LOCAL_HOUR),
    )
    due: list[tuple[str, tuple[str, str]]] = []
    for name, marker, hour_hit in candidates:
        if hour_hit and marker not in fired:
            fired.add(marker)
            due.append((name, marker))
    return due


async def _dispatch(app: FastAPI, name: str, now: datetime) -> str:
    state = app.state
    settings: Settings = state.settings
    if name == "monthly_grant":
        return await scheduler_tasks.run_monthly_grant_task(
            state.session_factory, settings, now
        )
    if name == "expire_subscriptions":
        return await scheduler_tasks.run_expire_subscriptions_task(
            state.session_factory, settings, now
        )
    result = await scheduler_tasks.run_reconcile_task(
        state.session_factory, settings, state.alert_outlet, now
    )
    return f"diff:{len(result.differences)}"


async def run_scheduler_loop(app: FastAPI) -> None:
    """调度主循环：按 tick 轮询到点任务；ERDOS_SCHEDULER_ENABLED=false 时整条链路关闭。"""
    settings: Settings = app.state.settings
    if not settings.scheduler_enabled:
        logger.info("调度后台循环已关闭（ERDOS_SCHEDULER_ENABLED=false），三大任务仅剩管理端触发")
        return
    fired: set[tuple[str, str]] = set()
    while True:
        now = utc_now()
        for name, marker in due_tasks(now, settings, fired):
            try:
                result = await _dispatch(app, name, now)
                logger.info("调度任务完成：%s -> %s", name, result)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 归还周期标记：发放类任务不能因一次瞬时故障就整月不再尝试。
                # 注意 scheduler_runs 的 claim 是一次性的（撞键即 already_ran），所以任务体
                # 已写入 running 行之后的失败不会重跑；真正的重复发放防护在积分流水 exec_id 上。
                fired.discard(marker)
                logger.exception("调度任务失败：%s", name)
        await asyncio.sleep(settings.scheduler_tick_seconds)
