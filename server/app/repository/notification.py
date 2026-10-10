"""通知发送记录仓储落库实现（notification_send_logs）。

message_id 唯一约束兜底幂等去重；mark_sent/mark_failed 原子更新状态。
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.notification.ports import (
    NotificationLogRecord,
    NotificationLogRepository,
)
from app.repository.models import NotificationSendLog


def _new_id() -> str:
    return str(uuid.uuid4())


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _utc_required(value: datetime | None) -> datetime:
    if value is None:
        raise ValueError("非空时间列读取为 NULL")
    return _ensure_utc(value)  # type: ignore[return-value]


def _record(row: NotificationSendLog) -> NotificationLogRecord:
    return NotificationLogRecord(
        id=row.id,
        message_id=row.message_id,
        channel=row.channel,
        template_id=row.template_id,
        target=row.target,
        status=row.status,
        error=row.error,
        created_at=_utc_required(row.created_at),
        sent_at=row.sent_at,
    )


class SQLAlchemyNotificationLogRepository(NotificationLogRepository):
    """notification_send_logs 表实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, record: NotificationLogRecord) -> NotificationLogRecord | None:
        row = NotificationSendLog(
            id=_new_id() if not record.id else record.id,
            message_id=record.message_id,
            channel=record.channel,
            template_id=record.template_id,
            target=record.target,
            status=record.status,
            error=record.error,
            created_at=record.created_at,
            sent_at=record.sent_at,
        )
        try:
            async with self._session.begin_nested():  # 撞幂等键只撤本次插入
                self._session.add(row)
                await self._session.flush()
        except IntegrityError:
            # 并发窗口：另一路已按同一 message_id 落库。整会话 rollback 会把调用方
            # 同事务里的其它写入一起抹掉（scheduler.claim、monitoring.upsert_minute
            # 改前是同一个坑），这里只交回 None 让上层按"已处理"复用既有记录。
            # add() 必须也在保存点内：留在外面的话这次失败的实例会挂在会话里，
            # 后续任何操作都以 PendingRollbackError 收场。
            return None
        return _record(row)

    async def find_by_message_id(self, message_id: str) -> NotificationLogRecord | None:
        row = (
            await self._session.execute(
                select(NotificationSendLog).where(
                    NotificationSendLog.message_id == message_id
                )
            )
        ).scalar_one_or_none()
        return _record(row) if row is not None else None

    async def mark_sent(self, message_id: str, now: datetime) -> NotificationLogRecord:
        await self._session.execute(
            update(NotificationSendLog)
            .where(NotificationSendLog.message_id == message_id)
            .values(status="sent", sent_at=now)
        )
        return await self.find_by_message_id(message_id)  # type: ignore[return-value]

    async def mark_failed(
        self, message_id: str, error: str, now: datetime
    ) -> NotificationLogRecord:
        await self._session.execute(
            update(NotificationSendLog)
            .where(NotificationSendLog.message_id == message_id)
            .values(status="failed", error=error)
        )
        return await self.find_by_message_id(message_id)  # type: ignore[return-value]
