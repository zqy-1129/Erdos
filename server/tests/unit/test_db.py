"""数据库引擎双驱动分支测试。"""

import pytest

from app.infra.db import create_engine, create_session_factory


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///./x.db",
        "postgresql+asyncpg://u:p@localhost:5432/erdos",
    ],
)
async def test_create_engine_by_scheme(url: str) -> None:
    engine = create_engine(url)
    try:
        assert engine.dialect.name in {"sqlite", "postgresql"}
        factory = create_session_factory(engine)
        assert factory is not None
    finally:
        await engine.dispose()