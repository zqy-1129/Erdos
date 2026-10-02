"""趋势指标领域端口与数据载体。"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TrendPoint:
    """单个趋势采样点。"""

    ts: datetime
    value: int


@dataclass(frozen=True, slots=True)
class TrendSeries:
    """下采样后的趋势序列（含范围元数据）。"""

    granularity: str
    start: datetime
    end: datetime
    points: list[TrendPoint]


@dataclass(frozen=True, slots=True)
class DailyStatPoint:
    """用户日快照点。"""

    stat_date: date
    total_users: int
    new_users: int
    active_users: int


class OnlineTrendRepository(Protocol):
    """在线分钟桶仓储端口。"""

    async def list_between(self, start: datetime, end: datetime) -> list[TrendPoint]:
        """返回 [start, end) 区间内的分钟采样点（升序）。"""
        ...

    async def upsert_minute(self, minute_ts: datetime, count: int) -> bool:
        """写入/更新某分钟的在线数（最新值覆盖）；返回是否新建了该分钟桶。"""
        ...

    async def prune_before(self, cutoff: datetime) -> int:
        """删除 minute_ts < cutoff 的分钟桶，返回删除行数。"""
        ...


class UsersTrendRepository(Protocol):
    """用户日快照仓储端口。"""

    async def list_between(self, start: date, end: date) -> list[DailyStatPoint]:
        """返回 [start, end] 区间内日快照（升序）。"""
        ...