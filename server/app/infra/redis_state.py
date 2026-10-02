"""Redis 跨进程状态实现与构建器（SP2-7 多实例迁移：防爆破 + 验证码限流）。

- 默认（未配置 ERDOS_REDIS_URL）：返回进程内实现（LoginLockout / FixedWindowCodeLimiter），
  行为完全不变；
- 配置后：RedisLoginLockout / RedisCodeWindowLimiter 以相同异步签名提供跨实例计数；
- 降级策略：Redis 不可用时记录告警并「失败开放」（防爆破视为未锁定、限流放行），
  保证可用性优先——凭据校验等主防线不受影响。
redis 包为可选依赖（惰性导入），未安装时自动回退进程内并告警一次。
"""

import time
from typing import Protocol

from app.core.config import Settings
from app.core.logging import get_logger
from app.domain.auth.lockout import LockoutGuard, LoginLockout
from app.domain.notification.ports import VerificationCodeLimiter
from app.infra.verification_limiter import FixedWindowCodeLimiter

_logger = get_logger("erdos.redis_state")

# Redis 键格式（与登录防爆破进程内实现保持一致）
_ACCOUNT_PREFIX = "acct:"
_IP_PREFIX = "ip:"
_CODE_LAST_PREFIX = "vcode:last:"
_CODE_DAY_PREFIX = "vcode:day:"

_SECONDS_PER_DAY = 86400


class _RedisOps(Protocol):
    """redis.asyncio 客户端的窄接口（incr/pexpire/get/set/delete/decr/pttl）。

    抽离为协议便于测试注入内存假实现，不强制安装 redis 包。
    """

    async def incr(self, key: str) -> int: ...
    async def decr(self, key: str) -> int: ...
    async def pexpire(self, key: str, ms: int) -> None: ...
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, px: int) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def pttl(self, key: str) -> int: ...


class RedisLoginLockout:
    """跨进程防爆破（Redis INCR + PEXPIRE 滑动窗口计数）。

    语义与 LoginLockout 对齐：任一维度计数 ≥ threshold 即锁定；
    计数带 lock_seconds TTL，静默期后自动清零（滑动窗口）。
    """

    def __init__(self, threshold: int, lock_seconds: int, client: _RedisOps) -> None:
        if threshold < 1 or lock_seconds < 1:
            raise ValueError("threshold 与 lock_seconds 必须 ≥1")
        self._threshold = threshold
        self._lock_ms = lock_seconds * 1000
        self._client = client
        self._degraded = False

    def _warn(self, exc: Exception) -> None:
        if not self._degraded:
            self._degraded = True
            _logger.warning("Redis 防爆破不可用，已降级为失败开放（可用性优先）：%s", exc)

    async def register_failure(self, username: str, client_ip: str) -> None:
        try:
            for key in (_ACCOUNT_PREFIX + username, _IP_PREFIX + (client_ip or "-")):
                await self._client.incr(key)
                await self._client.pexpire(key, self._lock_ms)
        except Exception as exc:  # noqa: BLE001 - Redis 不可用降级
            self._warn(exc)

    async def is_locked(self, username: str, client_ip: str) -> bool:
        try:
            for key in (_ACCOUNT_PREFIX + username, _IP_PREFIX + (client_ip or "-")):
                raw = await self._client.get(key)
                if raw is not None and int(raw) >= self._threshold:
                    return True
            return False
        except Exception as exc:  # noqa: BLE001 - Redis 不可用降级
            self._warn(exc)
            return False

    async def clear_account(self, username: str) -> None:
        try:
            await self._client.delete(_ACCOUNT_PREFIX + username)
        except Exception as exc:  # noqa: BLE001 - Redis 不可用降级
            self._warn(exc)


class RedisCodeWindowLimiter:
    """跨进程验证码限流（重发间隔 + 日限额，固定窗口）。

    day 计数以首次 INCR 起 24h TTL（滑动日窗口）；触发日限额的请求
    不消耗额度（DECR 回滚），retry_after 取两者较大值。
    """

    def __init__(self, resend_seconds: int, daily_limit: int, client: _RedisOps) -> None:
        if resend_seconds < 1 or daily_limit < 1:
            raise ValueError("resend_seconds 必须 ≥1 且 daily_limit 必须 ≥1")
        self._resend_ms = resend_seconds * 1000
        self._daily = daily_limit
        self._day_ms = _SECONDS_PER_DAY * 1000
        self._client = client
        self._degraded = False

    def _warn(self, exc: Exception) -> None:
        if not self._degraded:
            self._degraded = True
            _logger.warning("Redis 验证码限流不可用，已降级为放行（可用性优先）：%s", exc)

    async def allow(self, key: str) -> bool:
        now_ms = int(time.time() * 1000)
        try:
            last = await self._client.get(_CODE_LAST_PREFIX + key)
            if last is not None and now_ms - int(last) < self._resend_ms:
                return False
            day_count = await self._client.incr(_CODE_DAY_PREFIX + key)
            if day_count == 1:
                await self._client.pexpire(_CODE_DAY_PREFIX + key, self._day_ms)
            if day_count > self._daily:
                await self._client.decr(_CODE_DAY_PREFIX + key)  # 拒绝不消耗额度
                return False
            await self._client.set(_CODE_LAST_PREFIX + key, str(now_ms), px=self._resend_ms)
            return True
        except Exception as exc:  # noqa: BLE001 - Redis 不可用降级
            self._warn(exc)
            return True

    async def retry_after_seconds(self, key: str) -> float:
        now_ms = int(time.time() * 1000)
        wait = 0.0
        try:
            last = await self._client.get(_CODE_LAST_PREFIX + key)
            if last is not None and now_ms - int(last) < self._resend_ms:
                wait = max(wait, (self._resend_ms - (now_ms - int(last))) / 1000.0)
            day_count = int(await self._client.get(_CODE_DAY_PREFIX + key) or 0)
            ttl = await self._client.pttl(_CODE_DAY_PREFIX + key)
            if day_count >= self._daily and ttl > 0:
                wait = max(wait, ttl / 1000.0)
            return wait
        except Exception as exc:  # noqa: BLE001 - Redis 不可用降级
            self._warn(exc)
            return 0.0


def build_redis_client(redis_url: str | None) -> _RedisOps | None:
    """惰性创建 redis.asyncio 客户端；未配置或包缺失返回 None。"""
    if not redis_url:
        return None
    try:
        from redis import asyncio as redis_asyncio

        return redis_asyncio.Redis.from_url(redis_url, decode_responses=True)
    except ImportError:
        _logger.warning("未安装 redis 包：ERDOS_REDIS_URL 已配置但回退进程内实现")
        return None


def build_lockout(config: Settings, client: _RedisOps | None) -> LockoutGuard:
    """按配置构建登录防爆破守卫（Redis 跨进程 / 进程内）。"""
    if client is not None:
        return RedisLoginLockout(config.auth_lockout_threshold, config.auth_lockout_seconds, client)
    return LoginLockout(config.auth_lockout_threshold, config.auth_lockout_seconds)


def build_code_limiter(config: Settings, client: _RedisOps | None) -> VerificationCodeLimiter:
    """按配置构建验证码限流器（Redis 跨进程 / 进程内）。"""
    if client is not None:
        return RedisCodeWindowLimiter(
            config.verification_resend_seconds, config.verification_daily_limit, client
        )
    return FixedWindowCodeLimiter(
        config.verification_resend_seconds, config.verification_daily_limit
    )