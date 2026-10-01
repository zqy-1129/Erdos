"""审计仓储落库实现：端口在此落地，ORM 与 SQL 只出现在本层。"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import CONFLICT, AppError
from app.domain.audit.ports import AuditEvent, AuditEventView
from app.repository.base import SQLAlchemyRepository
from app.repository.models import AuditLog


class SQLAlchemyAuditLogRepository(SQLAlchemyRepository[AuditLog]):
    """审计日志仓储（SQLAlchemy 实现）。"""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, AuditLog)

    async def exists_by_request_key(self, request_key: str) -> bool:
        stmt = select(AuditLog.id).where(AuditLog.request_key == request_key).limit(1)
        return (await self._session.execute(stmt)).first() is not None

    async def add_event(self, event: AuditEvent) -> AuditEventView:
        entity = AuditLog(
            request_key=event.request_key,
            actor_type=event.actor_type,
            actor_id=event.actor_id,
            action=event.action,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            detail=event.detail,
            client_ip=event.client_ip,
        )
        try:
            await self.add(entity)
        except IntegrityError as exc:
            # 并发窗口内撞唯一约束：等价于重复请求，映射为幂等冲突
            raise AppError(CONFLICT, detail="相同请求已并发处理") from exc
        return AuditEventView(
            id=entity.id,
            actor_type=entity.actor_type,
            actor_id=entity.actor_id,
            action=entity.action,
            resource_type=entity.resource_type,
            resource_id=entity.resource_id,
            detail=entity.detail,
            client_ip=entity.client_ip,
            created_at=entity.created_at,
        )