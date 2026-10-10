"""Prometheus 指标定义与暴露（研发手册 SP2-1 网关指标）。

指标命名统一 `erdos_*`，path 标签取路由模板（route.path）以控制基数，
避免把带参数的原始路径打进标签造成指标爆炸。
性能目标（P95 < 200ms）通过 request_duration 直方图 bucket 覆盖观测。
"""

from collections.abc import Mapping
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.responses import Response

# 网关指标
REQUEST_TOTAL = Counter(
    "erdos_http_requests_total",
    "HTTP 请求总数",
    labelnames=["method", "path", "status"],
)
REQUEST_DURATION = Histogram(
    "erdos_http_request_duration_seconds",
    "HTTP 请求耗时（秒）",
    labelnames=["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.5, 1.0, 2.5, 5.0),
)
REQUEST_IN_FLIGHT = Gauge(
    "erdos_http_requests_in_flight",
    "当前处理中的请求数",
)

# 运行期健康计数：进程内已有的两处计数（值班路由、总线消费者失败）在抓取时刷进指标。
# 命名为 *_total 且单调递增（语义是计数器），但用 Gauge 导出——真源在各自对象里，
# 这里只做镜像，避免同一份计数记两遍而对不上。标签基数有界：通道 3 个、主题为已注册主题。
ALERT_ROUTED_TOTAL = Gauge(
    "erdos_alert_routed_total",
    "已路由告警数（按值班通道）",
    labelnames=["channel"],
)
BUS_DELIVERY_FAILURES_TOTAL = Gauge(
    "erdos_bus_delivery_failures_total",
    "消息消费者异常累计（按主题）",
    labelnames=["topic"],
)


def export_runtime_counters(state: Any) -> None:
    """把进程内计数刷进 Prometheus 指标（抓取时调用）。

    为什么挂在抓取而不是采样循环上：监测可以关（`monitoring_enabled=false`），
    但告警路由与总线失败和监测无关——关掉监测不该让这两条可观测性一起消失。
    取属性一律走 getattr 兜底：`/metrics` 可能在 lifespan 装配完成前被抓到，
    此时返回 500 比少两条指标糟糕得多。
    """
    counts = getattr(getattr(state, "alert_outlet", None), "routed_counts", None)
    for channel, total in (counts or {}).items():
        ALERT_ROUTED_TOTAL.labels(channel=str(channel)).set(total)
    failures = getattr(getattr(state, "message_bus", None), "delivery_failures", None)
    for topic, total in (failures or {}).items():
        BUS_DELIVERY_FAILURES_TOTAL.labels(topic=str(topic)).set(total)


def route_path_label(scope: Mapping[str, Any]) -> str:
    """取路由模板作为 path 标签；未匹配到路由时返回 'unmatched' 防基数爆炸。"""
    route = scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else "unmatched"


def metrics_response() -> Response:
    """Prometheus 抓取端点响应。"""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)