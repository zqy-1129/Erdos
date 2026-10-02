"""通知服务域端口与数据载体（SP2-7，与《服务端架构》通知服务对齐）。

关键约束：
- 发送记录落库（message_id 幂等，全链路审计）；
- 异步发送不阻塞业务主链路（经消息队列解耦）；
- 验证码限流（60s/次 + 日 10 次）防刷。
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class NotificationChannel(StrEnum):
    """通知渠道。"""

    EMAIL = "email"
    SMS = "sms"


class NotificationStatus(StrEnum):
    """发送状态。"""

    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class NotificationLogRecord:
    """发送记录视图。"""

    id: str
    message_id: str
    channel: str
    template_id: str
    target: str
    status: str
    error: str | None
    created_at: datetime
    sent_at: datetime | None


class NotificationLogRepository(Protocol):
    """通知发送记录仓储端口。"""

    async def append(self, record: NotificationLogRecord) -> NotificationLogRecord | None:
        """落库发送记录；message_id 冲突返回 None（幂等去重）。"""
        ...

    async def find_by_message_id(self, message_id: str) -> NotificationLogRecord | None:
        """按消息幂等键查记录。"""
        ...

    async def mark_sent(self, message_id: str, now: datetime) -> NotificationLogRecord:
        """标记发送成功。"""
        ...

    async def mark_failed(self, message_id: str, error: str, now: datetime) -> NotificationLogRecord:
        """标记发送失败。"""
        ...


class NotificationSender(Protocol):
    """通知发送器端口（dev 日志 / 生产 SMTP/短信）。"""

    async def send(self, channel: str, template_id: str, target: str, payload: dict) -> None:
        """发送一条通知；失败抛异常。"""
        ...


class VerificationCodeLimiter(Protocol):
    """验证码限流端口（60s/次 + 日 10 次；进程内/Redis 实现同签名）。"""

    async def allow(self, key: str) -> bool:
        """尝试放行一次验证码发送；返回 False 触发限流。"""
        ...

    async def retry_after_seconds(self, key: str) -> float:
        """下次可发送还需等待的秒数。"""
        ...
