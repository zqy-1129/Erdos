"""FastAPI 依赖注入：会话、主体身份、配置等请求级依赖。"""

from collections.abc import AsyncIterator, Callable

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import PERMISSION_DENIED, UNAUTHENTICATED, AppError
from app.infra.auth import Principal
from app.infra.events import EventBroker


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """请求级数据库会话（只读依赖；写事务请使用 UnitOfWork）。"""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        yield session


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """会话工厂依赖（供 UnitOfWork 使用）。"""
    return request.app.state.session_factory


def get_settings(request: Request) -> Settings:
    """应用配置依赖。"""
    return request.app.state.settings


def get_principal(request: Request) -> Principal | None:
    """当前主体（网关鉴权透传的结果；未携带令牌时为 None）。"""
    return getattr(request.state, "principal", None)


def require_principal(request: Request) -> Principal:
    """当前主体（强制）：无凭证时抛 40101。"""
    principal = get_principal(request)
    if principal is None:
        raise AppError(UNAUTHENTICATED)
    return principal


def require_dashboard_admin(request: Request) -> Principal:
    """看板管理端守卫：凭证 + 角色命中 dashboard_admin_roles，否则 40101/40301。"""
    principal = require_principal(request)
    settings: Settings = request.app.state.settings
    if not set(settings.admin_roles()) & set(principal.roles):
        raise AppError(
            PERMISSION_DENIED,
            detail=f"需要角色：{'/'.join(settings.admin_roles())}",
        )
    return principal


def require_roles(*roles: str) -> Callable[[Request], Principal]:
    """角色守卫工厂（SP2-2 RBAC）：凭证 + 至少命中一个声明角色，否则 40101/40301。"""

    def checker(request: Request) -> Principal:
        principal = require_principal(request)
        if not set(roles) & set(principal.roles):
            raise AppError(PERMISSION_DENIED, detail=f"需要角色：{'/'.join(roles)}")
        return principal

    return checker


def get_event_broker(request: Request) -> EventBroker:
    """事件总线依赖（看板实时推送）。"""
    return request.app.state.event_broker