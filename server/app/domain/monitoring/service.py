"""运行监测领域服务：告警阈值评估与趋势下采样（纯逻辑，无 IO）。

- 告警规则：每个指标一条「超阈值」判定；评估器记忆当前活跃告警集合，
  仅在集合发生增删时产出转换事件（触发/恢复），避免每周期重复告警；
- 趋势下采样：计数/比率类指标桶内取 mean、延迟类取 max（与 METRICS.agg 一致）。
"""

from collections.abc import Callable
from datetime import UTC, datetime

from app.domain.alerts.severity import availability_of
from app.domain.monitoring.ports import (
    AlertThresholds,
    AlertTransition,
    MonitoringPoint,
    MonitoringSample,
    MonitoringTrendSeries,
)


class AlertEvaluator:
    """告警状态机：活跃集合 diff -> 转换事件流。

    规则含方向：延迟/错误率/容量是"超阈值"触发，可用性是"跌破阈值"触发。
    可用性带最小样本门（低流量窗口里几个 5xx 就跌破 99.5% 属于统计噪声，不该叫醒值班）。
    """

    def __init__(
        self,
        thresholds: AlertThresholds,
        *,
        availability_target: float = 0.995,
        availability_min_requests: float = 50.0,
    ) -> None:
        gt = "gt"
        rules: list[tuple[str, Callable[[MonitoringSample], float], float, str]] = [
            (metric, getter, thresholds.value_of(metric), gt)
            for metric, getter in (
                ("p95_ms", lambda s: s.p95_ms),
                ("error_rate", lambda s: s.error_rate),
                ("qps", lambda s: s.qps),
                ("cpu_percent", lambda s: s.cpu_percent),
                ("memory_percent", lambda s: s.memory_percent),
                ("db_query_p95_ms", lambda s: s.db_query_p95_ms),
                ("db_pool_usage", lambda s: s.db_pool_usage),
            )
        ]
        rules.append(
            (
                "availability",
                lambda s: availability_of(
                    s.error_rate, s.qps * s.window_seconds, availability_min_requests
                ),
                availability_target,
                "lt",
            )
        )
        self._rules = rules
        self._active: set[str] = set()

    @property
    def active(self) -> frozenset[str]:
        return frozenset(self._active)

    def active_view(self) -> dict[str, str]:
        """活跃告警视图（metric -> 文案），供 overview 即时读取。"""
        thresholds = {metric: (getter, threshold, cmp) for metric, getter, threshold, cmp in self._rules}
        out: dict[str, str] = {}
        for metric in sorted(self._active):
            _, threshold, cmp = thresholds[metric]
            relation = "超过" if cmp == "gt" else "跌破"
            out[metric] = f"{relation}阈值 {threshold:g}"
        return out

    def evaluate(self, sample: MonitoringSample) -> list[AlertTransition]:
        """评估一次采样；返回新增触发/恢复的转换事件。"""
        triggered = {
            metric
            for metric, getter, threshold, cmp in self._rules
            if (getter(sample) > threshold if cmp == "gt" else getter(sample) < threshold)
        }
        transitions: list[AlertTransition] = []
        for metric in sorted(triggered - self._active):
            transitions.append(self._transition(metric, sample, "triggered"))
        for metric in sorted(self._active - triggered):
            transitions.append(self._transition(metric, sample, "recovered"))
        self._active = triggered
        return transitions

    def _transition(
        self, metric: str, sample: MonitoringSample, state: str
    ) -> AlertTransition:
        getter, threshold, _ = next(
            (g, t, c) for m, g, t, c in self._rules if m == metric
        )
        return AlertTransition(
            metric=metric,
            state=state,
            value=round(getter(sample), 4),
            threshold=round(threshold, 4),
            occurred_at=sample.sampled_at,
        )


class MonitoringTrendService:
    """单指标分钟序列下采样：avg（计数/比率）或 max（延迟峰值）。"""

    GRANULARITIES: dict[str, int] = {
        "1m": 60,
        "5m": 300,
        "1h": 3600,
    }

    def build(
        self,
        raw: list[MonitoringPoint],
        granularity: str,
        start: datetime,
        end: datetime,
        agg: str,
        factor: float = 1.0,
    ) -> MonitoringTrendSeries:
        """按粒度聚合分钟点（口径随 agg），输出展示单位（含 factor 换算）。"""
        bucket = self.GRANULARITIES.get(granularity)
        if bucket is None:
            raise ValueError(f"不支持的粒度：{granularity}（可选 1m/5m/1h）")
        if agg not in ("avg", "max"):
            raise ValueError(f"不支持的聚合口径：{agg}（可选 avg/max）")
        start = _normalize(start)
        end = _normalize(end)
        buckets: dict[int, tuple[float, int]] = {}
        for point in raw:
            ts = _normalize(point.ts)
            if not (start <= ts <= end):
                continue
            key = int(ts.timestamp()) // bucket
            value, count = buckets.get(key, (0.0, 0))
            if agg == "avg":
                buckets[key] = (value + point.value, count + 1)
            else:
                buckets[key] = (max(value, point.value), count + 1)
        points = [
            MonitoringPoint(
                ts=_from_epoch(key * bucket),
                value=round(
                    (value / count if agg == "avg" else value) * factor, 3
                ),
            )
            for key, (value, count) in sorted(buckets.items())
        ]
        return MonitoringTrendSeries(
            granularity=granularity, start=start, end=end, points=points
        )


def _normalize(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _from_epoch(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, tz=UTC)