"""看板事件投影端口（事件与走势时间对齐的数据载体）。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class EventData:
    """事件投影视图（对外可序列化负载）。"""

    id: int
    occurred_at: datetime
    type: str
    severity: str
    actor_id: str | None
    payload: dict[str, Any] | None


class DashboardEventRepository(Protocol):
    """看板事件投影仓储端口（投影幂等由 dedup_key 保证）。"""

    async def append(
        self,
        *,
        occurred_at: datetime,
        type: str,
        severity: str,
        actor_id: str | None,
        payload: dict[str, Any] | None,
        dedup_key: str | None = None,
    ) -> None:
        """追加事件；dedup_key 已存在时静默跳过（幂等投影）。"""
        ...

    async def query(
        self,
        *,
        types: set[str] | None,
        start: datetime | None,
        end: datetime | None,
        limit: int,
        offset: int,
    ) -> tuple[list[EventData], int]:
        """按类型/时间范围筛选事件，返回 (items 按时间倒序, total)。"""
        ...