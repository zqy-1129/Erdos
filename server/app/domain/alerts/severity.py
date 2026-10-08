"""告警分级与值班路由（SP4-3）：P0/P1/P2 三级 + 飞书/邮件/消息三通道。

对齐《服务端架构》SLO 告警分级：
- P0：可用性跌破 99.5% → 飞书即时；
- P1：错误率 > 1% 或 P99 > 500ms → 邮件；
- P2：对账差异非零 → 消息提醒。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final


class Severity(StrEnum):
    P0 = "P0"  # 可用性跌破 99.5%，飞书即时
    P1 = "P1"  # 错误率 > 1% 或 P99 > 500ms，邮件
    P2 = "P2"  # 对账差异非零，消息提醒


class Channel(StrEnum):
    FEISHU = "feishu"  # 飞书即时
    EMAIL = "email"  # 邮件
    MESSAGE = "message"  # 消息提醒


# 级别 → 路由通道
SEVERITY_CHANNEL: dict[Severity, Channel] = {
    Severity.P0: Channel.FEISHU,
    Severity.P1: Channel.EMAIL,
    Severity.P2: Channel.MESSAGE,
}


@dataclass(frozen=True, slots=True)
class Alert:
    """一条告警。"""

    severity: Severity
    metric: str
    message: str
    occurred_at: datetime


def classify_availability(uptime_ratio: float) -> Severity | None:
    """可用性分级：< 99.5% 为 P0。"""
    if uptime_ratio < 0.995:
        return Severity.P0
    return None


def classify_error_or_latency(error_rate: float, p99_ms: float) -> Severity | None:
    """错误率/延迟分级：错误率 > 1% 或 P99 > 500ms 为 P1。"""
    if error_rate > 0.01 or p99_ms > 500:
        return Severity.P1
    return None


def classify_reconcile_diff(diff_count: int) -> Severity | None:
    """对账差异分级：差异非零为 P2。"""
    if diff_count > 0:
        return Severity.P2
    return None


# 《服务端架构》§10 与 PRD 6.1 的承诺：云端可用性 99.5%。这是产品口径，不做成部署可调项。
SLO_AVAILABILITY: Final[float] = 0.995

# 采样指标 -> SLO 档位。可用性是唯一 P0；错误率与延迟类为 P1；容量水位为 P2。
# 延迟档按 P95 口径评估（PRD 写的是 P99>500ms），P99 需要监测表增列，
# 该替换口径已登记待契约/架构评审确认，不假装它就是 P99。
METRIC_SEVERITY: Final[dict[str, Severity]] = {
    "availability": Severity.P0,
    "error_rate": Severity.P1,
    "p95_ms": Severity.P1,
    "db_query_p95_ms": Severity.P1,
    "qps": Severity.P2,
    "cpu_percent": Severity.P2,
    "memory_percent": Severity.P2,
    "db_pool_usage": Severity.P2,
}


def severity_for_metric(metric: str) -> Severity:
    """采样指标所属 SLO 档位；未知指标按 P2 兜底（宁可多报不可漏报）。"""
    return METRIC_SEVERITY.get(metric, Severity.P2)


def availability_of(error_rate: float, requests_in_window: float, min_requests: float) -> float:
    """窗口可用性 = 1 - 5xx 占比；样本不足时返回 1.0（低流量下几个 5xx 不该触发 P0）。

    error_rate 的口径就是 5xx 占比（infra/monitoring 只把 status>=500 计为错误）。
    """
    if requests_in_window < min_requests:
        return 1.0
    return 1.0 - error_rate


class SilenceManager:
    """告警静默合并：同级别同指标在静默窗口内去重（只发一次）。"""

    def __init__(self, silence_window_seconds: int = 300) -> None:
        self._window = silence_window_seconds
        self._last_sent: dict[tuple[Severity, str], datetime] = {}

    def should_send(self, alert: Alert, now: datetime) -> bool:
        """是否应发送（静默窗口内同键告警去重）。"""
        key = (alert.severity, alert.metric)
        last = self._last_sent.get(key)
        if last is not None and now - last < timedelta(seconds=self._window):
            return False
        self._last_sent[key] = now
        return True


class AlertRouter:
    """值班路由：按级别路由到飞书/邮件/消息通道，静默去重。"""

    def __init__(self, silence: SilenceManager | None = None) -> None:
        self._silence = silence or SilenceManager()
        self._routed: list[tuple[Alert, Channel]] = []

    def route(self, alert: Alert, now: datetime) -> Channel | None:
        """路由一条告警；静默窗口内去重返回 None。"""
        if not self._silence.should_send(alert, now):
            return None
        channel = SEVERITY_CHANNEL[alert.severity]
        self._routed.append((alert, channel))
        return channel

    @property
    def routed(self) -> list[tuple[Alert, Channel]]:
        return self._routed
