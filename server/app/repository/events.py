"""看板事件投影仓储（dedup_key 唯一约束做幂等兜底，同键静默跳过）。"""

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.events.ports import DashboardEventRepository, EventData
from app.repository.models import DashboardEvent


class SQLAlchemyDashboardEventRepository(DashboardEventRepository):
    """dashboard_events 表实现。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        occurred_at: datetime,
        type: str,
        severity: str,
        actor_id: str | None,
        payload: dict[str, Any] | None,
        dedup_key: str | None = None,
    ) -> None:
        try:
            self._session.add(
                DashboardEvent(
                    occurred_at=occurred_at,
                    type=type,
                    severity=severity,
                    actor_id=actor_id,
                    payload=payload,
                    dedup_key=dedup_key,
                )
            )
            await self._session.flush()
        except IntegrityError:
            # 幂等投影：同 dedup_key 已在库 -> 回滚本次写入并跳过
            await self._session.rollback()

    async def query(
        self,
        *,
        types: set[str] | None,
        start: datetime | None,
        end: datetime | None,
        limit: int,
        offset: int,
    ) -> tuple[list[EventData], int]:
        conditions: list[Any] = []
        if types:
            conditions.append(DashboardEvent.type.in_(sorted(types)))
        if start is not None:
            conditions.append(DashboardEvent.occurred_at >= start)
        if end is not None:
            conditions.append(DashboardEvent.occurred_at <= end)

        count_stmt = select(func.count()).select_from(DashboardEvent)
        list_stmt = select(DashboardEvent)
        if conditions:
            count_stmt = count_stmt.where(*conditions)
            list_stmt = list_stmt.where(*conditions)
        total = int((await self._session.execute(count_stmt)).scalar_one())

        rows = (
            (
                await self._session.execute(
                    list_stmt.order_by(DashboardEvent.occurred_at.desc())
                    .order_by(DashboardEvent.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        items = [
            EventData(
                id=row.id,
                occurred_at=row.occurred_at,
                type=row.type,
                severity=row.severity,
                actor_id=row.actor_id,
                payload=row.payload,
            )
            for row in rows
        ]
        return items, total