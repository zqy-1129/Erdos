"""Alembic 迁移环境（SQLite/PostgreSQL 双驱动，异步执行）。

连接串解析顺序：ini 显式 sqlalchemy.url（测试/CI 注入）> ERDOS_DATABASE_URL 环境变量。
"""

import asyncio
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import Settings
from app.repository.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    return url or Settings().database_url


def _section() -> dict[str, Any]:
    section = config.get_section(config.config_ini_section, {}) or {}
    return {**section, "sqlalchemy.url": _database_url()}


def run_migrations_offline() -> None:
    """离线模式：生成 SQL 脚本（不连库）。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    connectable = async_engine_from_config(_section(), prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """在线模式：执行迁移（异步引擎双驱动）。"""
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()