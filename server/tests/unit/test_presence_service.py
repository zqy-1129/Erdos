"""在线状态领域服务单元测试（端口打桩）。"""

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.presence.ports import Heartbeat
from app.domain.presence.service import PresenceService

NOW = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


class FakePresenceRepository:
    """内存会话表：user_id -> last_seen；touch 返回是否新建。"""

    def __init__(self) -> None:
        self.seen: dict[str, datetime] = {}

    async def touch(self, heartbeat: Heartbeat) -> bool:
        created = heartbeat.user_id not in self.seen
        self.seen[heartbeat.user_id] = heartbeat.occurred_at
        return created

    async def count_online(self, cutoff: datetime) -> int:
        return sum(1 for ts in self.seen.values() if ts >= cutoff)


def _hb(user: str, when: datetime = NOW) -> Heartbeat:
    return Heartbeat(user_id=user, device_id="d1", client_ip=None, occurred_at=when)


async def test_heartbeat_updates_and_stats() -> None:
    repo = FakePresenceRepository()
    service = PresenceService(repo, online_window_seconds=90.0)
    first = await service.heartbeat(_hb("u1"))
    assert first.session_created is True
    assert first.stats.current_online == 1
    second = await service.heartbeat(_hb("u2"))
    assert second.session_created is True
    assert (await service.stats(NOW)).current_online == 2
    repeat = await service.heartbeat(_hb("u1"))
    assert repeat.session_created is False, "已存在会话不产生上线事件"


async def test_window_excludes_stale_sessions() -> None:
    repo = FakePresenceRepository()
    service = PresenceService(repo, online_window_seconds=90.0)
    await service.heartbeat(_hb("stale", NOW - timedelta(seconds=120)))
    await service.heartbeat(_hb("fresh", NOW))
    assert (await service.stats(NOW)).current_online == 1, "超窗会话不计入在线"


def test_invalid_window_rejected() -> None:
    with pytest.raises(ValueError):
        PresenceService(FakePresenceRepository(), online_window_seconds=0)


def test_window_seconds_exposed() -> None:
    service = PresenceService(FakePresenceRepository(), online_window_seconds=45)
    assert service.online_window_seconds == 45.0