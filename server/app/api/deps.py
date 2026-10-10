"""FastAPI 依赖注入：会话、主体身份、配置等请求级依赖，以及关键操作审计埋点。"""

from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import utc_now
from app.core.config import Settings
from app.core.errors import PERMISSION_DENIED, UNAUTHENTICATED, AppError
from app.infra.audit_recorder import AuditRecorder
from app.infra.auth import Principal
from app.infra.events import EventBroker


def client_ip(request: Request) -> str | None:
    """请求来源 IP（审计与看板事件用；X-Forwarded-For 取第一段）。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    client = request.scope.get("client")
    return client[0] if client else None


async def record_audit_in_tx(
    request: Request,
    session: AsyncSession,
    *,
    action: str,
    actor_id: str,
    resource_type: str,
    actor_type: str = "user",
    resource_id: str | None = None,
    detail: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    """随业务事务写审计：只用于**无幂等键**的高频事件（撞唯一约束会污染业务事务的那类不走这里）。

    代价对比见 infra/audit_recorder.AuditRecorder.record_in_session 的说明。
    """
    await AuditRecorder.record_in_session(
        session,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail=detail,
        client_ip=client_ip(request),
        request_key=None,
        now=now or utc_now(),
    )


async def record_audit(
    request: Request,
    *,
    action: str,
    actor_id: str,
    resource_type: str,
    actor_type: str = "user",
    resource_id: str | None = None,
    detail: dict[str, Any] | None = None,
    request_key: str | None = None,
    now: datetime | None = None,
) -> bool:
    """关键操作审计埋点（独立事务，业务提交之后调用）。

    为什么不让业务事务一起写：审计唯一键撞车会污染业务事务（IntegrityError 之后必须回滚，
    业务跟着丢），而失败路径（登录被拒）的业务事务本就回滚，回滚里带不走审计。
    写失败会经 AlertOutlet 发 P2 告警——"缺留痕"这件事本身不得静默。
    """
    at = now or utc_now()
    return await request.app.state.audit_recorder.record_or_alert(
        request.app.state.alert_outlet,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail=detail,
        client_ip=client_ip(request),
        request_key=request_key,
        now=at,
    )


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