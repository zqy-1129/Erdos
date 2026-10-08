"""业务告警端口（对账差异、资金差异这类非采样阈值告警的对外出口）。

为什么单独一层：domain 不许碰 broker/webhook（分层红线），但"差异必告警不可静默"是领域要求。
端口在这里，扇出实现在 infra/alert_outlet.py。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.domain.alerts.severity import Severity


@dataclass(frozen=True, slots=True)
class BusinessAlert:
    """一条业务告警：``key`` 进看板事件 payload.metric，``message`` 为「状态+原因」短句。"""

    key: str
    severity: Severity
    message: str
    value: float
    threshold: float = 0.0


class AlertOutlet(Protocol):
    """业务告警出口。"""

    async def emit(self, alert: BusinessAlert, now: datetime) -> bool:
        """外发一条告警；返回是否实际发出（静默窗口内去重返回 False）。"""
        ...
