"""滑动窗口限流器单元测试（注入式时钟）。"""

import pytest

from app.infra.rate_limit import SlidingWindowRateLimiter, client_ip_key


class FakeClock:
    """可手动推进的单调时钟。"""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def __call__(self) -> float:
        return self._now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def test_allow_within_capacity(clock: FakeClock) -> None:
    limiter = SlidingWindowRateLimiter(max_requests=3, window_seconds=60, clock=clock)
    assert limiter.allow("k") and limiter.allow("k") and limiter.allow("k")
    assert not limiter.allow("k"), "超过容量应被拒绝"


def test_retry_after_and_slide(clock: FakeClock) -> None:
    limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=60, clock=clock)
    limiter.allow("k")
    clock.advance(10)
    limiter.allow("k")
    assert limiter.retry_after_seconds("k") == pytest.approx(50.0)
    # 窗口滑过最老一条记录后恢复放行；新请求又把窗口填满（剩余等待 9s）
    clock.advance(51)
    assert limiter.allow("k")
    assert limiter.retry_after_seconds("k") == pytest.approx(9.0)


def test_retry_after_zero_when_not_limited(clock: FakeClock) -> None:
    limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=60, clock=clock)
    assert limiter.retry_after_seconds("k") == 0.0
    limiter.allow("k")
    assert limiter.retry_after_seconds("k") == pytest.approx(60.0)


def test_keys_are_isolated(clock: FakeClock) -> None:
    limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=60, clock=clock)
    assert limiter.allow("a")
    assert limiter.allow("b"), "不同 key 互不影响"


def test_invalid_config_rejected() -> None:
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(max_requests=0, window_seconds=60)
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(max_requests=1, window_seconds=0)


def test_client_ip_key() -> None:
    assert client_ip_key("1.2.3.4") == "ip:1.2.3.4"
    assert client_ip_key(None) == "ip:unknown"