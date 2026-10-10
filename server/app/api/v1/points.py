"""积分域接口（SP2-4 资金域核心）：账户查询、流水账单、预扣/确认/退还、离线对账、流水导出。

资金域红线：reserve/confirm/refund 以 exec_id 幂等；余额不足/终态迁移返回 409；
全部接口需登录态（require_principal）。
"""

import csv
import io
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_session_factory, record_audit, record_audit_in_tx, require_principal
from app.core.clock import utc_now
from app.core.config import Settings
from app.core.envelope import Envelope, ok
from app.core.logging import request_id_var
from app.domain.points.ports import AccountBalance, OfflineItem, ReserveRequest
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


class OfflineItemBody(BaseModel):
    """单条离线消耗上报。"""

    exec_id: str = Field(min_length=1, max_length=64)
    task_id: str | None = Field(default=None, max_length=36)
    stage: str = Field(min_length=1, max_length=16)
    points: int = Field(gt=0)


class OfflineSyncBody(BaseModel):
    """离线消耗批量对账请求体。"""

    items: list[OfflineItemBody] = Field(min_length=1, max_length=200)


class OfflineItemView(BaseModel):
    """单条对账回执。"""

    exec_id: str
    status: str
    detail: str


class OfflineSyncView(BaseModel):
    """批量对账结果视图。"""

    applied: int
    duplicate: int
    insufficient: int
    frozen: bool
    balance: BalanceView | None
    items: list[OfflineItemView]


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
        if not result.already_reserved:
            # 许可签发留痕：无幂等键、随业务同事务（提交后再写会多一次单写锁，实测 P95 +71%）
            await record_audit_in_tx(
                request,
                uow.session,
                action="points.license_issued",
                actor_id=principal.subject,
                resource_type="grant",
                resource_id=payload.exec_id,
                detail={
                    "stage": payload.stage,
                    "points": payload.points,
                    "task_id": payload.task_id,
                    "key_version": result.grant.key_version,
                },
                now=now,
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


@router.post("/offline-sync", response_model=Envelope[OfflineSyncView], summary="离线消耗批量对账")
async def offline_sync(
    request: Request,
    payload: OfflineSyncBody,
    principal: Annotated[Principal, Depends(require_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[OfflineSyncView]:
    """离线期间产生的消耗批量上报对账：逐条幂等去重、余额扣减、欠费冻结。"""
    now = utc_now()
    async with UnitOfWork(session_factory) as uow:
        result = await _service(request, uow.session).reconcile(
            principal.subject,
            [
                OfflineItem(
                    exec_id=it.exec_id,
                    task_id=it.task_id,
                    stage=it.stage,
                    points=it.points,
                )
                for it in payload.items
            ],
            now,
        )
        view = OfflineSyncView(
            applied=result.applied,
            duplicate=result.duplicate,
            insufficient=result.insufficient,
            frozen=result.frozen,
            balance=_balance_view(result.balance) if result.balance else None,
            items=[
                OfflineItemView(exec_id=it.exec_id, status=it.status, detail=it.detail)
                for it in result.items
            ],
        )
    await record_audit(
        request,
        action="points.offline_reconciled",
        actor_id=principal.subject,
        resource_type="offline_sync",
        detail={
            "submitted": len(payload.items),
            "applied": result.applied,
            "duplicate": result.duplicate,
            "insufficient": result.insufficient,
            "frozen": result.frozen,
        },
        now=now,
    )
    return ok(view, request_id_var.get())


@router.get("/ledger/export", summary="积分流水 CSV 导出")
async def ledger_export(
    principal: Annotated[Principal, Depends(require_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> StreamingResponse:
    """导出当前主体全量积分流水为 CSV（含 UTF-8 BOM，兼容 Excel）。"""
    async with UnitOfWork(session_factory) as uow:
        items, _total = await SQLAlchemyLedgerRepository(uow.session).list_by_user(
            principal.subject, limit=200, offset=0
        )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["流水号", "幂等键", "变动", "余额类型", "类型", "状态", "来源", "阶段", "时间"]
    )
    for it in items:
        writer.writerow(
            [
                it.id,
                it.exec_id,
                it.delta,
                it.balance_type,
                it.kind,
                it.status,
                it.source,
                it.stage or "",
                it.created_at.isoformat(),
            ]
        )

    content = "\ufeff" + buffer.getvalue()  # UTF-8 BOM
    return StreamingResponse(
        iter([content.encode("utf-8")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="points_ledger.csv"'},
    )
