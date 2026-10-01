"""账号域端口与数据载体（SP2-3，与《数据模型设计》users/devices 对齐）。"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AccountRecord:
    """用户账号视图（对外不含密码哈希——哈希仅在仓储内部流转）。"""

    id: str
    email: str | None
    phone: str | None
    password_hash: str
    status: str
    role: str
    created_at: datetime
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class RegistrationRequest:
    """一次注册申请（邮箱/手机至少其一，密码强度由服务校验）。"""

    email: str | None
    phone: str | None
    password: str
    fingerprint: str
    platform: str | None
    client_ip: str | None


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    """注册结果：是否触发赠分与账号主体。"""

    account: AccountRecord
    gift_granted: bool


@dataclass(frozen=True, slots=True)
class DeviceCard:
    """设备登记视图。"""

    id: str
    platform: str | None
    last_seen_at: datetime
    first_gift_used: bool


class PasswordHasher(Protocol):
    """密码哈希端口（bcrypt cost≥12；SP2-3 后可按需升级算法）。"""

    def hash(self, plain: str) -> str: ...

    def verify(self, plain: str, hashed: str) -> bool: ...


class AccountRepository(Protocol):
    """用户账号仓储端口。"""

    async def create(self, record: AccountRecord) -> AccountRecord:
        """创建账号（uuid 由仓储生成回写）。"""
        ...

    async def find_by_identifier(self, identifier: str) -> AccountRecord | None:
        """按邮箱或手机查找（含已注销记录，供登录层判定）。"""
        ...

    async def get(self, user_id: str) -> AccountRecord | None:
        """按主键读取。"""
        ...

    async def update_password(self, user_id: str, password_hash: str) -> bool:
        """更新密码哈希；账号不存在返回 False。"""
        ...

    async def anonymize(self, user_id: str, deleted_at: datetime) -> bool:
        """注销匿名化（软删除 + 清除登录标识/凭据，流水保留）。"""
        ...


class DeviceRepository(Protocol):
    """设备登记仓储端口（指纹防刷与赠分标记）。"""

    async def exists_fingerprint(self, fingerprint: str) -> bool:
        """该设备指纹是否已登记过（任意用户）。"""
        ...

    async def register(
        self,
        user_id: str,
        fingerprint: str,
        platform: str | None,
        first_gift_used: bool,
        now: datetime,
    ) -> str:
        """登记新设备，返回设备 id。"""
        ...

    async def touch(
        self,
        user_id: str,
        fingerprint: str,
        platform: str | None,
        now: datetime,
    ) -> None:
        """登录时更新设备登入时间（不存在则补登记）。"""
        ...

    async def list_by_user(self, user_id: str) -> list[DeviceCard]:
        """某用户的设备列表（按最近活跃倒序）。"""
        ...


_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(value: str | None) -> bool:
    """邮箱格式粗校验（与 PRD 输入语义一致，不做 DNS 级校验）。"""
    return bool(value) and bool(_EMAIL_PATTERN.fullmatch(value or ""))