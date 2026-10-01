"""在线状态领域端口与数据载体（看板 FR-1）。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """一次心跳（幂等：同 user+device 重复心跳只更新 last_seen）。"""

    user_id: str
    device_id: str | None
    client_ip: str | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class OnlineStats:
    """在线统计（聚合视图，对外不含任何用户标识）。"""

    current_online: int
    window_seconds: float
    server_time: datetime


@dataclass(frozen=True, slots=True)
class HeartbeatResult:
    """心跳处理结果：最新统计 + 是否新建了会话（用于上线事件判定）。"""

    stats: OnlineStats
    session_created: bool


class PresenceRepository(Protocol):
    """在线会话仓储端口。"""

    async def touch(self, heartbeat: Heartbeat) -> bool:
        """写入/更新会话最近心跳时间；返回是否新建会话。"""
        ...

    async def count_online(self, cutoff: datetime) -> int:
        """统计 last_seen >= cutoff 的去重用户数。"""
        ...