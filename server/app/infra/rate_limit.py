"""网关限流组件。

通过 RateLimiter 协议抽象能力边界：当前实现为进程内滑动窗口（单实例适用），
多副本部署时可无侵入替换为 Redis 等共享存储实现。
"""

from collections import defaultdict, deque
from collections.abc import Callable
from typing import Protocol

MonotonicClock = Callable[[], float]


class RateLimiter(Protocol):
    """限流器协议（infra 能力端口）。"""

    def allow(self, key: str) -> bool:
        """尝试放行一次请求；返回 False 表示触发限流。"""
        ...

    def retry_after_seconds(self, key: str) -> float:
        """返回该 key 下次可放行还需等待的秒数（未触发限流时为 0）。"""
        ...


class SlidingWindowRateLimiter:
    """进程内滑动窗口限流器。

    `clock` 可注入（默认 time.monotonic）以支持时钟精度与测试；
    不依赖系统墙钟，避免时区/校时抖动影响限流判定。
    """

    def __init__(
        self,
        max_requests: int,
        window_seconds: float,
        clock: MonotonicClock | None = None,
    ) -> None:
        if max_requests < 1 or window_seconds <= 0:
            raise ValueError("max_requests 必须 ≥1 且 window_seconds 必须 >0")
        self._max = max_requests
        self._window = float(window_seconds)
        self._clock: MonotonicClock = clock or _default_clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, window: deque[float], now: float) -> None:
        while window and now - window[0] >= self._window:
            window.popleft()

    def allow(self, key: str) -> bool:
        now = self._clock()
        window = self._hits[key]
        self._prune(window, now)
        if len(window) >= self._max:
            return False
        window.append(now)
        return True

    def retry_after_seconds(self, key: str) -> float:
        now = self._clock()
        window = self._hits[key]
        self._prune(window, now)
        if not window or len(window) < self._max:
            return 0.0
        return max(0.0, window[0] + self._window - now)


def _default_clock() -> float:
    import time

    return time.monotonic()


def client_ip_key(ip: str | None) -> str:
    """构造限流键。"""
    return f"ip:{ip or 'unknown'}"