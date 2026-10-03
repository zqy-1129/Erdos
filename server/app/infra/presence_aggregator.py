"""在线分钟桶聚合器（看板在线趋势数据源，性能优化项，2026-10-03）。

背景：原实现每次心跳同事务写 presence_minute_agg 分钟桶——所有并发心跳
落在同一分钟行上（单行漏斗），PG 下 100 并发 P95 数百 ms（SP2-8 压测实测）。
方案（登记于 reports/SP2-8验收预案 优化项 1）：心跳仅在进程内记录在线值，
后台任务按固定周期批量落库（单消费者无竞争），消除漏斗且不改表结构/接口。

语义说明：
- 同分钟多次心跳取「最后一次在线值」（与原覆盖写语义一致）；
- 落库延迟 ≤ presence_aggregate_interval_seconds（默认 60s），趋势为近似分钟
  快照（实时在线口径由 SSE presence.online_count 承担，不受影响）；
- 进程重启丢失聚合窗口内未落库的分钟桶（≤1 个间隔窗口，可接受）。
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.repository.metrics import SQLAlchemyOnlineTrendRepository

_logger = get_logger("erdos.presence_aggregator")


class MinuteAggregator:
    """进程内分钟桶聚合：record（心跳侧，锁内覆盖）→ flush_and_prune（后台单消费者）。"""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], retention_days: int
    ) -> None:
        self._factory = session_factory
        self._retention_days = retention_days
        self._buckets: dict[datetime, int] = {}
        self._lock = asyncio.Lock()

    async def record(self, minute_ts: datetime, online: int) -> None:
        """记录某分钟的在线值（同分钟多次心跳取最后一次）。"""
        async with self._lock:
            self._buckets[minute_ts] = online

    async def flush_and_prune(self) -> int:
        """将聚合桶批量落库并执行保留期裁剪；返回写入桶数。"""
        async with self._lock:
            pending = self._buckets
            self._buckets = {}
        if not pending:
            return 0
        async with self._factory() as session:
            repo = SQLAlchemyOnlineTrendRepository(session)
            for minute_ts, online in sorted(pending.items()):
                await repo.upsert_minute(minute_ts=minute_ts, count=online)
            cutoff = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(
                days=self._retention_days
            )
            await repo.prune_before(cutoff)
            await session.commit()
        return len(pending)

    async def run_loop(
        self,
        interval_seconds: float,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """后台循环：每 interval_seconds 落库一次；异常告警并继续（不雪崩）。"""
        while True:
            await sleep(interval_seconds)
            try:
                n = await self.flush_and_prune()
                if n:
                    _logger.info("分钟桶批量落库 %d 桶", n)
            except Exception:  # noqa: BLE001 - 后台循环必须吞异常保持存活
                _logger.exception("分钟桶落库失败（下周期重试）")