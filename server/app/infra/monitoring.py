"""运行监测采集器（看板运维域）：HTTP/SQL 延迟、系统资源与窗口聚合。

职责边界：
- 仅采集与聚合进程内事实；不落库、不推送 SSE（由采样器编排）；
- 写路径均发生在 asyncio 事件循环内，deque/dict 追加无 await 交叉点，天然原子；
- 接口频率按「两次快照之间」的计数增量计算，不做长期累积；
- 分位数基于窗口内排序样本的线性插值，窗口单调推进、样本有界。
"""

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, TypeVar

import psutil

DEFAULT_BUCKET_SIZE: Final = 20_000
ERROR_STATUS_THRESHOLD: Final = 500  # 5xx 计为错误（4xx 属于客户端语义，不计）

_TS = TypeVar("_TS", bound=tuple[Any, ...])


def percentile(sorted_values: list[float], p: float) -> float:
    """线性插值分位数（0<p<=100）；空序列返回 0.0。"""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (p / 100.0) * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


@dataclass(frozen=True, slots=True)
class PathStats:
    """单个接口在窗口内的调用统计。"""

    path: str
    requests: int


@dataclass(frozen=True, slots=True)
class RawSnapshot:
    """窗口内聚合后的原始指标（单位：ms / 比率 / req/s）。"""

    qps: float
    error_rate: float
    p50_ms: float
    p95_ms: float
    db_query_p95_ms: float
    paths: tuple[PathStats, ...]
    window_seconds: float


class MonitoringCollector:
    """进程内指标采集：HTTP 请求、SQL 查询、进程资源。

    - `_latencies` / `_queries`：带单调时钟时间戳的样本环（窗口过滤基于时间而非条数）；
    - `_path_counts`：路径累计计数（单调递增），快照时取增量即窗口内调用量。
    """

    def __init__(self, bucket_size: int = DEFAULT_BUCKET_SIZE) -> None:
        if bucket_size < 1:
            raise ValueError("bucket_size 必须 ≥1")
        self._latencies: deque[tuple[float, float, bool]] = deque(maxlen=bucket_size)
        self._queries: deque[tuple[float, float]] = deque(maxlen=bucket_size)
        self._path_counts: dict[str, int] = {}
        self._last_path_snapshot: dict[str, int] = {}
        self._process = psutil.Process()
        self._process.cpu_percent(None)  # 首次调用建立基线

    def record_http(self, path: str, duration_s: float, status: int) -> None:
        """HTTP 请求完成时记录（中间件调用）。"""
        self._latencies.append((time.perf_counter(), duration_s, status >= ERROR_STATUS_THRESHOLD))
        self._path_counts[path] = self._path_counts.get(path, 0) + 1

    def record_query(self, duration_s: float) -> None:
        """SQL 语句执行完成时记录（引擎事件钩子调用）。"""
        self._queries.append((time.perf_counter(), duration_s))

    def system_metrics(self) -> tuple[float, float, float]:
        """进程资源：CPU%、内存%（占全机）、RSS（MB）。"""
        return (
            self._process.cpu_percent(None),
            self._process.memory_percent(),
            self._process.memory_info().rss / (1024 * 1024),
        )

    def snapshot(self, window_seconds: float, now: Callable[[], float] | None = None) -> RawSnapshot:
        """按滑动窗口聚合：QPS、错误率、P50/P95、SQL P95 与路径频率。"""
        if window_seconds <= 0:
            raise ValueError("window_seconds 必须 >0")
        clock = now or time.perf_counter
        cutoff = clock() - window_seconds

        def prune(samples: deque[_TS], cutoff: float) -> None:
            while samples and samples[0][0] < cutoff:
                samples.popleft()

        prune(self._latencies, cutoff)
        prune(self._queries, cutoff)

        durations = sorted(duration_ms(s) for s in self._latencies)
        errors = sum(1 for s in self._latencies if s[2])
        query_ms = sorted(s[1] * 1000 for s in self._queries)

        counts = dict(self._path_counts)
        delta = {
            path: counts.get(path, 0) - self._last_path_snapshot.get(path, 0)
            for path in set(counts) | set(self._last_path_snapshot)
            if counts.get(path, 0) != self._last_path_snapshot.get(path, 0)
        }
        self._last_path_snapshot = counts

        total = len(self._latencies)
        return RawSnapshot(
            qps=total / window_seconds,
            error_rate=(errors / total) if total else 0.0,
            p50_ms=percentile(durations, 50.0),
            p95_ms=percentile(durations, 95.0),
            db_query_p95_ms=percentile(query_ms, 95.0),
            paths=tuple(
                PathStats(path=path, requests=count)
                for path, count in sorted(delta.items(), key=lambda item: item[1], reverse=True)[:10]
            ),
            window_seconds=window_seconds,
        )


def duration_ms(sample: tuple[float, ...]) -> float:
    """样本 (ts, seconds, ...) 的耗时转毫秒。"""
    return sample[1] * 1000


_collector: MonitoringCollector | None = None


def set_collector(collector: MonitoringCollector) -> None:
    """装配全局采集器（create_app 时调用；进程内单实例）。"""
    global _collector
    _collector = collector


def get_collector() -> MonitoringCollector:
    """取全局采集器（SQL 引擎事件钩子等无 scope 上下文的位置使用）。"""
    if _collector is None:
        raise RuntimeError("MonitoringCollector 尚未装配")
    return _collector