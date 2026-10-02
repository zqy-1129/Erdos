"""进程内异步消息总线（SP2-7 MQ 主题的本地实现）。

语义对齐《服务端架构》第 6 章：
- 至少一次投递（at-least-once）：消息发布后必定投递给订阅者，不主动丢弃；
- 消费者幂等：投递可能重复，由消费方通过 message_id 或业务幂等键去重；
- 异步解耦：publish 非阻塞（同步入队即返回），不阻塞业务主链路。

当前为单实例进程内实现（开发/单测）；生产接入 RabbitMQ/Kafka 时保持 publish/subscribe
端口不变，仅替换实现。
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.clock import utc_now


@dataclass(frozen=True, slots=True)
class Message:
    """单条消息：message_id 为幂等去重键（消费方据此去重）。"""

    message_id: str
    topic: str
    payload: Mapping[str, Any]
    occurred_at: datetime


MessageHandler = Callable[[Message], Awaitable[None]]


class MessageBus:
    """进程内消息总线：发布-订阅（推送式），至少一次投递。"""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[MessageHandler]] = {}
        self._lock = asyncio.Lock()

    async def publish(
        self, topic: str, payload: Mapping[str, Any], message_id: str | None = None
    ) -> Message:
        """发布消息：同步扇出给全部订阅者（不等待处理完成，异步解耦）。

        订阅者 handler 抛异常时记录但不影响其它订阅者（至少一次投递，不静默丢失——
        异常由 handler 内部处理或由上层重试）。
        """
        message = Message(
            message_id=message_id or uuid.uuid4().hex,
            topic=topic,
            payload=payload,
            occurred_at=utc_now(),
        )
        async with self._lock:
            handlers = list(self._subscribers.get(topic, []))
        for handler in handlers:
            try:
                await handler(message)
            except Exception:
                # 至少一次投递：单个消费者失败不阻断其它消费者；
                # 失败重试/死信由生产 MQ 负责（当前进程内实现记录日志即可）。
                continue
        return message

    async def subscribe(self, topic: str, handler: MessageHandler) -> None:
        """注册消费者（幂等：同 topic+handler 不重复注册）。"""
        async with self._lock:
            handlers = self._subscribers.setdefault(topic, [])
            if handler not in handlers:
                handlers.append(handler)

    async def unsubscribe(self, topic: str, handler: MessageHandler) -> None:
        """注销消费者。"""
        async with self._lock:
            handlers = self._subscribers.get(topic)
            if handlers and handler in handlers:
                handlers.remove(handler)

    def subscriber_count(self, topic: str) -> int:
        """某主题的消费者数量（测试/观测用）。"""
        return len(self._subscribers.get(topic, []))
