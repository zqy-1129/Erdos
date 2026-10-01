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


def route_path_label(scope: Mapping[str, Any]) -> str:
    """取路由模板作为 path 标签；未匹配到路由时返回 'unmatched' 防基数爆炸。"""
    route = scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else "unmatched"


def metrics_response() -> Response:
    """Prometheus 抓取端点响应。"""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)