"""运行监测领域端口、指标定义与数据载体（看板运维域）。

- 指标定义（METRICS）是契约：列名、单位、聚合口径与展示因子统一在此登记，
  采样落库、趋势接口、告警评估、前端展示四方共用；
- 口径约定：error_rate / db_pool_usage 存储为 0~1 比率，展示为百分比（factor=100）；
  cpu/mem 存储即百分比；趋势下采样：计数/比率类取 avg，延迟类取 max（峰值口径）。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Protocol

from app.core.config import Settings
from app.infra.monitoring import PathStats


@dataclass(frozen=True, slots=True)
class MetricDef:
    """单一监测指标定义。"""

    key: str
    label: str
    unit: str
    column: str
    agg: str  # avg | max（下采样口径）
    factor: float = 1.0  # 存储值 -> 展示值 换算系数


METRICS: tuple[MetricDef, ...] = (
    MetricDef("qps", "请求吞吐", "req/s", "qps", "avg"),
    MetricDef("p50_ms", "P50 延迟", "ms", "p50_ms", "avg"),
    MetricDef("p95_ms", "P95 延迟", "ms", "p95_ms", "max"),
    MetricDef("error_rate", "错误率", "%", "error_rate", "avg", factor=100.0),
    MetricDef("cpu_percent", "CPU 使用率", "%", "cpu_percent", "avg"),
    MetricDef("memory_percent", "内存使用率", "%", "memory_percent", "avg"),
    MetricDef("db_query_p95_ms", "数据库查询 P95", "ms", "db_query_p95_ms", "max"),
    MetricDef("db_pool_usage", "连接池占用", "%", "db_pool_usage", "avg", factor=100.0),
)
METRIC_BY_KEY: dict[str, MetricDef] = {m.key: m for m in METRICS}


@dataclass(frozen=True, slots=True)
class MonitoringSample:
    """一次采样聚合后的全量指标（原始口径：比率 0~1、ms、百分比）。"""

    sampled_at: datetime
    qps: float
    error_rate: float
    p50_ms: float
    p95_ms: float
    cpu_percent: float
    memory_percent: float
    rss_mb: float
    db_query_p95_ms: float
    db_pool_usage: float
    paths: tuple[PathStats, ...] = ()
    window_seconds: float = 60.0

    def value_of(self, key: str) -> float:
        return getattr(self, key)


@dataclass(frozen=True, slots=True)
class AlertThresholds:
    """告警阈值集合（原始口径：错误率为比率，其余同展示单位）。

    KEYS 限定「有告警规则」的指标键：p50 等纯观测指标不设阈值，不出现在展示阈值里。
    """

    KEYS: ClassVar[tuple[str, ...]] = (
        "p95_ms",
        "error_rate",
        "qps",
        "cpu_percent",
        "memory_percent",
        "db_query_p95_ms",
        "db_pool_usage",
    )

    p95_ms: float
    error_rate: float
    qps: float
    cpu_percent: float
    memory_percent: float
    db_query_p95_ms: float
    db_pool_usage: float

    @classmethod
    def from_settings(cls, settings: Settings) -> "AlertThresholds":
        return cls(
            p95_ms=settings.alert_p95_ms,
            error_rate=settings.alert_error_rate,
            qps=settings.alert_qps,
            cpu_percent=settings.alert_cpu_percent,
            memory_percent=settings.alert_memory_percent,
            db_query_p95_ms=settings.alert_db_p95_ms,
            db_pool_usage=settings.alert_db_pool_usage,
        )

    def value_of(self, key: str) -> float:
        return getattr(self, key)


@dataclass(frozen=True, slots=True)
class MonitoringPoint:
    """单指标分钟点（存储口径原值，浮点）。"""

    ts: datetime
    value: float


@dataclass(frozen=True, slots=True)
class MonitoringTrendSeries:
    """单指标下采样序列。"""

    granularity: str
    start: datetime
    end: datetime
    points: list[MonitoringPoint]


@dataclass(frozen=True, slots=True)
class AlertTransition:
    """一次告警状态转换（触发或恢复）。"""

    metric: str
    state: str  # triggered | recovered
    value: float
    threshold: float
    occurred_at: datetime

    @property
    def message(self) -> str:
        labels = {m.key: m.label for m in METRICS}
        units = {m.key: m.unit for m in METRICS}
        label = labels.get(self.metric, self.metric)
        unit = units.get(self.metric, "")
        action = "超过阈值" if self.state == "triggered" else "回落到阈值内"
        return f"{label} {self.value:.2f}{unit} {action} {self.threshold:.2f}{unit}"


class MonitoringTrendRepository(Protocol):
    """监测分钟快照仓储端口。"""

    async def upsert_minute(self, minute_ts: datetime, sample: MonitoringSample) -> None:
        """写入/覆盖该分钟的聚合快照。"""
        ...

    async def list_between(
        self, metric_key: str, start: datetime, end: datetime
    ) -> list[MonitoringPoint]:
        """查询单指标在时间范围内的分钟序列（存储口径原值）。"""
        ...

    async def prune_before(self, cutoff: datetime) -> int:
        """删除截止时间之前的快照，返回删除行数。"""
        ...


def display_values(sample: MonitoringSample) -> dict[str, float]:
    """采样 -> 展示口径（百分比/单位换算），供 SSE 负载与看板渲染。"""
    return {
        m.key: round(sample.value_of(m.key) * m.factor, 3) for m in METRICS
    }


def display_thresholds(thresholds: AlertThresholds) -> dict[str, float]:
    """阈值 -> 展示口径（仅包含有告警规则的指标键）。"""
    return {
        key: round(thresholds.value_of(key) * METRIC_BY_KEY[key].factor, 3)
        for key in AlertThresholds.KEYS
    }