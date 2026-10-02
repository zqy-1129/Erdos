"""通知发送器（dev/test 日志实现，SP2-7）。

生产环境替换为 SMTP/短信服务商实现（NotificationSender 端口不变）。
"""

import logging

from app.domain.notification.ports import NotificationSender

logger = logging.getLogger("erdos.notification")


class LogNotificationSender(NotificationSender):
    """dev/test 通知发送器：写日志并在内存留痕（联调/测试读取）。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, channel: str, template_id: str, target: str, payload: dict) -> None:
        record = {
            "channel": channel,
            "template_id": template_id,
            "target": target,
            "payload": payload,
        }
        self.sent.append(record)
        logger.info("通知已发送（dev 渠道）：channel=%s template=%s target=%s", channel, template_id, target)
