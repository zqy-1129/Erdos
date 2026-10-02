"""验证码固定窗口限流器（SP2-7）：重发间隔 + 每日限额双维度防刷。

与网关滑动窗口 QPS 限流（infra/rate_limit.py）职责不同：
本组件面向验证码发送的「60s/次 + 日 10 次」固定窗口限流。
默认进程内实现；多实例部署由 infra/redis_state.RedisCodeWindowLimiter
提供跨进程计数（同签名，见 service.py 的 VerificationCodeLimiter 端口）。
"""

import time
from collections import defaultdict, deque
from collections.abc import Callable


class FixedWindowCodeLimiter:
    """验证码固定窗口限流器（进程内）：resend_seconds 间隔 + daily_limit 每日上限。

    clock 可注入（默认 time.monotonic）以支持测试。
    """

    def __init__(
        self,
        resend_seconds: int,
        daily_limit: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if resend_seconds < 1 or daily_limit < 1:
            raise ValueError("resend_seconds 必须 ≥1 且 daily_limit 必须 ≥1")
        self._resend = float(resend_seconds)
        self._daily = daily_limit
        self._clock = clock or time.monotonic
        self._last_send: dict[str, float] = {}
        self._daily_hits: dict[str, deque[float]] = defaultdict(deque)

    def _prune_daily(self, window: deque[float], now: float) -> None:
        day = 86400.0
        while window and now - window[0] >= day:
            window.popleft()

    async def allow(self, key: str) -> bool:
        now = self._clock()
        # 维度 1：重发间隔（60s）
        last = self._last_send.get(key)
        if last is not None and now - last < self._resend:
            return False
        # 维度 2：每日限额（10 次）
        hits = self._daily_hits[key]
        self._prune_daily(hits, now)
        if len(hits) >= self._daily:
            return False
        # 放行：记录
        self._last_send[key] = now
        hits.append(now)
        return True

    async def retry_after_seconds(self, key: str) -> float:
        now = self._clock()
        last = self._last_send.get(key)
        if last is not None and now - last < self._resend:
            return max(0.0, last + self._resend - now)
        hits = self._daily_hits[key]
        self._prune_daily(hits, now)
        if len(hits) >= self._daily:
            return max(0.0, hits[0] + 86400.0 - now)
        return 0.0
