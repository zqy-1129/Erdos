"""pytest 公共夹具：应用、HTTP 客户端、独立数据库等。"""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.infra.auth import Principal
from app.infra.db import create_engine, create_session_factory
from app.main import create_app
from app.repository.models import Base


class StaticIntrospector:
    """测试用静态鉴权器：subject=令牌原文，可注入固定角色（模拟 SP2-2 RBAC 角色态）。"""

    def __init__(self, roles: tuple[str, ...] = ()) -> None:
        self._roles = roles

    async def introspect(self, token: str) -> Principal:
        return Principal(subject=token, roles=self._roles, verified=False)


@pytest.fixture
def settings(tmp_path) -> Settings:
    """测试配置：临时 SQLite 文件库 + 宽松限流（限流专项测试自行收紧）。"""
    return Settings(
        env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        rate_limit_requests=1000,
        audit_rate_limit_requests=1000,
        database_echo=False,
        payment_callback_secret="test-callback-secret",
    )


@pytest.fixture
async def app(settings: Settings):
    """已建表的 FastAPI 应用（独立引擎，测试结束自动释放）。"""
    application = create_app(settings)
    engine = application.state.engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield application
    await engine.dispose()


@pytest.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    """走 ASGI 直连的异步 HTTP 客户端（不启真实网络端口）。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture
async def admin_app(settings: Settings):
    """角色为 (admin, operator) 的应用：看板管理端接口测试。"""
    application = create_app(settings, introspector=StaticIntrospector(("admin", "operator")))
    engine = application.state.engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield application
    await engine.dispose()


@pytest.fixture
async def admin_client(admin_app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=admin_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture
async def session_factory(settings: Settings):
    """独立会话工厂（仓储/事务专项测试；与 app 夹具的引擎互斥使用）。"""
    engine = create_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    yield factory
    await engine.dispose()