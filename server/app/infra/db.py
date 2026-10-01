"""数据库引擎与会话工厂（SQLite/PostgreSQL 双驱动）。

驱动选择完全由连接串 scheme 决定：业务与仓储代码不感知具体数据库，
满足研发手册「SQLite→PostgreSQL 切换业务零改动」红线。
"""

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
    PostgreSQL（asyncpg）走默认连接池并开启连接预检。
    """
    kwargs: dict[str, Any] = {"echo": echo}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_pre_ping"] = True
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