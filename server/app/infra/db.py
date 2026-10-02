"""数据库引擎与会话工厂（SQLite/PostgreSQL 双驱动）。

驱动选择完全由连接串 scheme 决定：业务与仓储代码不感知具体数据库，
满足研发手册「SQLite→PostgreSQL 切换业务零改动」红线。
"""

import os
import time
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.infra.monitoring import get_collector

_QUERY_START_KEY = "erdos_monitoring_query_start"


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """按连接串创建异步引擎。

    SQLite 显式关闭同线程检查（aiosqlite 由事件循环单线程驱动）；
    PostgreSQL（asyncpg）走显式配置的连接池（pool_pre_ping + 10+90）并开启连接预检。
    """
    kwargs: dict[str, Any] = {"echo": echo}
    if database_url.startswith("sqlite"):
        # check_same_thread=False：aiosqlite 由事件循环单线程驱动；
        # timeout=30：SQLite 单写者，设置 busy_timeout 让并发写等待而非立即
        # 抛 database is locked（生产切 PostgreSQL 后无此限制）。
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        # asyncpg 连接池：默认 5+10 在写密集并发下排队明显（SP2-8 CI 压测实测
        # P95 数百 ms），显式放宽至 10+90 上限 100；生产如需调整再提升为配置项。
        kwargs.update(
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=90,
            pool_timeout=30,
        )
        # 压测专用：ERDOS_DB_SYNC_COMMIT=off 关闭同步提交（连接级 server_settings）。
        # 仅用于性能基准环境（规避 CI 云盘 fsync 噪声）；生产默认为 on（持久性红线）。
        # CI perf-pg 任务显式设置该变量，其余环境不受影响。
        if os.environ.get("ERDOS_DB_SYNC_COMMIT") == "off":
            kwargs["connect_args"] = {"server_settings": {"synchronous_commit": "off"}}
    engine = create_async_engine(database_url, **kwargs)
    _register_query_hooks(engine)
    return engine


def _register_query_hooks(engine: AsyncEngine) -> None:
    """SQL 耗时采集钩子（监测看板数据源之一）。

    采集器未装配（如独立引擎的单测场景）时静默跳过，不影响数据库功能。
    """

    def before_cursor_execute(
        conn: Any, cursor: Any, statement: Any, parameters: Any, context: Any, executemany: Any
    ) -> None:
        conn.info[_QUERY_START_KEY] = time.perf_counter()

    def after_cursor_execute(
        conn: Any, cursor: Any, statement: Any, parameters: Any, context: Any, executemany: Any
    ) -> None:
        start = conn.info.pop(_QUERY_START_KEY, None)
        if start is None:
            return
        try:
            get_collector().record_query(time.perf_counter() - start)
        except RuntimeError:
            return

    event.listen(engine.sync_engine, "before_cursor_execute", before_cursor_execute)
    event.listen(engine.sync_engine, "after_cursor_execute", after_cursor_execute)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """创建异步会话工厂；每次请求获取独立会话，事务由 UnitOfWork 管理。"""
    return async_sessionmaker(engine, expire_on_commit=False)