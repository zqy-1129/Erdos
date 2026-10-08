"""支付查单兜底编排（PRD DF-003 异常处理 / EC-N7 / DEC-022 的服务端实现）。

要解决的问题：支付回调是"至少一次"投递，会丢。此前服务端只会读库和按 30 分钟盲关单，
用户已付款而回调丢失时，订单被关成终态、迟到回调又因 payment_no 已占用而幂等跳过——
钱收了、权益没发、也没人知道。本模块把 PRD 承诺的 T+5 分钟兜底补账补上。

事务形状（踩过坑，别再改回去）：读订单 → 渠道查单 → 补账/关单，三段各自一短事务，
网络 I/O 绝不夹在写事务里。同一连接开第二个写事务会被 SQLite 单写锁堵死
（database is locked，对账告警链路同样栽过），渠道 RT 抖动还会拖长事务持锁时间。

差异处置（已拍板口径）：closed 是终态不可迁移，因此"渠道已收款但订单已关单"与"回包金额不符"
一律落差异审计台账 + P2 告警走人工退款，不自动补账、更不静默丢弃。
"""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import utc_now
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.alerts.ports import AlertOutlet, BusinessAlert
from app.domain.alerts.severity import Severity
from app.domain.audit.ports import AuditEvent
from app.domain.billing.ports import (
    ChannelPayment,
    ChannelQueryStatus,
    OrderRecord,
    PaymentChannel,
    ReconcileAction,
)
from app.domain.billing.service import BillingService
from app.domain.points.service import LicenseSigner
from app.infra.billing_deps import build_billing_service
from app.repository.audit import SQLAlchemyAuditLogRepository
from app.repository.billing import SQLAlchemyOrderRepository
from app.repository.scheduler import SQLAlchemySchedulerRunRepository
from app.repository.uow import UnitOfWork

logger = logging.getLogger("erdos.billing.reconcile")

# 查单去重记账走 scheduler_runs（task_name + batch_key 唯一约束），不新增表字段
ORDER_QUERY_TASK = "order_query"
ORDER_CLOSED_CHECK_TASK = "order_closed_check"


@dataclass(frozen=True, slots=True)
class ReconcileSummary:
    """一轮扫描（或一次轮询触发）的动作计数，供管理端与压测断言。"""

    scanned: int = 0
    settled: int = 0
    closed: int = 0
    differences: int = 0
    pending: int = 0
    throttled: int = 0
    unavailable: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "settled": self.settled,
            "closed": self.closed,
            "differences": self.differences,
            "pending": self.pending,
            "throttled": self.throttled,
            "unavailable": self.unavailable,
        }

    def counted(self, action: ReconcileAction) -> "ReconcileSummary":
        """把一个动作计入汇总（不可变 dataclass，返回新实例）。"""
        field = {
            ReconcileAction.SETTLED: "settled",
            ReconcileAction.CLOSED: "closed",
            ReconcileAction.DIFFERENCE: "differences",
            ReconcileAction.PENDING: "pending",
            ReconcileAction.THROTTLED: "throttled",
            ReconcileAction.UNAVAILABLE: "unavailable",
            ReconcileAction.NOT_FOUND: "pending",
        }[action]
        return replace(self, **{field: getattr(self, field) + 1})


class OrderReconciler:
    """查单兜底用例编排：轮询触发、后台扫描与管理端批量扫描共用同一套判定。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        channels: Mapping[str, PaymentChannel],
        settings: Settings,
        signer: LicenseSigner,
        alerts: AlertOutlet,
    ) -> None:
        self._session_factory = session_factory
        self._channels = channels
        self._settings = settings
        self._signer = signer
        self._alerts = alerts

    # ------------------------------------------------------------------
    # 单订单查单兜底
    # ------------------------------------------------------------------
    async def reconcile_order(
        self, order_id: str, now: datetime, *, throttle: bool = False
    ) -> ReconcileAction:
        """对一个订单执行一次查单兜底。

        created 订单：到 T+N 分钟后查单，PAID 补账 / NOT_PAID 过期则关单 / 其余不动。
        closed 订单：每天复核一次"渠道是否其实收了钱"（回调也丢了就只能靠这里兜），
        发现收款即落差异台账并告警——closed 是终态，不自动补账。
        paid / refunded：终态无疑问，不查。

        throttle=True 时按查单窗口去重（客户端轮询路径用，避免每 2 秒打一次渠道）；
        后台扫描每轮每单只来一次，不需要去重。
        """
        order = await self._load(order_id)
        if order is None:
            return ReconcileAction.NOT_FOUND
        if order.status == "closed":
            return await self._confirm_closed(order, now)
        if order.status != "created":
            return ReconcileAction.PENDING
        if now < order.created_at + timedelta(
            minutes=self._settings.order_query_after_minutes
        ):
            return ReconcileAction.PENDING  # 未到 T+5 分钟
        if throttle and not await self._claim(
            ORDER_QUERY_TASK, order_id, now, self._settings.order_query_window_minutes * 60
        ):
            return ReconcileAction.THROTTLED

        channel = self._channels.get(order.channel)
        if channel is None:
            logger.warning("订单 %s 渠道 %s 未注册：不查单、不关单", order_id, order.channel)
            return ReconcileAction.UNAVAILABLE
        try:
            result = await channel.query_order(order_id)  # 网络 I/O，此处不持任何事务
        except Exception:
            logger.exception("渠道 %s 查单异常：order=%s", channel.name, order_id)
            return ReconcileAction.UNAVAILABLE

        if result.status is ChannelQueryStatus.PAID and result.payment is not None:
            return await self._settle(order, result.payment, channel.name, now)
        if result.status is ChannelQueryStatus.NOT_PAID:
            return await self._close_if_expired(order, now)
        logger.warning(
            "查单结果未知：order=%s channel=%s reason=%s（订单保持 created 等下一轮）",
            order_id,
            channel.name,
            result.reason,
        )
        return ReconcileAction.UNAVAILABLE

    async def _confirm_closed(self, order: OrderRecord, now: datetime) -> ReconcileAction:
        """已关单订单的收款复核：每天最多确认一次，发现收款只记差异不补账。"""
        day_seconds = 24 * 3600
        if not await self._claim(
            ORDER_CLOSED_CHECK_TASK, order.id, now, day_seconds
        ):
            return ReconcileAction.THROTTLED
        channel = self._channels.get(order.channel)
        if channel is None:
            return ReconcileAction.UNAVAILABLE
        try:
            result = await channel.query_order(order.id)
        except Exception:
            logger.exception("已关单订单复核查单异常：order=%s", order.id)
            return ReconcileAction.UNAVAILABLE
        if result.status is not ChannelQueryStatus.PAID or result.payment is None:
            return ReconcileAction.PENDING
        async with UnitOfWork(self._session_factory) as uow:
            outcome = await self._service(uow.session).settle_from_channel(
                order.id, result.payment, channel.name, now
            )
        if outcome.difference is None:
            # payment_no 已被记过：同一笔差异只进一次台账，后续复核静默收敛
            return ReconcileAction.PENDING
        await self.record_difference(
            order_id=order.id,
            payment_no=result.payment.payment_no,
            reason=outcome.difference,
            detail={
                "channel": channel.name,
                "order_status": outcome.order.status,
                "order_price_cents": order.price_cents,
                "channel_amount_cents": result.payment.amount_cents,
                "evidence": "closed_recheck",
            },
            now=now,
        )
        logger.warning("已关单订单查到收款，转人工处置：order=%s", order.id)
        return ReconcileAction.DIFFERENCE

    async def _settle(
        self, order: OrderRecord, payment: ChannelPayment, channel_name: str, now: datetime
    ) -> ReconcileAction:
        async with UnitOfWork(self._session_factory) as uow:
            service = self._service(uow.session)
            outcome = await service.settle_from_channel(
                order.id, payment, channel_name, now
            )
        if outcome.difference is not None:
            await self.record_difference(
                order_id=order.id,
                payment_no=payment.payment_no,
                reason=outcome.difference,
                detail={
                    "channel": channel_name,
                    "order_status": outcome.order.status,
                    "order_price_cents": order.price_cents,
                    "channel_amount_cents": payment.amount_cents,
                    "evidence": "channel_query",
                },
                now=now,
            )
            return ReconcileAction.DIFFERENCE
        return ReconcileAction.SETTLED if outcome.applied else ReconcileAction.PENDING

    async def _close_if_expired(
        self, order: OrderRecord, now: datetime
    ) -> ReconcileAction:
        async with UnitOfWork(self._session_factory) as uow:
            latest = await self._service(uow.session).close_unpaid(order.id, now)
        if latest is not None and latest.status == "closed":
            logger.info("订单 %s 渠道确认未收款且已过时限，关单", order.id)
            return ReconcileAction.CLOSED
        return ReconcileAction.PENDING

    # ------------------------------------------------------------------
    # 批量扫描（后台循环 + 管理端触发）
    # ------------------------------------------------------------------
    async def sweep(self, now: datetime, limit: int | None = None) -> ReconcileSummary:
        """扫描待兜底订单（到点未支付 + 复核窗口内已关单），逐单处理且互不中断。"""
        size = limit or self._settings.order_reconcile_batch_size
        created_cutoff = now - timedelta(minutes=self._settings.order_query_after_minutes)
        closed_before = now - timedelta(hours=self._settings.order_closed_recheck_hours)
        closed_after = now - timedelta(days=self._settings.order_closed_recheck_days)
        async with UnitOfWork(self._session_factory) as uow:
            orders = await SQLAlchemyOrderRepository(uow.session).list_reconcilable(
                created_before=created_cutoff,
                closed_before=closed_before,
                closed_after=closed_after,
                limit=size,
            )

        summary = ReconcileSummary(scanned=len(orders))
        for order in orders:
            try:
                action = await self.reconcile_order(order.id, now)
            except Exception:
                logger.exception("查单兜底处理失败：order=%s", order.id)
                action = ReconcileAction.UNAVAILABLE
            summary = summary.counted(action)
        return summary

    # ------------------------------------------------------------------
    # 差异台账 + 告警
    # ------------------------------------------------------------------
    async def record_difference(
        self,
        *,
        order_id: str,
        payment_no: str,
        reason: str,
        detail: dict[str, object],
        now: datetime,
    ) -> None:
        """资金差异落审计台账（request_key 幂等）并外发 P2 告警。

        台账写不进去也要告警：差异被静默吞掉是本模块要修的那个 bug 的同类错误。
        """
        request_key = f"payment-diff:{order_id}:{payment_no}"
        already_recorded = False
        try:
            async with UnitOfWork(self._session_factory) as uow:
                repo = SQLAlchemyAuditLogRepository(uow.session)
                if await repo.exists_by_request_key(request_key):
                    already_recorded = True
                else:
                    await repo.add_event(
                        AuditEvent(
                            actor_type="system",
                            actor_id="payment_reconcile",
                            action="billing.payment_difference",
                            resource_type="order",
                            resource_id=order_id,
                            detail={**detail, "reason": reason},
                            client_ip=None,
                            request_key=request_key,
                        )
                    )
        except AppError:
            # 并发下撞 request_key 唯一约束：等价于另一路已记账，不重复告警
            already_recorded = True
            logger.info("资金差异台账已存在：%s", request_key)
        except Exception:
            logger.exception("资金差异台账写入失败，仍继续告警：%s", request_key)

        if not already_recorded:
            await self._alerts.emit(
                BusinessAlert(
                    key="payment_difference",
                    severity=Severity.P2,
                    message=f"订单 {order_id} 资金差异：{reason}"[:500],
                    value=1.0,
                ),
                now,
            )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _service(self, session: AsyncSession) -> BillingService:
        return build_billing_service(session, self._settings, self._signer)

    async def _load(self, order_id: str) -> OrderRecord | None:
        async with UnitOfWork(self._session_factory) as uow:
            order = await SQLAlchemyOrderRepository(uow.session).get(order_id)
        return order

    async def _claim(
        self, task: str, order_id: str, now: datetime, bucket_seconds: int
    ) -> bool:
        """按时间桶去重：同一订单同一桶只有一个调用方真去查渠道（防轮询打爆渠道）。"""
        bucket = int(now.timestamp()) // max(1, bucket_seconds)
        async with UnitOfWork(self._session_factory) as uow:
            claimed = await SQLAlchemySchedulerRunRepository(uow.session).claim(
                task, f"{order_id}:{bucket}", now
            )
        return claimed


async def run_order_reconcile_loop(app: FastAPI) -> None:
    """查单兜底后台扫描循环（lifespan 启动，取消即退出）。

    ERDOS_ORDER_RECONCILE_INTERVAL_SECONDS=0 时整条循环不启动，兜底只靠客户端轮询与管理端触发。
    """
    settings: Settings = app.state.settings
    interval = settings.order_reconcile_interval_seconds
    if interval <= 0:
        logger.info("查单兜底后台扫描已关闭（ERDOS_ORDER_RECONCILE_INTERVAL_SECONDS=0）")
        return
    reconciler: OrderReconciler = app.state.order_reconciler
    while True:
        try:
            summary = await reconciler.sweep(utc_now())
            if summary.scanned:
                logger.info("查单兜底扫描：%s", summary.as_dict())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("查单兜底扫描周期失败（下周期重试）")
        await asyncio.sleep(interval)
