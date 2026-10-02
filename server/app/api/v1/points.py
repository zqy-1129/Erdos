"""积分域接口（SP2-4 资金域核心）：账户查询、流水账单、预扣/确认/退还。

资金域红线：reserve/confirm/refund 以 exec_id 幂等；余额不足/终态迁移返回 409；
全部接口需登录态（require_principal）。
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_session_factory, require_principal
from app.core.clock import utc_now
from app.core.config import Settings
from app.core.envelope import Envelope, ok
from app.core.logging import request_id_var
from app.domain.points.ports import AccountBalance, ReserveRequest
from app.domain.points.service import PointsService
from app.infra.auth import Principal
from app.repository.points import (
    SQLAlchemyGrantRepository,
    SQLAlchemyLedgerRepository,
    SQLAlchemyPointAccountRepository,
)
from app.repository.uow import UnitOfWork

router = APIRouter(prefix="/points", tags=["points"])


class BalanceView(BaseModel):
    """积分账户余额视图。"""

    user_id: str
    purchased_balance: int
    monthly_balance: int
    frozen: bool


class LedgerView(BaseModel):
    """积分流水视图。"""

    id: str
    exec_id: str
    delta: int
    balance_type: str
    kind: str
    status: str
    source: str
    stage: str | None
    created_at: datetime


class LedgerPageView(BaseModel):
    """流水账单分页视图。"""

    items: list[LedgerView]
    total: int


class ReserveBody(BaseModel):
    """预扣请求体。"""

    exec_id: str = Field(min_length=1, max_length=64)
    task_id: str | None = Field(default=None, max_length=36)
    stage: str = Field(min_length=1, max_length=16)
    points: int = Field(gt=0)


class GrantView(BaseModel):
    """阶段许可视图。"""

    exec_id: str
    stage: str
    points: int
    signature: str
    key_version: str
    issued_at: datetime
    expires_at: datetime
    status: str


class ReserveView(BaseModel):
    """预扣结果视图。"""

    balance: BalanceView
    grant: GrantView
    already_reserved: bool


class SettleBody(BaseModel):
    """确认/退还请求体。"""

    exec_id: str = Field(min_length=1, max_length=64)


class SettleView(BaseModel):
    """确认/退还结果视图。"""

    balance: BalanceView
    status: str


def _balance_view(balance: AccountBalance) -> BalanceView:
    return BalanceView(
        user_id=balance.user_id,
        purchased_balance=balance.purchased_balance,
        monthly_balance=balance.monthly_balance,
        frozen=balance.frozen,
    )


def _service(request: Request, session: AsyncSession) -> PointsService:
    settings: Settings = request.app.state.settings
    return PointsService(
        SQLAlchemyPointAccountRepository(session),
        SQLAlchemyLedgerRepository(session),
        SQLAlchemyGrantRepository(session),
        request.app.state.license_signer,
        settings,
    )


@router.get("/balance", response_model=Envelope[BalanceView], summary="积分账户余额")
async def balance(
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[BalanceView]:
    """当前主体积分账户余额（不存在则返回零余额账户）。"""
    now = utc_now()
    async with UnitOfWork(session_factory) as uow:
        account = await SQLAlchemyPointAccountRepository(uow.session).get_or_create(
            principal.subject, now
        )
    return ok(_balance_view(account), request_id_var.get())


@router.get("/ledger", response_model=Envelope[LedgerPageView], summary="积分流水账单")
async def ledger(
    principal: Annotated[Principal, Depends(require_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Envelope[LedgerPageView]:
    """当前主体积分流水（时间倒序，分页）。"""
    async with UnitOfWork(session_factory) as uow:
        items, total = await SQLAlchemyLedgerRepository(uow.session).list_by_user(
            principal.subject, limit, offset
        )
    return ok(
        LedgerPageView(
            items=[
                LedgerView(
                    id=it.id,
                    exec_id=it.exec_id,
                    delta=it.delta,
                    balance_type=it.balance_type,
                    kind=it.kind,
                    status=it.status,
                    source=it.source,
                    stage=it.stage,
                    created_at=it.created_at,
                )
                for it in items
            ],
            total=total,
        ),
        request_id_var.get(),
    )


@router.post("/reserve", response_model=Envelope[ReserveView], summary="预扣（阶段许可签发）")
async def reserve(
    request: Request,
    payload: ReserveBody,
    principal: Annotated[Principal, Depends(require_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[ReserveView]:
    """预扣积分并签发阶段许可（exec_id 幂等，重复预扣不重复扣减）。"""
    now = utc_now()
    async with UnitOfWork(session_factory) as uow:
        result = await _service(request, uow.session).reserve(
            ReserveRequest(
                exec_id=payload.exec_id,
                user_id=principal.subject,
                task_id=payload.task_id,
                stage=payload.stage,
                points=payload.points,
            ),
            now,
        )
        view = ReserveView(
            balance=_balance_view(result.balance),
            grant=GrantView(
                exec_id=result.grant.exec_id,
                stage=result.grant.stage,
                points=result.grant.points,
                signature=result.grant.signature,
                key_version=result.grant.key_version,
                issued_at=result.grant.issued_at,
                expires_at=result.grant.expires_at,
                status=result.grant.status,
            ),
            already_reserved=result.already_reserved,
        )
    return ok(view, request_id_var.get())


@router.post("/confirm", response_model=Envelope[SettleView], summary="确认消耗（阶段完成）")
async def confirm(
    request: Request,
    payload: SettleBody,
    principal: Annotated[Principal, Depends(require_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[SettleView]:
    """确认预扣（reserved → confirmed，终态；幂等）。"""
    now = utc_now()
    async with UnitOfWork(session_factory) as uow:
        result = await _service(request, uow.session).confirm(
            principal.subject, payload.exec_id, now
        )
        view = SettleView(
            balance=_balance_view(result.balance), status=result.ledger.status
        )
    return ok(view, request_id_var.get())


@router.post("/refund", response_model=Envelope[SettleView], summary="退还（阶段取消）")
async def refund(
    request: Request,
    payload: SettleBody,
    principal: Annotated[Principal, Depends(require_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[SettleView]:
    """退还预扣并返还余额（reserved → refunded，终态；幂等）。"""
    now = utc_now()
    async with UnitOfWork(session_factory) as uow:
        result = await _service(request, uow.session).refund(
            principal.subject, payload.exec_id, now
        )
        view = SettleView(
            balance=_balance_view(result.balance), status=result.ledger.status
        )
    return ok(view, request_id_var.get())
