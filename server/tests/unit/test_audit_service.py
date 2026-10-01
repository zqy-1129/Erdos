"""审计领域服务单元测试（端口打桩，纯领域逻辑验证）。"""

from datetime import UTC, datetime

import pytest

from app.core.errors import AppError
from app.domain.audit.ports import AuditEvent, AuditEventView
from app.domain.audit.service import AuditService


class FakeAuditRepository:
    """端口桩实现：内存记录，用于验证领域编排逻辑。"""

    def __init__(self) -> None:
        self.saved: list[AuditEvent] = []

    async def exists_by_request_key(self, request_key: str) -> bool:
        return any(e.request_key == request_key for e in self.saved)

    async def add_event(self, event: AuditEvent) -> AuditEventView:
        self.saved.append(event)
        return AuditEventView(
            id=len(self.saved),
            actor_type=event.actor_type,
            actor_id=event.actor_id,
            action=event.action,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            detail=event.detail,
            client_ip=event.client_ip,
            created_at=datetime(2026, 10, 1, tzinfo=UTC),
        )


def _event(key: str = "key-1") -> AuditEvent:
    return AuditEvent(
        actor_type="user",
        actor_id="u1",
        action="account.login",
        resource_type="account",
        resource_id="r1",
        detail={"mfa": False},
        client_ip="1.2.3.4",
        request_key=key,
    )


async def test_record_once() -> None:
    repo = FakeAuditRepository()
    service = AuditService(repo)
    view = await service.record(_event())
    assert view.id == 1
    assert len(repo.saved) == 1


async def test_duplicate_key_conflict() -> None:
    service = AuditService(FakeAuditRepository())
    await service.record(_event("dup"))
    with pytest.raises(AppError) as exc_info:
        await service.record(_event("dup"))
    assert exc_info.value.spec.code == 40901
    assert exc_info.value.detail is not None