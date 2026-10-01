"""认证授权领域端口与数据载体（SP2-2）。

契约要点：
- RBAC 四角色：user / teacher（P2 教师端）/ operator / admin；
- 刷新令牌为不可知随机串，服务端仅存 SHA-256 哈希；
- 轮换即吊销：旧令牌被标记 revoked 并记录 replaced_by_hash 形成家族链。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

ROLE_USER = "user"
ROLE_TEACHER = "teacher"
ROLE_OPERATOR = "operator"
ROLE_ADMIN = "admin"
ALL_ROLES: tuple[str, ...] = (ROLE_USER, ROLE_TEACHER, ROLE_OPERATOR, ROLE_ADMIN)


@dataclass(frozen=True, slots=True)
class AuthIdentity:
    """认证后的主体身份（凭据校验或令牌验签的产物）。"""

    subject: str
    roles: tuple[str, ...] = ()
    device_id: str | None = None


@dataclass(frozen=True, slots=True)
class TokenPair:
    """一次登录/刷新产出的双令牌。"""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 0
    refresh_expires_in: int = 0


@dataclass(frozen=True, slots=True)
class RefreshSession:
    """服务端刷新会话记录（以哈希为键；user_id 关联《数据模型设计》users）。"""

    token_hash: str
    user_id: str
    roles: tuple[str, ...]
    device_id: str | None
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    replaced_by_hash: str | None = None


class CredentialVerifier(Protocol):
    """密码凭据校验端口（SP2-3 账号服务接入后替换为真实实现）。"""

    async def verify(self, username: str, password: str) -> AuthIdentity | None:
        """校验用户名密码；失败返回 None（不泄露具体原因）。"""
        ...


class RefreshTokenRepository(Protocol):
    """刷新令牌仓储端口（存储、轮换吊销、设备维度撤销）。"""

    async def get(self, token_hash: str) -> RefreshSession | None:
        """按哈希读取会话（含已吊销记录）。"""
        ...

    async def save(self, session: RefreshSession) -> None:
        """写入新会话。"""
        ...

    async def revoke_if_active(self, token_hash: str, revoked_at: datetime, replaced_by_hash: str | None) -> bool:
        """条件吊销（仅未吊销时生效）；返回是否吊销成功（并发轮换防护）。"""
        ...

    async def revoke_device(self, subject: str, device_id: str, revoked_at: datetime) -> int:
        """吊销同一主体同一设备的全部未吊销令牌，返回吊销数量。"""
        ...

    async def revoke_user(self, user_id: str, revoked_at: datetime) -> int:
        """吊销某用户全部未吊销令牌（注销/改密场景），返回吊销数量。"""
        ...


class TokenManager(Protocol):
    """访问令牌签发/验签与公钥发布端口。"""

    def issue_access(self, identity: AuthIdentity) -> str:
        """签发短期访问令牌（Ed25519 签名）。"""
        ...

    def verify(self, token: str) -> AuthIdentity | None:
        """验签并校验有效期/签发方；失败返回 None。"""
        ...

    def public_jwks(self) -> dict:
        """公钥集（JWKS 格式，含密钥版本 kid）。"""
        ...