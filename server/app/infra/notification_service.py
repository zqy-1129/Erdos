"""通知用例装配（infra 组装域服务与仓储）：API 入口与调度任务共用一份构造口径。

为什么单独一层：`NotificationService` 的四个依赖此前只在 `api/v1/notifications.py` 里拼一次，
调度任务要复用就得再拼一遍——参数一改就漏改一处，正是月赠/对账那条链路踩过的坑。
domain 不许 import repository，所以装配放在这里。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.notification.ports import (
    NotificationSender,
    VerificationCodeLimiter,
)
from app.domain.notification.service import NotificationService
from app.repository.notification import SQLAlchemyNotificationLogRepository


def build_notification_service(
    session: AsyncSession,
    sender: NotificationSender,
    code_limiter: VerificationCodeLimiter,
    settings: Settings,
) -> NotificationService:
    """组装续费提醒/验证码共用的通知用例（同一条幂等与限流实现）。"""
    return NotificationService(
        SQLAlchemyNotificationLogRepository(session),
        sender,
        code_limiter,
        settings,
    )
