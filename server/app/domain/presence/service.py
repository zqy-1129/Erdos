"""在线状态领域服务：心跳登记与在线判定编排（无 SQL、无 ORM）。"""

from datetime import datetime, timedelta

from app.domain.presence.ports import Heartbeat, HeartbeatResult, OnlineStats, PresenceRepository


class PresenceService:
    """在线监测用例：TTL 滑动窗口判定（窗口 = 心跳间隔的数倍，容错丢包）。"""

    def __init__(self, repo: PresenceRepository, online_window_seconds: float) -> None:
        if online_window_seconds <= 0:
            raise ValueError("online_window_seconds 必须 >0")
        self._repo = repo
        self._window = online_window_seconds

    @property
    def online_window_seconds(self) -> float:
        return self._window

    async def heartbeat(self, heartbeat: Heartbeat) -> HeartbeatResult:
        """登记心跳并返回最新统计与是否新建会话（供上线事件投影）。"""
        created = await self._repo.touch(heartbeat)
        stats = await self.stats(now=heartbeat.occurred_at)
        return HeartbeatResult(stats=stats, session_created=created)

    async def stats(self, now: datetime) -> OnlineStats:
        """统计当前在线：last_seen 落在滑动窗口内的去重用户数。"""
        cutoff = now - timedelta(seconds=self._window)
        current = await self._repo.count_online(cutoff)
        return OnlineStats(
            current_online=current,
            window_seconds=self._window,
            server_time=now,
        )