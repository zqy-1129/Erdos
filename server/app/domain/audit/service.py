"""审计领域服务：幂等判定与记录编排（无 SQL、无 ORM 依赖）。"""

from app.core.errors import CONFLICT, AppError
from app.domain.audit.ports import AuditEvent, AuditEventView, AuditLogRepository


class AuditService:
    """审计事件记录用例。"""

    def __init__(self, repo: AuditLogRepository) -> None:
        self._repo = repo

    async def record(self, event: AuditEvent) -> AuditEventView:
        """写入审计事件（幂等：同 Idempotency-Key 只入账一次）。"""
        if await self._repo.exists_by_request_key(event.request_key):
            raise AppError(CONFLICT, detail="该 Idempotency-Key 对应请求已处理")
        return await self._repo.add_event(event)