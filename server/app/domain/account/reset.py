"""密码重置（SP2-3）：一次性令牌 + 重发限流（PRD F-001：60s/次、每日 10 次）。

- 令牌单实例内存保存（进程无状态化时需迁移 Redis/落库，SP2-7 窗口收口）；
- 账号存在性不对外泄露：请求阶段无论账号是否存在均返回受理；
- 发送渠道经 ResetNotifier 端口（dev 实现为日志，生产接邮件/短信）。
"""

import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from app.core.clock import utc_now
from app.core.errors import RATE_LIMITED, RESET_TOKEN_INVALID, AppError
from app.domain.account.ports import AccountRepository, PasswordHasher
from app.domain.account.service import PasswordPolicy

_RESET_DAILY_SECONDS = 86400


class ResetNotifier(Protocol):
    """重置令牌发送渠道端口。"""

    async def send(self, identifier: str, token: str) -> None: ...


class PasswordResetService:
    """重置令牌生命周期：issue（限流）-> confirm（校验 + 落库）。

    repo 由调用方（请求级事务）注入；令牌/限流状态进程内保存。
    """

    def __init__(
        self,
        hasher: PasswordHasher,
        policy: PasswordPolicy,
        notifier: ResetNotifier,
        *,
        token_ttl_seconds: int,
        resend_seconds: int,
        daily_limit: int,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._hasher = hasher
        self._policy = policy
        self._notifier = notifier
        self._token_ttl = token_ttl_seconds
        self._resend_seconds = resend_seconds
        self._daily_limit = daily_limit
        self._now = now
        self._tokens: dict[str, tuple[str | None, datetime]] = {}  # token -> (user_id, expires)
        self._requests: dict[str, tuple[datetime, datetime, int]] = {}  # id -> (last_sent, day_start, count)

    async def request(self, repo: AccountRepository, identifier: str) -> None:
        """申请重置（受理即返回；实际发送走 notifier）。"""
        now = self._now()
        last_sent, day_start, count = self._requests.get(identifier, (None, now, 0))
        if last_sent is not None and (now - last_sent).total_seconds() < self._resend_seconds:
            raise AppError(RATE_LIMITED, detail="验证码发送过于频繁，请稍后再试")
        if now - day_start > timedelta(seconds=_RESET_DAILY_SECONDS):
            day_start, count = now, 0
        if count >= self._daily_limit:
            raise AppError(RATE_LIMITED, detail="今日发送次数已达上限，请明日再试")

        record = await repo.find_by_identifier(identifier)
        token = secrets.token_urlsafe(32)
        self._tokens[token] = (
            record.id if record is not None else None,
            now + timedelta(seconds=self._token_ttl),
        )
        self._requests[identifier] = (now, day_start, count + 1)
        await self._notifier.send(identifier, token)

    async def confirm(self, repo: AccountRepository, token: str, new_password: str) -> str:
        """确认重置：令牌校验 -> 强度校验 -> 更新哈希；返回 user_id 供吊销会话。"""
        grant = self._tokens.get(token)
        if grant is None or self._now() > grant[1]:
            raise AppError(RESET_TOKEN_INVALID)
        user_id, _expires = grant
        self._policy.validate(new_password)
        if user_id is None:
            # 申请时账号不存在（防枚举受理），确认必然失败
            raise AppError(RESET_TOKEN_INVALID)
        self._tokens.pop(token, None)
        updated = await repo.update_password(
            user_id, self._hasher.hash(new_password)
        )
        if not updated:
            raise AppError(RESET_TOKEN_INVALID)
        return user_id