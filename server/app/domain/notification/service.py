"""通知服务（SP2-7）：异步发送 + 幂等去重 + 验证码限流。

关键红线（对齐《服务端架构》通知服务 + SP2-7 提示词）：
- 发送记录落库（message_id 幂等，全链路审计）；
- 异步发送不阻塞业务主链路（经消息队列解耦）；
- 验证码限流（60s/次 + 日 10 次）防刷；
- 同一条消息重复投递只发送一次（消费者幂等）。
"""

import secrets
from datetime import datetime

from app.core.config import Settings
from app.core.errors import RATE_LIMITED, AppError
from app.domain.notification.ports import (
    NotificationChannel,
    NotificationLogRecord,
    NotificationLogRepository,
    NotificationSender,
    NotificationStatus,
    VerificationCodeLimiter,
)


class NotificationService:
    """通知用例：发送（幂等）+ 验证码（限流）+ 续费提醒。"""

    def __init__(
        self,
        logs: NotificationLogRepository,
        sender: NotificationSender,
        code_limiter: VerificationCodeLimiter,
        settings: Settings,
    ) -> None:
        self._logs = logs
        self._sender = sender
        self._code_limiter = code_limiter
        self._settings = settings

    # ------------------------------------------------------------------
    # 通用发送（幂等去重）
    # ------------------------------------------------------------------
    async def send(
        self,
        message_id: str,
        channel: str,
        template_id: str,
        target: str,
        payload: dict,
        now: datetime,
    ) -> NotificationLogRecord:
        """发送通知：message_id 幂等（重复投递只发送一次），结果落库。

        先落库 queued 记录（唯一约束兜底），撞唯一约束说明已发送/处理过，
        直接返回既有记录，不再重复发送。
        """
        # 幂等：已发送过 -> 直接返回
        existing = await self._logs.find_by_message_id(message_id)
        if existing is not None:
            return existing

        # 落库 queued（唯一约束兜底并发重复）
        queued = await self._logs.append(
            NotificationLogRecord(
                id="",
                message_id=message_id,
                channel=channel,
                template_id=template_id,
                target=target,
                status=NotificationStatus.QUEUED.value,
                error=None,
                created_at=now,
                sent_at=None,
            )
        )
        if queued is None:
            # 并发撞唯一约束：另一请求已处理，返回既有
            return await self._logs.find_by_message_id(message_id)  # type: ignore[return-value]

        # 实际发送
        try:
            await self._sender.send(channel, template_id, target, payload)
            return await self._logs.mark_sent(message_id, now)
        except Exception as exc:  # noqa: BLE001 - 发送失败落库为 failed，不阻断主链路
            return await self._logs.mark_failed(message_id, str(exc)[:256], now)

    # ------------------------------------------------------------------
    # 验证码（限流 + 防刷）
    # ------------------------------------------------------------------
    async def send_verification_code(
        self, target: str, now: datetime
    ) -> str:
        """发送验证码：限流（60s/次 + 日 10 次），返回生成的验证码（dev 可见）。

        限流键用 target（手机号/邮箱），超出限流抛 429。
        """
        key = f"code:{target}"
        if not self._code_limiter.allow(key):
            retry = self._code_limiter.retry_after_seconds(key)
            raise AppError(RATE_LIMITED, detail=f"验证码发送过于频繁，请 {int(retry)} 秒后重试")

        code = secrets.randbelow(1_000_000)  # 6 位验证码
        await self._sender.send(
            NotificationChannel.SMS.value,
            "verification_code",
            target,
            {"code": f"{code:06d}"},
        )
        return f"{code:06d}"

    # ------------------------------------------------------------------
    # 续费前 3 天提醒
    # ------------------------------------------------------------------
    async def notify_renewal_reminder(
        self, user_id: str, target: str, expire_at: datetime, now: datetime
    ) -> NotificationLogRecord:
        """续费提醒：订阅到期前 3 天发送（幂等：message_id = renew:{user}:{到期日}）。"""
        days_left = (expire_at - now).days
        if days_left > self._settings.renewal_remind_days:
            raise ValueError("未到提醒窗口")
        message_id = f"renew:{user_id}:{expire_at.date().isoformat()}"
        return await self.send(
            message_id=message_id,
            channel=NotificationChannel.EMAIL.value,
            template_id="renewal_reminder",
            target=target,
            payload={"expire_at": expire_at.isoformat()},
            now=now,
        )
