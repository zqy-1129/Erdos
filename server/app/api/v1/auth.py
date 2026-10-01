"""认证授权接口（SP2-2/SP2-3）：登录/刷新/注销/注册/密码重置/JWKS。

- login / refresh / logout / jwks / register / reset 为公开端点（令牌自校验 + 防爆破守卫）；
- 注册与登录成功、账号锁定投影 dashboard_events 供管理看板事件泳道联动；
- 注册赠 100 分以领域事件发布（points.registration_grant），SP2-4 积分域消费。
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_event_broker, get_session_factory
from app.core.clock import utc_now
from app.core.config import Settings
from app.core.envelope import Envelope, ok
from app.core.errors import ACCOUNT_LOCKED, CONFLICT, AppError
from app.core.logging import request_id_var
from app.core.topics import EVENTS_TOPIC, REGISTRATION_GIFT_TOPIC
from app.domain.account.ports import RegistrationRequest
from app.domain.account.service import PasswordPolicy, RegisterService
from app.domain.auth.ports import AuthIdentity, TokenManager, TokenPair
from app.domain.auth.service import AuthService
from app.infra.events import EventBroker
from app.repository.account import SQLAlchemyAccountRepository, SQLAlchemyDeviceRepository
from app.repository.auth import SQLAlchemyRefreshTokenRepository
from app.repository.events import SQLAlchemyDashboardEventRepository
from app.repository.metrics import SQLAlchemyUsersTrendRepository
from app.repository.uow import UnitOfWork

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """登录请求体。"""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    device_id: str | None = Field(default=None, max_length=64)
    fingerprint: str | None = Field(default=None, max_length=64)
    platform: str | None = Field(default=None, max_length=32)


class RegisterCreate(BaseModel):
    """注册请求体（邮箱/手机至少其一；指纹用于赠分防刷）。"""

    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    fingerprint: str = Field(min_length=8, max_length=64)
    platform: str | None = Field(default=None, max_length=32)


class ResetRequest(BaseModel):
    """重置申请（登录标识）。"""

    identifier: str = Field(min_length=3, max_length=255)


class ResetConfirm(BaseModel):
    """重置确认。"""

    reset_token: str = Field(min_length=16, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class ResetAcceptedView(BaseModel):
    """重置申请受理视图（不泄露账号存在性）。"""

    accepted: bool


class RefreshRequest(BaseModel):
    """刷新请求体（携带设备标识时可校验设备一致性）。"""

    refresh_token: str = Field(min_length=16, max_length=128)
    device_id: str | None = Field(default=None, max_length=64)


class LogoutRequest(BaseModel):
    """注销请求体。"""

    refresh_token: str = Field(min_length=16, max_length=128)


class TokenPairView(BaseModel):
    """双令牌响应视图。"""

    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int
    refresh_expires_in: int


class LogoutView(BaseModel):
    """注销结果视图（幂等：已失效令牌返回 0）。"""

    revoked: int


@router.post("/login", response_model=Envelope[TokenPairView], summary="密码登录")
async def login(
    payload: LoginRequest,
    request: Request,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    broker: Annotated[EventBroker, Depends(get_event_broker)],
) -> Envelope[TokenPairView]:
    """校验凭据并签发双令牌；失败计入防爆破，锁定写看板事件。"""
    service = _auth_service(request)
    now = utc_now()
    try:
        async with UnitOfWork(session_factory) as uow:
            pair = await service.login(
                SQLAlchemyRefreshTokenRepository(uow.session),
                username=payload.username,
                password=payload.password,
                device_id=payload.device_id,
                client_ip=_client_ip(request),
            )
            if payload.fingerprint:
                # 登录设备登记：指纹/平台落 devices（不涉赠分，first_gift_used 随指纹存在性）
                identity = request.app.state.token_manager.verify(pair.access_token)
                if identity is not None:
                    await SQLAlchemyDeviceRepository(uow.session).touch(
                        user_id=identity.subject,
                        fingerprint=payload.fingerprint,
                        platform=payload.platform,
                        now=now,
                    )
            await SQLAlchemyDashboardEventRepository(uow.session).append(
                occurred_at=now,
                type="auth.login",
                severity="info",
                actor_id=payload.username,
                payload={"device_id": payload.device_id},
            )
    except AppError as exc:
        if exc.spec.code == ACCOUNT_LOCKED.code:
            await broker.publish(
                EVENTS_TOPIC,
                {
                    "type": "auth.lockout",
                    "severity": "warning",
                    "actor_id": payload.username,
                    "occurred_at": now.isoformat(),
                    "payload": {"username": payload.username},
                },
            )
        raise
    await broker.publish(
        EVENTS_TOPIC,
        {
            "type": "auth.login",
            "severity": "info",
            "actor_id": payload.username,
            "occurred_at": now.isoformat(),
        },
    )
    return ok(_pair_view(pair), request_id_var.get())


@router.post("/register", response_model=Envelope[TokenPairView], summary="注册（赠分事件 + 自动登录）")
async def register(
    payload: RegisterCreate,
    request: Request,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    broker: Annotated[EventBroker, Depends(get_event_broker)],
) -> Envelope[TokenPairView]:
    """注册即登录。同邮箱/手机冲突 409；同指纹重复注册不再触发赠分（防刷）。

    副作用（同事务）：建号 -> 设备登记（赠分标记）-> 赠分事件投影
    （points.grant，dedup 键贴账号）-> 日快照 +1 -> 审计（account.register）。
    提交后发布注册赠分领域事件供积分域消费。
    """
    settings: Settings = request.app.state.settings
    now = utc_now()
    policy = PasswordPolicy(settings.account_password_min_length)
    gift_granted = False
    async with UnitOfWork(session_factory) as uow:
        try:
            result = await RegisterService(
                SQLAlchemyAccountRepository(uow.session),
                SQLAlchemyDeviceRepository(uow.session),
                request.app.state.password_hasher,
                policy,
            ).register(
                RegistrationRequest(
                    email=payload.email,
                    phone=payload.phone,
                    password=payload.password,
                    fingerprint=payload.fingerprint,
                    platform=payload.platform,
                    client_ip=_client_ip(request),
                ),
                now,
            )
        except IntegrityError:
            # 并发注册撞唯一约束：事务已回滚，转为业务冲突
            raise AppError(CONFLICT, detail="该邮箱或手机号已注册") from None
        gift_granted = result.gift_granted
        events = SQLAlchemyDashboardEventRepository(uow.session)
        await events.append(
            occurred_at=now,
            type="account.register",
            severity="info",
            actor_id=result.account.id,
            payload={"role": result.account.role},
        )
        if gift_granted:
            await events.append(
                occurred_at=now,
                type="points.grant",
                severity="info",
                actor_id=result.account.id,
                payload={"points": settings.account_registration_gift_points},
                dedup_key=f"gift:{result.account.id}",
            )
        await SQLAlchemyUsersTrendRepository(uow.session).apply_daily_delta(
            stat_date=now.date(), total_delta=1, new_delta=1
        )
        pair = await _auth_service(request).issue_pair(
            SQLAlchemyRefreshTokenRepository(uow.session),
            AuthIdentity(subject=result.account.id, roles=("user",)),
            device_id=None,
        )
    await broker.publish(
        EVENTS_TOPIC,
        {
            "type": "account.register",
            "severity": "info",
            "actor_id": result.account.id,
            "occurred_at": now.isoformat(),
        },
    )
    if gift_granted:
        await broker.publish(
            REGISTRATION_GIFT_TOPIC,
            {
                "user_id": result.account.id,
                "points": settings.account_registration_gift_points,
                "occurred_at": now.isoformat(),
            },
        )
    return ok(_pair_view(pair), request_id_var.get())


@router.post(
    "/password/reset/request",
    response_model=Envelope[ResetAcceptedView],
    summary="申请密码重置（60s/次，每日 10 次）",
)
async def reset_request(
    payload: ResetRequest,
    request: Request,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[ResetAcceptedView]:
    """生成一次性重置令牌并经渠道发送；无论账号是否存在均返回受理（防枚举）。"""
    async with UnitOfWork(session_factory) as uow:
        await request.app.state.reset_service.request(
            SQLAlchemyAccountRepository(uow.session), payload.identifier
        )
    return ok(ResetAcceptedView(accepted=True), request_id_var.get())


@router.post(
    "/password/reset/confirm",
    response_model=Envelope[ResetAcceptedView],
    summary="确认密码重置（吊销该用户全部刷新令牌）",
)
async def reset_confirm(
    payload: ResetConfirm,
    request: Request,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    broker: Annotated[EventBroker, Depends(get_event_broker)],
) -> Envelope[ResetAcceptedView]:
    """校验令牌并更新密码；成功后吊销全部设备会话（安全惯例）。"""
    async with UnitOfWork(session_factory) as uow:
        user_id = await request.app.state.reset_service.confirm(
            SQLAlchemyAccountRepository(uow.session),
            payload.reset_token,
            payload.new_password,
        )
        await SQLAlchemyRefreshTokenRepository(uow.session).revoke_user(user_id, utc_now())
    await broker.publish(
        EVENTS_TOPIC,
        {
            "type": "auth.password_reset",
            "severity": "warning",
            "actor_id": user_id,
            "occurred_at": utc_now().isoformat(),
        },
    )
    return ok(ResetAcceptedView(accepted=True), request_id_var.get())


@router.post("/refresh", response_model=Envelope[TokenPairView], summary="刷新轮换（旧令牌即吊销）")
async def refresh(
    payload: RefreshRequest,
    request: Request,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[TokenPairView]:
    """用刷新令牌换取新双令牌；旧刷新令牌立即失效（换代防护）。"""
    service = _auth_service(request)
    async with UnitOfWork(session_factory) as uow:
        pair = await service.refresh(
            SQLAlchemyRefreshTokenRepository(uow.session),
            refresh_token=payload.refresh_token,
            device_id=payload.device_id,
        )
    return ok(_pair_view(pair), request_id_var.get())


@router.post("/logout", response_model=Envelope[LogoutView], summary="注销（设备维度吊销）")
async def logout(
    payload: LogoutRequest,
    request: Request,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[LogoutView]:
    """吊销该令牌所属设备的全部刷新令牌（幂等）。"""
    service = _auth_service(request)
    async with UnitOfWork(session_factory) as uow:
        revoked = await service.logout(
            SQLAlchemyRefreshTokenRepository(uow.session),
            refresh_token=payload.refresh_token,
        )
    return ok(LogoutView(revoked=revoked), request_id_var.get())


@router.get("/jwks", response_model=Envelope[dict[str, Any]], summary="公钥发布（JWKS）")
async def jwks(request: Request) -> Envelope[dict[str, Any]]:
    """Ed25519 公钥集（含密钥版本 kid），供客户端/引擎离线验签。"""
    manager: TokenManager = request.app.state.token_manager
    return ok(manager.public_jwks(), request_id_var.get())


def _auth_service(request: Request) -> AuthService:
    settings: Settings = request.app.state.settings
    return AuthService(
        verifier=request.app.state.credential_verifier,
        tokens=request.app.state.token_manager,
        lockout=request.app.state.auth_lockout,
        access_ttl_seconds=settings.auth_access_ttl_seconds,
        refresh_ttl_days=settings.auth_refresh_ttl_days,
    )


def _pair_view(pair: TokenPair) -> TokenPairView:
    return TokenPairView(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        token_type=pair.token_type,
        expires_in=pair.expires_in,
        refresh_expires_in=pair.refresh_expires_in,
    )


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None