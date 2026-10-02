"""告警 Webhook 外发（看板增强项 #10）：monitor.alert 状态转换 → 通用 JSON POST。

设计约束：
- 配置驱动：ERDOS_ALERT_WEBHOOK_URL 为空时整条链路关闭（默认 dev 零行为变化）；
- 旁路增强：外发失败只告警日志、绝不重抛 —— 监测主循环与看板告警不受渠道故障影响；
- 通用 payload（source/type/severity/metric/state/value/threshold/message/occurred_at），
  接收端（飞书/钉钉/企微自定义机器人）按模板适配；发送函数可注入（测试桩）。
"""

import asyncio
import json
import logging
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.config import Settings
from app.domain.monitoring.ports import AlertTransition

logger = logging.getLogger("erdos.alertwebhook")

# 发送函数端口：url / payload / 超时秒 → 本次投递是否成功（可注入测试桩）
SendFn = Callable[[str, dict[str, Any], float], Awaitable[bool]]


async def _post_json(url: str, payload: dict[str, Any], timeout_seconds: float) -> bool:
    """标准库异步投递（零新依赖）：POST JSON，2xx 视为成功。"""

    def _send() -> bool:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return 200 <= int(getattr(response, "status", 200)) < 300

    return await asyncio.to_thread(_send)


class AlertWebhookDispatcher:
    """告警外发调度：dispatch 为 fire-and-forget（不阻塞监测主循环）。"""

    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float = 3.0,
        retries: int = 2,
        backoff_seconds: float = 1.0,
        send_fn: SendFn = _post_json,
    ) -> None:
        self._url = (url or "").strip()
        self._timeout = timeout_seconds
        self._retries = max(0, retries)
        self._backoff = backoff_seconds
        self._send_fn = send_fn

    @classmethod
    def from_settings(cls, settings: Settings) -> "AlertWebhookDispatcher":
        return cls(
            settings.alert_webhook_url,
            timeout_seconds=settings.alert_webhook_timeout_seconds,
            retries=settings.alert_webhook_retries,
            backoff_seconds=settings.alert_webhook_backoff_seconds,
        )

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    def payload_for(self, transition: AlertTransition) -> dict[str, Any]:
        """状态转换 → 外发载荷（与看板事件载荷口径一致）。"""
        return {
            "source": "erdos-server",
            "type": "monitor.alert",
            "severity": "warning" if transition.state == "triggered" else "info",
            "metric": transition.metric,
            "state": transition.state,
            "value": transition.value,
            "threshold": transition.threshold,
            "message": transition.message,
            "occurred_at": transition.occurred_at.isoformat(),
        }

    def dispatch(self, transition: AlertTransition) -> None:
        """fire-and-forget 外发；未配置或失败均不影响监测主循环。"""
        if not self.enabled:
            return
        asyncio.create_task(self._send_guarded(transition))

    async def send(self, transition: AlertTransition) -> bool:
        """投递一次状态转换（指数退避重试）；耗尽重试仍失败返回 False。"""
        payload = self.payload_for(transition)
        for attempt in range(self._retries + 1):
            if attempt > 0:
                await asyncio.sleep(self._backoff * (2 ** (attempt - 1)))
            try:
                if await self._send_fn(self._url, payload, self._timeout):
                    return True
            except Exception:
                logger.warning(
                    "告警 Webhook 投递异常（第 %d/%d 次）：metric=%s state=%s",
                    attempt + 1,
                    self._retries + 1,
                    transition.metric,
                    transition.state,
                    exc_info=True,
                )
        logger.error(
            "告警 Webhook 投递失败（已重试 %d 次）：metric=%s url=%s",
            self._retries,
            transition.metric,
            self._url,
        )
        return False

    async def _send_guarded(self, transition: AlertTransition) -> None:
        """后台任务守卫：任何异常不外抛（外发为旁路增强）。"""
        try:
            await self.send(transition)
        except Exception:
            logger.exception("告警 Webhook 后台投递崩溃：metric=%s", transition.metric)