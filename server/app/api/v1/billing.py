"""计费订阅域接口（SP2-5 资金域核心）：商品 / 下单 / 订单 / 回调 / 退款 / 订阅 / 月赠。

资金域红线：金额一律整数分；下单以 idempotency_key 幂等；回调 payment_no 幂等 +
HMAC 签名验签（payment_callback_secret 未配置时 fail-closed 拒绝，防伪回调铸币）；
退款遵循 PRD F-005（订阅 7 天未用可退，积分包不退）。
"""

import hmac
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_session_factory, require_principal, require_roles
from app.core.clock import utc_now
from app.core.config import Settings
from app.core.envelope import Envelope, ok
from app.core.errors import INTERNAL_ERROR, NOT_FOUND, UNAUTHENTICATED, AppError
from app.core.logging import request_id_var
from app.domain.billing.ports import (
    CallbackRecord,
    CreateOrderRequest,
    OrderRecord,
    ProductRecord,
)
from app.domain.billing.service import BillingService, callback_digest
from app.infra.auth import Principal
from app.infra.billing_deps import build_billing_service
from app.repository.billing import (
    SQLAlchemyOrderRepository,
    SQLAlchemySubscriptionRepository,
)
from app.repository.uow import UnitOfWork

router = APIRouter(prefix="/billing", tags=["billing"])
admin_dep = Annotated[Principal, Depends(require_roles("admin"))]


class ProductView(BaseModel):
    id: str
    code: str
    type: str
    name: str
    price_cents: int
    points: int
    duration_days: int


class OrderView(BaseModel):
    id: str
    product_id: str
    price_cents: int
    channel: str
    status: str
    idempotency_key: str
    expires_at: datetime
    paid_at: datetime | None
    refunded_at: datetime | None
    created_at: datetime


class CreateOrderBody(BaseModel):
    product_code: str = Field(min_length=1, max_length=32)
    channel: str = Field(default="mock", max_length=16)
    idempotency_key: str = Field(min_length=1, max_length=64)


class CreateOrderView(BaseModel):
    order: OrderView
    product: ProductView
    already_exists: bool


class CallbackBody(BaseModel):
    payment_no: str = Field(min_length=1, max_length=64)
    order_id: str = Field(min_length=1, max_length=36)
    raw_digest: str = Field(min_length=1, max_length=128)


class SubscriptionView(BaseModel):
    plan: str
    status: str
    start_at: datetime
    end_at: datetime
    last_monthly_grant_at: datetime | None


def _product_view(p: ProductRecord) -> ProductView:
    return ProductView(
        id=p.id,
        code=p.code,
        type=p.type,
        name=p.name,
        price_cents=p.price_cents,
        points=p.points,
        duration_days=p.duration_days,
    )


def _order_view(o: OrderRecord) -> OrderView:
    return OrderView(
        id=o.id,
        product_id=o.product_id,
        price_cents=o.price_cents,
        channel=o.channel,
        status=o.status,
        idempotency_key=o.idempotency_key,
        expires_at=o.expires_at,
        paid_at=o.paid_at,
        refunded_at=o.refunded_at,
        created_at=o.created_at,
    )


def _service(request: Request, session: AsyncSession) -> BillingService:
    """计费服务构造走 infra 装配（与查单兜底扫描共用，避免两处参数漂移）。"""
    settings: Settings = request.app.state.settings
    return build_billing_service(session, settings, request.app.state.license_signer)


@router.get("/products", response_model=Envelope[list[ProductView]], summary="商品列表")
async def products(
    request: Request,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[list[ProductView]]:
    """启用中的商品列表（订阅 / 积分包）。"""
    async with UnitOfWork(session_factory) as uow:
        items = await _service(request, uow.session).list_products()
        view = [_product_view(p) for p in items]
    return ok(view, request_id_var.get())


@router.post("/orders", response_model=Envelope[CreateOrderView], summary="下单")
async def create_order(
    request: Request,
    payload: CreateOrderBody,
    principal: Annotated[Principal, Depends(require_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[CreateOrderView]:
    """下单（idempotency_key 幂等，30 分钟未支付关单）。"""
    now = utc_now()
    async with UnitOfWork(session_factory) as uow:
        result = await _service(request, uow.session).create_order(
            CreateOrderRequest(
                user_id=principal.subject,
                product_code=payload.product_code,
                channel=payload.channel,
                idempotency_key=payload.idempotency_key,
            ),
            now,
        )
        view = CreateOrderView(
            order=_order_view(result.order),
            product=_product_view(result.product),
            already_exists=result.already_exists,
        )
    return ok(view, request_id_var.get())


@router.get("/orders/{order_id}", response_model=Envelope[OrderView], summary="订单查询")
async def get_order(
    request: Request,
    order_id: str,
    principal: Annotated[Principal, Depends(require_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[OrderView]:
    """订单查询（PRD DF-003 兜底入口）：客户端轮询即触发服务端 T+5 分钟查单补账。

    归属校验先于查单——不给非属主触发渠道查询的机会；查单本身按窗口去重，轮询再密也只在
    每个窗口打渠道一次。查单动作对订单状态的影响通过重读返回，不在响应里谎报。
    """
    now = utc_now()
    async with UnitOfWork(session_factory) as uow:
        order = await SQLAlchemyOrderRepository(uow.session).get(order_id)
    if order is None or order.user_id != principal.subject:
        # 与「不存在」同响应，不泄露他人订单的存在性（水平越权防护）
        raise AppError(NOT_FOUND, detail="订单不存在")

    await request.app.state.order_reconciler.reconcile_order(order_id, now, throttle=True)

    async with UnitOfWork(session_factory) as uow:
        latest = await SQLAlchemyOrderRepository(uow.session).get(order_id)
    if latest is None:
        raise AppError(NOT_FOUND, detail="订单不存在")
    return ok(_order_view(latest), request_id_var.get())


@router.post(
    "/orders/reconcile", response_model=Envelope[dict], summary="查单兜底批量扫描（admin）"
)
async def reconcile_orders(
    request: Request,
    admin: admin_dep,
) -> Envelope[dict]:
    """管理端触发一轮查单兜底扫描（后台循环之外的应急/验收入口）。"""
    summary = await request.app.state.order_reconciler.sweep(utc_now())
    return ok(summary.as_dict(), request_id_var.get())


@router.post("/callbacks/payment", response_model=Envelope[OrderView], summary="支付回调")
async def payment_callback(
    request: Request,
    payload: CallbackBody,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[OrderView]:
    """支付回调入账（签名验签 + payment_no 幂等，重复回调不重复发放权益）。

    有效签名的回调就是渠道的收款凭据，因此过期订单不再拒收：created 状态一律补账；
    已关单（终态）则落差异台账并告警，由人工退款——绝不静默吞款。
    """
    settings: Settings = request.app.state.settings
    # HMAC 验签（fail-closed）：secret 未配置或签名不符一律拒绝，防伪回调免费铸币
    if not settings.payment_callback_secret:
        raise AppError(INTERNAL_ERROR, detail="支付回调签名密钥未配置")
    expected = callback_digest(
        settings.payment_callback_secret, payload.payment_no, payload.order_id
    )
    if not hmac.compare_digest(expected, payload.raw_digest):
        raise AppError(UNAUTHENTICATED, detail="支付回调签名校验失败")
    now = utc_now()
    async with UnitOfWork(session_factory) as uow:
        order = await SQLAlchemyOrderRepository(uow.session).get(payload.order_id)
        if order is None:
            raise AppError(NOT_FOUND, detail="订单不存在")
        result = await _service(request, uow.session).handle_callback(
            order.user_id,
            CallbackRecord(
                id="",
                payment_no=payload.payment_no,
                order_id=payload.order_id,
                raw_digest=payload.raw_digest,
                received_at=now,
                processed=False,
            ),
            now,
        )
        view = _order_view(result.order)

    if result.difference is not None:
        # 明细取自 result.order（与回调同一订单），避免跨事务边界复用可能为 None 的旧引用
        await request.app.state.order_reconciler.record_difference(
            order_id=payload.order_id,
            payment_no=payload.payment_no,
            reason=result.difference,
            detail={
                "channel": result.order.channel,
                "order_status": result.order.status,
                "order_price_cents": result.order.price_cents,
                "evidence": "signed_callback",
            },
            now=now,
        )
    return ok(view, request_id_var.get())


@router.post("/orders/{order_id}/refund", response_model=Envelope[OrderView], summary="退款")
async def refund(
    request: Request,
    order_id: str,
    principal: Annotated[Principal, Depends(require_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Envelope[OrderView]:
    """退款（PRD F-005：订阅 7 天未用可退，积分包不退）。"""
    now = utc_now()
    async with UnitOfWork(session_factory) as uow:
        result = await _service(request, uow.session).refund(
            principal.subject, order_id, now
        )
        view = _order_view(result.order)
    return ok(view, request_id_var.get())


@router.get("/subscription", response_model=Envelope[SubscriptionView | None], summary="订阅查询")
async def subscription(
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    plan: str = Query("monthly", pattern="^(monthly|yearly)$"),
) -> Envelope[SubscriptionView | None]:
    """当前主体订阅状态。"""
    async with UnitOfWork(session_factory) as uow:
        sub = await SQLAlchemySubscriptionRepository(uow.session).get(
            principal.subject, plan
        )
        view = (
            SubscriptionView(
                plan=sub.plan,
                status=sub.status,
                start_at=sub.start_at,
                end_at=sub.end_at,
                last_monthly_grant_at=sub.last_monthly_grant_at,
            )
            if sub
            else None
        )
    return ok(view, request_id_var.get())


@router.post("/subscription/monthly-grant", response_model=Envelope[dict], summary="月赠触发")
async def monthly_grant(
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    plan: str = Query("monthly", pattern="^(monthly|yearly)$"),
) -> Envelope[dict]:
    """触发订阅月赠（幂等：每人每月只入账一次）。"""
    now = utc_now()
    async with UnitOfWork(session_factory) as uow:
        result = await _service(request, uow.session).grant_monthly(
            principal.subject, plan, now
        )
        view = {
            "granted_points": result.granted_points,
            "already_granted": result.already_granted,
        }
    return ok(view, request_id_var.get())
