"""登录防爆破（SP2-2）：账号 + IP 双维度失败计数与临时锁定。

- 单进程内存实现（重启即清零）：满足当前单实例部署与验收矩阵；
  多实例/持久化锁定在 SP2-7 或接入 Redis 时替换为分布式计数；
- 锁定到期后自动解除并清零计数（无人工解锁接口）。
"""

from collections.abc import Callable
from datetime import datetime, timedelta

from app.core.clock import utc_now


class LoginLockout:
    """登录失败守卫：同账号或同 IP 连续失败达到阈值即锁定一段时间。"""

    def __init__(
        self,
        threshold: int,
        lock_seconds: int,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        if threshold < 1:
            raise ValueError("threshold 必须 ≥1")
        if lock_seconds < 1:
            raise ValueError("lock_seconds 必须 ≥1")
        self._threshold = threshold
        self._lock_seconds = lock_seconds
        self._now = now
        # key（acct:<name> / ip:<ip>） -> (failed_count, locked_until)
        self._attempts: dict[str, tuple[int, datetime | None]] = {}

    def register_failure(self, username: str, client_ip: str) -> None:
        """记录一次失败；达到阈值即武装锁定（第 6 次尝试起被拒）。"""
        current = self._now()
        for key in (_account_key(username), _ip_key(client_ip)):
            count, _locked = self._attempts.get(key, (0, None))
            count += 1
            if count >= self._threshold:
                self._attempts[key] = (count, current + timedelta(seconds=self._lock_seconds))
            else:
                self._attempts[key] = (count, None)

    def is_locked(self, username: str, client_ip: str) -> bool:
        """账号或 IP 任一维度处于锁定期则拒绝。"""
        current = self._now()
        return any(
            self._check_locked(key, current)
            for key in (_account_key(username), _ip_key(client_ip))
        )

    def clear_account(self, username: str) -> None:
        """登录成功后清零账号维度计数（IP 维度保留，防跨账号撞库）。"""
        self._attempts.pop(_account_key(username), None)

    def _check_locked(self, key: str, current: datetime) -> bool:
        entry = self._attempts.get(key)
        if entry is None:
            return False
        _count, locked_until = entry
        if locked_until is not None and current < locked_until:
            return True
        if locked_until is not None and current >= locked_until:
            self._attempts.pop(key, None)  # 到期自动解除并清零
            return False
        return False


def _account_key(username: str) -> str:
    return f"acct:{username}"


def _ip_key(client_ip: str) -> str:
    return f"ip:{client_ip or '-'}"