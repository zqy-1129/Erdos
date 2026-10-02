"""Redis 跨进程状态实现测试（SP2-7 多实例迁移）。

内存 FakeRedis 实现 _RedisOps 窄接口（带假时钟与 TTL 语义），
覆盖 RedisLoginLockout / RedisCodeWindowLimiter 行为、TTL 到期与降级路径；
不依赖真实 redis 服务与 redis 包。
"""

from app.core.config import Settings
from app.domain.auth.lockout import LoginLockout
from app.infra.redis_state import (
    RedisCodeWindowLimiter,
    RedisLoginLockout,
    build_code_limiter,
    build_lockout,
    build_redis_client,
)
from app.infra.verification_limiter import FixedWindowCodeLimiter

_DAY_MS = 86400 * 1000


class FakeRedis:
    """进程内假 Redis：按毫秒假时钟实现窄接口的 TTL 语义。"""

    def __init__(self) -> None:
        self.now_ms = 0
        self._store: dict[str, str] = {}
        self._expire: dict[str, int] = {}

    def advance(self, ms: int) -> None:
        self.now_ms += ms

    def _drop_if_expired(self, key: str) -> None:
        expire = self._expire.get(key)
        if expire is not None and expire <= self.now_ms:
            self._store.pop(key, None)
            self._expire.pop(key, None)

    async def incr(self, key: str) -> int:
        self._drop_if_expired(key)
        value = int(self._store.get(key, 0)) + 1
        self._store[key] = str(value)
        return value

    async def decr(self, key: str) -> int:
        self._drop_if_expired(key)
        value = int(self._store.get(key, 0)) - 1
        self._store[key] = str(value)
        return value

    async def pexpire(self, key: str, ms: int) -> None:
        if key in self._store:
            self._expire[key] = self.now_ms + ms

    async def get(self, key: str) -> str | None:
        self._drop_if_expired(key)
        return self._store.get(key)

    async def set(self, key: str, value: str, px: int) -> None:
        self._store[key] = value
        self._expire[key] = self.now_ms + px

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)
        self._expire.pop(key, None)

    async def pttl(self, key: str) -> int:
        self._drop_if_expired(key)
        if key not in self._store:
            return -2
        expire = self._expire.get(key)
        return expire - self.now_ms if expire is not None else -1


class BrokenRedis(FakeRedis):
    """模拟连接故障：所有操作抛 ConnectionError。"""

    async def _boom(self) -> None:
        raise ConnectionError("redis down")

    async def incr(self, key: str) -> int:
        await self._boom()

    async def get(self, key: str) -> str | None:
        await self._boom()

    async def set(self, key: str, value: str, px: int) -> None:
        await self._boom()


# ---------- RedisLoginLockout ----------

async def test_redis_lockout_threshold_and_dimensions() -> None:
    lockout = RedisLoginLockout(threshold=5, lock_seconds=900, client=FakeRedis())
    for _ in range(5):
        await lockout.register_failure("alice", "1.2.3.4")
    assert await lockout.is_locked("alice", "1.2.3.4") is True
    assert await lockout.is_locked("bob", "1.2.3.4") is True  # IP 维度
    await lockout.clear_account("alice")
    assert await lockout.is_locked("alice", "1.2.3.4") is True  # IP 维度保留


async def test_redis_lockout_ttl_expiry() -> None:
    client = FakeRedis()
    lockout = RedisLoginLockout(threshold=5, lock_seconds=900, client=client)
    for _ in range(5):
        await lockout.register_failure("alice", "1.2.3.4")
    assert await lockout.is_locked("alice", "1.2.3.4") is True
    client.advance(901 * 1000)  # 静默超过锁定窗口 -> 计数过期
    assert await lockout.is_locked("alice", "1.2.3.4") is False


async def test_redis_lockout_below_threshold_not_locked() -> None:
    lockout = RedisLoginLockout(threshold=5, lock_seconds=900, client=FakeRedis())
    for _ in range(4):
        await lockout.register_failure("alice", "1.2.3.4")
    assert await lockout.is_locked("alice", "1.2.3.4") is False


# ---------- RedisCodeWindowLimiter ----------

async def test_redis_code_limiter_resend_and_daily() -> None:
    client = FakeRedis()
    limiter = RedisCodeWindowLimiter(resend_seconds=60, daily_limit=3, client=client)
    assert await limiter.allow("13800000000") is True
    assert await limiter.allow("13800000000") is False  # 60s 内
    retry = await limiter.retry_after_seconds("13800000000")
    assert retry > 0 and retry <= 60.0
    client.advance(61 * 1000)
    assert await limiter.allow("13800000000") is True
    client.advance(61 * 1000)
    assert await limiter.allow("13800000000") is True  # 日第 3 次
    client.advance(61 * 1000)
    assert await limiter.allow("13800000000") is False  # 日限额用尽
    retry = await limiter.retry_after_seconds("13800000000")
    assert retry > 61.0  # 等待到日窗口滚动


async def test_redis_code_limiter_day_window_rolls() -> None:
    client = FakeRedis()
    limiter = RedisCodeWindowLimiter(resend_seconds=1, daily_limit=2, client=client)
    assert await limiter.allow("k") is True
    client.advance(2000)
    assert await limiter.allow("k") is True
    client.advance(2000)
    assert await limiter.allow("k") is False
    client.advance(_DAY_MS)  # 日窗滚动后恢复
    assert await limiter.allow("k") is True


# ---------- 降级与构建器 ----------

async def test_degradation_fail_open() -> None:
    """Redis 故障：防爆破视为未锁定、限流放行（可用性优先，不抛异常）。"""
    lockout = RedisLoginLockout(threshold=5, lock_seconds=900, client=BrokenRedis())
    limiter = RedisCodeWindowLimiter(resend_seconds=60, daily_limit=3, client=BrokenRedis())
    assert await lockout.is_locked("alice", "1.2.3.4") is False
    await lockout.register_failure("alice", "1.2.3.4")  # 不抛
    await lockout.clear_account("alice")  # 不抛
    assert await limiter.allow("k") is True
    assert await limiter.retry_after_seconds("k") == 0.0


async def test_builders_fallback_to_in_process() -> None:
    """未配置 redis_url：构建器返回进程内实现。"""
    config = Settings(env="test", database_url="sqlite+aiosqlite:///:memory:", redis_url=None)
    assert build_redis_client(config.redis_url) is None
    lockout = build_lockout(config, None)
    limiter = build_code_limiter(config, None)
    assert isinstance(lockout, LoginLockout)
    assert isinstance(limiter, FixedWindowCodeLimiter)


async def test_builders_redis_when_client_present() -> None:
    """配置 redis_url 且客户端可用：返回 Redis 实现。"""
    config = Settings(env="test", database_url="sqlite+aiosqlite:///:memory:", redis_url="redis://x")
    client = FakeRedis()
    assert isinstance(build_lockout(config, client), RedisLoginLockout)
    assert isinstance(build_code_limiter(config, client), RedisCodeWindowLimiter)