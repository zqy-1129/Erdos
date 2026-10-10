"""关键操作审计落库（《服务端架构》§9：登录 / 购买 / 权益变更 / 许可签发 / 离线对账 / 管理员操作）。

为什么单独一层：这些事件此前只写看板投影（dashboard_events），`audit_logs` 表唯一的写入方是
外部提交的 POST /v1/audit/events——也就是"服务端自己的关键操作没有任何不可篡改留痕"，
而 PRD 安全项与手册验收红线都要求"关键操作全审计"。

事务形状：一律在业务事务**提交之后**用独立短事务写。同事务写审计看起来更原子，但
①审计唯一键撞车会污染业务事务（IntegrityError 后 session 必须回滚，业务跟着丢）；
②失败路径（如登录被拒）业务事务本就回滚，审计必须活下来才能记录这次失败。

失败不静默：写不进审计就 ERROR 日志 + P2 告警（audit_write_failed），账务流水与看板事件仍在，
但缺留痕必须让人知道，不能等季度复盘才发现。
"""

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError
from app.domain.alerts.ports import AlertOutlet, BusinessAlert
from app.domain.alerts.severity import Severity
from app.domain.audit.ports import AuditEvent
from app.repository.audit import SQLAlchemyAuditLogRepository
from app.repository.uow import UnitOfWork

logger = logging.getLogger("erdos.audit")


class AuditRecorder:
    """独立事务写审计事件；重复 request_key 视为已记录（幂等，不报错也不重复告警）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @staticmethod
    async def record_in_session(
        session: AsyncSession,
        *,
        actor_type: str,
        actor_id: str,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        detail: dict[str, Any] | None = None,
        client_ip: str | None = None,
        request_key: str | None = None,
        now: datetime,
    ) -> None:
        """随业务事务一起写审计（高频且无幂等键的事件专用）。

        为什么这条不也放事务外：本机 A/B 实测，把每次 reserve 的审计改成提交后的第二个事务，
        SQLite 单写者下 P95 从 1624ms 涨到 2774ms（QPS 45.7→31.0）。无 request_key 就不可能
        撞唯一约束，因此不存在"审计冲突污染业务事务"的风险，同事务写既原子又更便宜。
        """
        await SQLAlchemyAuditLogRepository(session).add_event(
            AuditEvent(
                actor_type=actor_type,
                actor_id=actor_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                detail=detail,
                client_ip=client_ip,
                request_key=request_key,
            )
        )

    async def record(
        self,
        *,
        actor_type: str,
        actor_id: str,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        detail: dict[str, Any] | None = None,
        client_ip: str | None = None,
        request_key: str | None = None,
        now: datetime,
    ) -> bool | None:
        """写一条审计：True=新增，False=已存在（幂等跳过），None=写入失败（不得影响业务）。"""
        try:
            async with UnitOfWork(self._session_factory) as uow:
                repo = SQLAlchemyAuditLogRepository(uow.session)
                if request_key is not None and await repo.exists_by_request_key(request_key):
                    return False
                await repo.add_event(
                    AuditEvent(
                        actor_type=actor_type,
                        actor_id=actor_id,
                        action=action,
                        resource_type=resource_type,
                        resource_id=resource_id,
                        detail=detail,
                        client_ip=client_ip,
                        request_key=request_key,
                    )
                )
            return True
        except AppError:
            # 并发下撞 request_key 唯一约束：等价于已记录
            logger.info("审计已存在：%s", request_key)
            return False
        except Exception:
            logger.exception(
                "审计写入失败：%s actor=%s resource=%s", action, actor_id, resource_id
            )
            return None

    async def record_or_alert(
        self,
        alerts: AlertOutlet,
        *,
        actor_type: str,
        actor_id: str,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        detail: dict[str, Any] | None = None,
        client_ip: str | None = None,
        request_key: str | None = None,
        now: datetime,
    ) -> bool:
        """写审计；写失败额外发一条 P2 告警，保证"缺留痕"这件事本身不静默。"""
        result = await self.record(
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            detail=detail,
            client_ip=client_ip,
            request_key=request_key,
            now=now,
        )
        if result is None:
            await alerts.emit(
                BusinessAlert(
                    key="audit_write_failed",
                    severity=Severity.P2,
                    message=f"审计写入失败：{action}（{resource_type}={resource_id or '-'}）",
                    value=1.0,
                ),
                now,
            )
            return False
        return result
