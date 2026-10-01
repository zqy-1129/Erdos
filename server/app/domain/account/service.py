"""账号域服务（SP2-3）：注册（指纹防刷赠分判定）、注销匿名化与密码强度策略。"""

from datetime import datetime

from app.core.errors import BAD_REQUEST, CONFLICT, AppError
from app.domain.account.ports import (
    AccountRecord,
    AccountRepository,
    DeviceRepository,
    PasswordHasher,
    RegistrationRequest,
    RegistrationResult,
    is_valid_email,
)


class PasswordPolicy:
    """密码强度策略（PRD F-001：≥8 位且同时含字母与数字）。"""

    def __init__(self, min_length: int) -> None:
        if min_length < 1:
            raise ValueError("min_length 必须 ≥1")
        self._min_length = min_length

    def validate(self, password: str) -> None:
        if len(password) < self._min_length:
            raise AppError(BAD_REQUEST, detail=f"密码长度至少 {self._min_length} 位")
        if not (any(c.isalpha() for c in password) and any(c.isdigit() for c in password)):
            raise AppError(BAD_REQUEST, detail="密码必须同时包含字母与数字")


class RegisterService:
    """注册用例：标识唯一性 -> 密码哈希 -> 建号 -> 设备登记 -> 赠分判定。"""

    def __init__(
        self,
        repo: AccountRepository,
        devices: DeviceRepository,
        hasher: PasswordHasher,
        policy: PasswordPolicy,
    ) -> None:
        self._repo = repo
        self._devices = devices
        self._hasher = hasher
        self._policy = policy

    async def register(self, request: RegistrationRequest, now: datetime) -> RegistrationResult:
        """注册；同邮箱/手机冲突抛 CONFLICT，同指纹重复注册不再赠分。"""
        self._policy.validate(request.password)
        if not (is_valid_email(request.email) or request.phone):
            raise AppError(BAD_REQUEST, detail="邮箱与手机至少提供一项且邮箱需格式合法")
        for identifier in (request.email, request.phone):
            if identifier and await self._repo.find_by_identifier(identifier) is not None:
                raise AppError(CONFLICT, detail="该邮箱或手机号已注册")

        record = await self._repo.create(
            AccountRecord(
                id="",  # 仓储生成 uuid 回写
                email=request.email,
                phone=request.phone,
                password_hash=self._hasher.hash(request.password),
                status="active",
                role="user",
                created_at=now,
                deleted_at=None,
            )
        )
        # 防刷：同设备指纹此前已登记（任意用户）即不再赠分（验收：同指纹多次注册只赠一次）
        fingerprint_seen = await self._devices.exists_fingerprint(request.fingerprint)
        gift_granted = not fingerprint_seen
        await self._devices.register(
            user_id=record.id,
            fingerprint=request.fingerprint,
            platform=request.platform,
            first_gift_used=fingerprint_seen,
            now=now,
        )
        return RegistrationResult(account=record, gift_granted=gift_granted)


class DeactivateService:
    """注销用例：匿名化（软删除 + 清登录标识凭据），流水保留可审计。"""

    def __init__(self, repo: AccountRepository) -> None:
        self._repo = repo

    async def deactivate(self, user_id: str, now: datetime) -> bool:
        """注销；重复注销幂等返回 False。"""
        return await self._repo.anonymize(user_id, now)