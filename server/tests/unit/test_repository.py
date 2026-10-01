"""仓储基类与事务边界测试（SQLite 真实落库）。"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.audit.ports import AuditEvent
from app.repository.audit import SQLAlchemyAuditLogRepository
from app.repository.base import SQLAlchemyRepository
from app.repository.models import AuditLog
from app.repository.uow import UnitOfWork


def _entity(n: int) -> AuditLog:
    return AuditLog(
        request_key=f"key-{n}",
        actor_type="user",
        actor_id=f"u{n}",
        action="account.login",
        resource_type="account",
        resource_id=None,
        detail=None,
        client_ip=None,
    )


async def test_add_get_list_delete(session_factory) -> None:
    async with session_factory() as session:
        repo: SQLAlchemyRepository[AuditLog] = SQLAlchemyRepository(session, AuditLog)
        e1, e2 = _entity(1), _entity(2)
        await repo.add(e1)
        await repo.add(e2)
        await session.commit()

        assert (await repo.get(e1.id)) is e1
        items, total = await repo.list(limit=10)
        assert total == 2 and len(items) == 2
        items, total = await repo.list(limit=1, offset=1)
        assert total == 2 and len(items) == 1

        await repo.delete(e1)
        await session.commit()
        items, total = await repo.list(limit=10)
        assert total == 1


async def test_list_invalid_args(session_factory) -> None:
    async with session_factory() as session:
        repo: SQLAlchemyRepository[AuditLog] = SQLAlchemyRepository(session, AuditLog)
        with pytest.raises(ValueError):
            await repo.list(limit=0)
        with pytest.raises(ValueError):
            await repo.list(limit=1, offset=-1)


async def test_audit_repo_duplicate_via_unique_constraint(session_factory) -> None:
    """绕开领域服务预检查，直接验证唯一约束兜底映射为 40901。"""
    async with session_factory() as session:
        repo = SQLAlchemyAuditLogRepository(session)
        event = AuditEvent(
            actor_type="user",
            actor_id="u1",
            action="a",
            resource_type="r",
            resource_id=None,
            detail=None,
            client_ip=None,
            request_key="uniq",
        )
        await repo.add_event(event)
        await session.commit()
        assert await repo.exists_by_request_key("uniq")
        from app.core.errors import AppError

        with pytest.raises(AppError) as exc_info:
            await repo.add_event(event)
        assert exc_info.value.spec.code == 40901


def test_uow_guards_session_outside_context(session_factory) -> None:
    uow = UnitOfWork(session_factory)
    with pytest.raises(RuntimeError):
        _ = uow.session


async def test_uow_commit_on_success(session_factory) -> None:
    async with UnitOfWork(session_factory) as uow:
        assert isinstance(uow.session, AsyncSession)
        repo: SQLAlchemyRepository[AuditLog] = SQLAlchemyRepository(uow.session, AuditLog)
        await repo.add(_entity(7))
    async with session_factory() as session:
        items, _ = await SQLAlchemyRepository(session, AuditLog).list(limit=10)
        assert len(items) == 1, "正常退出应自动提交（单提交语义）"


async def test_uow_rollback_on_error(session_factory) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        async with UnitOfWork(session_factory) as uow:
            repo: SQLAlchemyRepository[AuditLog] = SQLAlchemyRepository(uow.session, AuditLog)
            await repo.add(_entity(8))
            raise RuntimeError("boom")
    async with session_factory() as session:
        items, total = await SQLAlchemyRepository(session, AuditLog).list(limit=10)
        assert total == 0, "异常应整体回滚"