"""在线会话仓储落库实现（双驱动兼容：不做数据库方言特化 upsert）。"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.presence.ports import Heartbeat, PresenceRepository
from app.repository.models import PresenceSession


class SQLAlchemyPresenceRepository(PresenceRepository):
    """presence_sessions 表实现：按 (user_id, device_id) 幂等 touch。

    并发语义：先查后写；并发窗口内撞唯一约束时回滚并转为更新，
    保证同一 (user_id, device_id) 同一时刻只有一行且心跳不丢失。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def touch(self, heartbeat: Heartbeat) -> bool:
        stmt = select(PresenceSession).where(
            PresenceSession.user_id == heartbeat.user_id,
            PresenceSession.device_id == heartbeat.device_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            try:
                self._session.add(
                    PresenceSession(
                        user_id=heartbeat.user_id,
                        device_id=heartbeat.device_id,
                        last_seen_at=heartbeat.occurred_at,
                        client_ip=heartbeat.client_ip,
                    )
                )
                await self._session.flush()
                return True
            except IntegrityError:
                # 并发窗口：另一请求已插入同键行 -> 撤销本次插入，转为更新
                await self._session.rollback()
                row = (await self._session.execute(stmt)).scalar_one()
        row.last_seen_at = heartbeat.occurred_at
        row.client_ip = heartbeat.client_ip
        await self._session.flush()
        return False

    async def count_online(self, cutoff: datetime) -> int:
        stmt = select(func.count()).select_from(PresenceSession).where(
            PresenceSession.last_seen_at >= cutoff
        )
        return int((await self._session.execute(stmt)).scalar_one())