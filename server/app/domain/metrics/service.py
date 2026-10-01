"""趋势领域服务：粒度下采样（桶内取 max，反映并发峰值口径）。

在线数为"当前值"采样，向下聚合取区间最大值是并发视角的正确口径，
与 FR-1 在线统计保持一致（文档化约定，禁止改为均值等其它口径）。
"""

from datetime import UTC, datetime, timedelta

from app.domain.metrics.ports import TrendPoint, TrendSeries


class TrendService:
    """在线趋势下采样编排（无 SQL）。"""

    GRANULARITIES: dict[str, int] = {
        "1m": 60,
        "5m": 300,
        "1h": 3600,
    }

    @classmethod
    def bucket_seconds(cls, granularity: str) -> int:
        if granularity not in cls.GRANULARITIES:
            raise ValueError(f"不支持的粒度：{granularity}（可选 1m/5m/1h）")
        return cls.GRANULARITIES[granularity]

    def build(
        self,
        raw: list[TrendPoint],
        granularity: str,
        start: datetime,
        end: datetime,
    ) -> TrendSeries:
        """按粒度聚合原生分钟点（桶内 max），返回升序序列。"""
        bucket = self.bucket_seconds(granularity)
        start = _normalize(start)
        end = _normalize(end)
        buckets: dict[int, int] = {}
        for point in raw:
            ts = _normalize(point.ts)
            if not (start <= ts <= end):
                continue
            key = int(ts.timestamp()) // bucket
            buckets[key] = max(buckets.get(key, 0), point.value)
        points = [
            TrendPoint(ts=_from_epoch(key * bucket), value=value)
            for key, value in sorted(buckets.items())
        ]
        return TrendSeries(granularity=granularity, start=start, end=end, points=points)


def _normalize(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _from_epoch(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, tz=UTC)


def default_start(end: datetime, span: timedelta) -> datetime:
    return end - span