"""审计领域端口与数据载体（不依赖任何 Web/ORM 实现）。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """待记录的审计事件（领域输入）。"""

    actor_type: str
    actor_id: str
    action: str
    resource_type: str
    resource_id: str | None
    detail: dict[str, Any] | None
    client_ip: str | None
    # 幂等去重键；None 表示这条事件不做去重（高频路径如许可签发，撞唯一约束会污染业务事务）
    request_key: str | None


@dataclass(frozen=True, slots=True)
class AuditEventView:
    """审计事件回读视图（领域输出，供 API 层转为响应 DTO）。"""

    id: int
    actor_type: str
    actor_id: str
    action: str
    resource_type: str
    resource_id: str | None
    detail: dict[str, Any] | None
    client_ip: str | None
    created_at: datetime


class AuditLogRepository(Protocol):
    """审计日志仓储端口。"""

    async def exists_by_request_key(self, request_key: str) -> bool:
        """按幂等键判断事件是否已入库。"""
        ...

    async def add_event(self, event: AuditEvent) -> AuditEventView:
        """写入事件并返回落库视图。"""
        ...