"""业务告警扇出实现（SP2-7/SP4-3 收敛点）：告警事件落库 + 看板 SSE + Webhook + 静默去重。

为什么存在：《服务端架构》§10 的三档 SLO 告警分级（P0 飞书即时 / P1 邮件 / P2 对账差异消息提醒）
此前只有 domain 纯函数与单测，运行链路里没有任何调用方——对账差异过去只在 HTTP 响应里回
``alerted=True``，没人看接口就等于没告警（同"已实现待验收"易误判的那类缺口）。
本实现只接业务差异类告警，采样阈值类告警仍走 monitoring 的 AlertEvaluator。

落库走独立事务且失败只记日志：告警是旁路，既不能把业务事务带下水滚，也不能因为发不出去就
让调用方失败（fail-open 于可用性、fail-loud 于日志）。
"""

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.topics import EVENTS_TOPIC
from app.domain.alerts.ports import AlertOutlet, BusinessAlert
from app.domain.alerts.severity import Alert, Severity, SilenceManager
from app.domain.monitoring.ports import AlertTransition
from app.infra.alert_webhook import AlertWebhookDispatcher
from app.infra.events import EventBroker
from app.repository.events import SQLAlchemyDashboardEventRepository
from app.repository.uow import UnitOfWork

logger = logging.getLogger("erdos.alerts")

# 告警级别 -> 事件 severity 字段（看板按 warning/info 着色，不认 P0/P1/P2）
_EVENT_SEVERITY: dict[Severity, str] = {
    Severity.P0: "critical",
    Severity.P1: "warning",
    Severity.P2: "warning",
}


class BrokerAlertOutlet(AlertOutlet):
    """进程内业务告警出口：静默窗口去重后落告警事件、推看板 SSE、外发 Webhook。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        broker: EventBroker,
        webhook: AlertWebhookDispatcher,
        silence: SilenceManager | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._broker = broker
        self._webhook = webhook
        self._silence = silence or SilenceManager()

    async def emit(self, alert: BusinessAlert, now: datetime, dedupe: bool = True) -> bool:
        """外发一条告警；dedupe=True 时同级别同指标在静默窗口内只发一次。

        采样类告警的状态机已经保证"只在状态翻转时产出"，必须传 dedupe=False，
        否则"触发→恢复→5 分钟内再触发"的 P0 会被静默窗口吞掉。恢复通知一律不去重。
        """
        entry = Alert(
            severity=alert.severity,
            metric=alert.key,
            message=alert.message,
            occurred_at=now,
        )
        deduped = (
            alert.state == "triggered"
            and dedupe
            and not self._silence.should_send(entry, now)
        )
        if deduped:
            logger.info("告警静默去重：%s %s", alert.key, alert.severity)
            return False

        transition = AlertTransition(
            metric=alert.key,
            state=alert.state,
            value=alert.value,
            threshold=alert.threshold,
            occurred_at=now,
            detail_message=alert.message,
        )
        event_severity = (
            _EVENT_SEVERITY[alert.severity]
            if alert.state == "triggered"
            else "info"
        )
        payload = {
            "metric": transition.metric,
            "level": alert.severity.value,
            "state": transition.state,
            "value": transition.value,
            "threshold": transition.threshold,
            "message": transition.message,
        }
        await self._persist(now, event_severity, payload)
        await self._broker.publish(
            EVENTS_TOPIC,
            {
                "type": "monitor.alert",
                "severity": event_severity,
                "actor_id": "system",
                "occurred_at": now.isoformat(),
                "payload": payload,
            },
        )
        # Webhook 未配置时 dispatch 直接返回；fire-and-forget，不阻塞调用方
        self._webhook.dispatch(transition)
        logger.log(
            logging.WARNING if alert.state == "triggered" else logging.INFO,
            "业务告警外发：level=%s metric=%s state=%s value=%s message=%s",
            alert.severity.value,
            alert.key,
            alert.state,
            alert.value,
            alert.message,
        )
        return True

    async def _persist(
        self, now: datetime, event_severity: str, payload: dict[str, Any]
    ) -> None:
        """告警事件独立事务落库；失败只记日志，不影响调用方事务与后续扇出。"""
        try:
            async with UnitOfWork(self._session_factory) as uow:
                await SQLAlchemyDashboardEventRepository(uow.session).append(
                    occurred_at=now,
                    type="monitor.alert",
                    severity=event_severity,
                    actor_id="system",
                    payload=payload,
                )
        except Exception:
            logger.exception(
                "告警事件落库失败：metric=%s（SSE 与 Webhook 仍会尝试外发）",
                payload.get("metric"),
            )
