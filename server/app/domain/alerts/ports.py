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
    """一条业务告警：``key`` 进事件 payload.metric，``message`` 为「状态+原因」短句。

    ``state`` 为 recovered 时表示恢复通知：不去重、事件按 info 着色，但仍外发渠道
    （值班需要知道什么时候好的）。
    """

    key: str
    severity: Severity
    message: str
    value: float
    threshold: float = 0.0
    state: str = "triggered"


class AlertOutlet(Protocol):
    """业务告警出口。"""

    async def emit(self, alert: BusinessAlert, now: datetime, dedupe: bool = True) -> bool:
        """外发一条告警；返回是否实际发出。

        dedupe=True 时同级别同指标在静默窗口内只发一次；调用方自带状态机去重（例如采样
        告警只在状态翻转时产出）时传 False，否则恢复后 5 分钟内再次触发会被静默吞掉。
        """
        ...
