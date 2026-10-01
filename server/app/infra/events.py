"""版本化内存事件总线（看板 FR-5 实时推送核心）。

设计要点：
- 发布有序、版本号单调递增（单进程内），订阅以游标对齐（since）；
- 保留有限缓冲窗口支持断连追赶；溢出后订阅方回退到全量拉取；
- 用于推送**聚合指标**（如在线数），禁止将用户明细/PII 放入事件负载（合规红线）。
"""

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.clock import utc_now

DEFAULT_BUFFER_SIZE = 500


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """单条事件：version 即 SSE 的 id（客户端游标）。"""

    version: int
    topic: str
    payload: Mapping[str, Any]
    occurred_at: datetime


class EventBroker:
    """进程内发布-订阅：有序发布 + 游标追赶订阅。"""

    def __init__(self, buffer_size: int = DEFAULT_BUFFER_SIZE) -> None:
        if buffer_size < 1:
            raise ValueError("buffer_size 必须 ≥1")
        self._buffer: deque[EventEnvelope] = deque(maxlen=buffer_size)
        self._subscribers: set[asyncio.Queue[EventEnvelope]] = set()
        self._version = 0
        self._lock = asyncio.Lock()

    @property
    def latest_version(self) -> int:
        return self._version

    async def publish(self, topic: str, payload: Mapping[str, Any]) -> EventEnvelope:
        """发布事件：版本 +1、入缓冲、扇出给全部订阅者。"""
        envelope = EventEnvelope(
            version=self._version + 1,
            topic=topic,
            payload=payload,
            occurred_at=utc_now(),
        )
        async with self._lock:
            self._version = envelope.version
            self._buffer.append(envelope)
            for queue in self._subscribers:
                self._offer(queue, envelope)
        return envelope

    def versions_after(self, since: int) -> list[EventEnvelope]:
        """返回版本号大于 since 的缓冲事件（缓冲已丢弃的无法补发）。"""
        return [env for env in self._buffer if env.version > since]

    async def subscribe(self, topics: set[str], since: int = 0) -> AsyncIterator[EventEnvelope]:
        """订阅指定主题：先补发缓冲内游标之后的积压，再实时消费。

        退出（生成器关闭）时自动取消订阅。
        """
        queue: asyncio.Queue[EventEnvelope] = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subscribers.add(queue)
            backlog = [e for e in self._buffer if e.version > since and e.topic in topics]
        try:
            for envelope in backlog:
                yield envelope
            while True:
                envelope = await queue.get()
                if envelope.topic in topics:
                    yield envelope
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    @staticmethod
    def _offer(queue: asyncio.Queue[EventEnvelope], envelope: EventEnvelope) -> None:
        """入队；慢消费者队列满时丢弃最旧（保实时性，客户端靠游标追赶）。"""
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        queue.put_nowait(envelope)