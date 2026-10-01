"""账号管理接口（SP2-3）：资料 / 改密 / 设备登记 / 注销（全部需登录态）。"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_event_broker, get_session_factory, require_principal
from app.core.clock import utc_now
from app.core.config import Settings
from app.core.envelope import Envelope, ok
from app.core.errors import INVALID_CREDENTIALS, NOT_FOUND, AppError
from app.core.logging import request_id_var
from app.core.topics import EVENTS_TOPIC
from app.domain.account.service import DeactivateService, PasswordPolicy
from app.infra.auth import Principal
from app.infra.events import EventBroker
from app.repository.account import SQLAlchemyAccountRepository, SQLAlchemyDeviceRepository
from app.repository.auth import SQLAlchemyRefreshTokenRepository
from app.repository.metrics import SQLAlchemyUsersTrendRepository
from app.repository.uow import UnitOfWork

router = APIRouter(prefix="/account", tags=["account"])


class ProfileView(BaseModel):
    """账号资料视图。"""

    user_id: str
    email: str | None
    phone: str | None
    role: str
    status: str
    created_at: datetime


class PasswordChange(BaseModel):
    """改密请求体（需验证旧密码）。"""

    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class DeviceView(BaseModel):
    """设备登记视图。"""

    id: str
    platform: str | None
    last_seen_at: datetime
    first_gift_used: bool


class DevicesView(BaseModel):
    """设备列表视图。"""

    items: list[DeviceView]


class DeactivateRequest(BaseModel):
    """注销请求体（refresh_token 可选，携带时同步吊销）。"""

    refresh_token: str | None = Field(default=None, max_length=128)


class DeactivateView(BaseModel):
    """注销结果视图。"""

    deactivated: bool
    revoked: int


@router.get("/profile", response_model=Envelope[ProfileView], summary="账号资料")
async def profile(
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[ProfileView]:
    """当前登录主体资料。"""
    async with UnitOfWork(session_factory) as uow:
        record = await SQLAlchemyAccountRepository(uow.session).get(principal.subject)
    if record is None:
        raise AppError(NOT_FOUND)
    return ok(
        ProfileView(
            user_id=record.id,
            email=record.email,
            phone=record.phone,
            role=record.role,
            status=record.status,
            created_at=record.created_at,
        ),
        request_id_var.get(),
    )


@router.post("/password", response_model=Envelope[ProfileView], summary="修改密码（吊销全部会话）")
async def change_password(
    payload: PasswordChange,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    broker: Annotated[EventBroker, Depends(get_event_broker)],
) -> Envelope[ProfileView]:
    """验证旧密码后更新；随后吊销该用户全部刷新令牌（安全惯例）。"""
    settings: Settings = request.app.state.settings
    policy = PasswordPolicy(settings.account_password_min_length)
    now = utc_now()
    async with UnitOfWork(session_factory) as uow:
        accounts = SQLAlchemyAccountRepository(uow.session)
        record = await accounts.get(principal.subject)
        if record is None:
            raise AppError(NOT_FOUND)
        hasher = request.app.state.password_hasher
        if not hasher.verify(payload.old_password, record.password_hash):
            raise AppError(INVALID_CREDENTIALS)
        policy.validate(payload.new_password)
        await accounts.update_password(
            principal.subject, hasher.hash(payload.new_password)
        )
        revoked = await SQLAlchemyRefreshTokenRepository(uow.session).revoke_user(
            principal.subject, now
        )
        view = ProfileView(
            user_id=principal.subject,
            email=record.email,
            phone=record.phone,
            role=record.role,
            status=record.status,
            created_at=record.created_at,
        )
    await broker.publish(
        EVENTS_TOPIC,
        {
            "type": "auth.password_change",
            "severity": "warning",
            "actor_id": principal.subject,
            "occurred_at": now.isoformat(),
            "payload": {"revoked": revoked},
        },
    )
    return ok(view, request_id_var.get())


@router.get("/devices", response_model=Envelope[DevicesView], summary="设备登记列表")
async def devices(
    principal: Annotated[Principal, Depends(require_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[DevicesView]:
    """当前主体的设备登记（按最近活跃倒序）。"""
    async with UnitOfWork(session_factory) as uow:
        cards = await SQLAlchemyDeviceRepository(uow.session).list_by_user(
            principal.subject
        )
    return ok(
        DevicesView(
            items=[
                DeviceView(
                    id=card.id,
                    platform=card.platform,
                    last_seen_at=card.last_seen_at,
                    first_gift_used=card.first_gift_used,
                )
                for card in cards
            ]
        ),
        request_id_var.get(),
    )


@router.delete("", response_model=Envelope[DeactivateView], summary="注销账号（匿名化 + 法务保留）")
async def deactivate(
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    broker: Annotated[EventBroker, Depends(get_event_broker)],
    payload: Annotated[DeactivateRequest | None, Body()] = None,
) -> Envelope[DeactivateView]:
    """注销：软删除 + 登录标识/凭据匿名化（流水保留可审计）；吊销全部会话。"""
    now = utc_now()
    async with UnitOfWork(session_factory) as uow:
        changed = await DeactivateService(
            SQLAlchemyAccountRepository(uow.session)
        ).deactivate(principal.subject, now)
        revoked = await SQLAlchemyRefreshTokenRepository(uow.session).revoke_user(
            principal.subject, now
        )
        if changed:
            await SQLAlchemyUsersTrendRepository(uow.session).apply_daily_delta(
                stat_date=now.date(), total_delta=-1
            )
    if changed:
        await broker.publish(
            EVENTS_TOPIC,
            {
                "type": "account.deactivate",
                "severity": "warning",
                "actor_id": principal.subject,
                "occurred_at": now.isoformat(),
            },
        )
    return ok(DeactivateView(deactivated=changed, revoked=revoked), request_id_var.get())