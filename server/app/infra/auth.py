"""鉴权组件。

`TokenIntrospector` 为能力端口：SP2-2 交付 JWT（Ed25519）验签实现后，
网关依赖与此端口保持不变；生产置 auth_enforce=True 即全覆盖强制。
密钥域注意：签名私钥仅本模块持有（环境变量注入），公钥经 JWKS 发布。
"""

import base64
import logging
import secrets
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import bcrypt
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ACCOUNT_FROZEN, AppError
from app.domain.account.ports import PasswordHasher
from app.domain.auth.ports import (
    ALL_ROLES,
    AuthIdentity,
    TokenManager,
)
from app.repository.account import SQLAlchemyAccountRepository

logger = logging.getLogger("erdos.auth")


@dataclass(frozen=True, slots=True)
class Principal:
    """请求主体（透传给领域层的统一身份载体）。"""

    subject: str
    roles: tuple[str, ...] = ()
    device_id: str | None = None
    claims: Mapping[str, Any] = field(default_factory=dict)
    verified: bool = False  # SP2-2 验签后为 True


class TokenIntrospector(Protocol):
    """令牌解析协议：Bearer 令牌 -> 主体身份（验签失败返回 None）。"""

    async def introspect(self, token: str) -> Principal | None: ...


class DevTokenIntrospector:
    """test 环境骨架实现：不做验签，将原始令牌透传为 subject（仅 env=test 装配）。"""

    def __init__(self, roles: tuple[str, ...] = ()) -> None:
        self._roles = roles

    async def introspect(self, token: str) -> Principal:
        return Principal(subject=token, roles=self._roles, verified=False)


@dataclass(frozen=True, slots=True)
class SigningKeys:
    """Ed25519 签名密钥集：首个为当前签发密钥，其余为验签兜底（轮换窗口）。"""

    active_kid: str
    private_keys: dict[str, Ed25519PrivateKey]


def build_signing_keys(raw: str, env: str) -> SigningKeys:
    """解析 `kid:hex32[,...]`；dev/test 未配置时自动生成临时密钥并告警。"""
    if raw.strip():
        private_keys: dict[str, Ed25519PrivateKey] = {}
        for entry in raw.split(","):
            kid, _, hex_key = entry.strip().partition(":")
            if not kid or len(hex_key) != 64:
                raise ValueError(f"签名密钥配置格式错误：{entry}（应为 kid:hex32）")
            private_keys[kid] = Ed25519PrivateKey.from_private_bytes(
                bytes.fromhex(hex_key)
            )
        if not private_keys:
            raise ValueError("auth_signing_keys 至少需要一个密钥")
        return SigningKeys(
            active_kid=next(iter(private_keys)), private_keys=private_keys
        )
    if env in ("dev", "test"):
        key = Ed25519PrivateKey.generate()
        logger.warning(
            "auth_signing_keys 未配置：已为 %s 环境生成临时 Ed25519 密钥（kid=dev-1），"
            "重启即失效——生产必须通过 ERDOS_AUTH_SIGNING_KEYS 注入。",
            env,
        )
        return SigningKeys(active_kid="dev-1", private_keys={"dev-1": key})
    raise RuntimeError("生产/预发环境必须配置 ERDOS_AUTH_SIGNING_KEYS（kid:hex32）")


class Ed25519TokenManager:
    """访问令牌签发/验签（JWT, EdDSA/Ed25519）+ JWKS 公钥发布。"""

    def __init__(self, keys: SigningKeys, issuer: str, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds 必须 >0")
        self._keys = keys
        self._issuer = issuer
        self._ttl = ttl_seconds

    def issue_access(self, identity: AuthIdentity) -> str:
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": self._issuer,
            "sub": identity.subject,
            "roles": list(identity.roles),
            "iat": now,
            "exp": now + self._ttl,
            "jti": uuid.uuid4().hex,
        }
        if identity.device_id:
            payload["device_id"] = identity.device_id
        return jwt.encode(
            payload,
            self._keys.private_keys[self._keys.active_kid],
            algorithm="EdDSA",
            headers={"kid": self._keys.active_kid},
        )

    def verify(self, token: str) -> AuthIdentity | None:
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            key = self._keys.private_keys.get(str(kid)) if kid else None
            if key is None:
                return None
            claims = jwt.decode(
                token,
                key.public_key(),
                algorithms=["EdDSA"],
                issuer=self._issuer,
                options={"require": ["exp", "sub", "iat"]},
            )
        except jwt.PyJWTError:
            return None
        roles = tuple(claims.get("roles") or ())
        return AuthIdentity(
            subject=str(claims["sub"]),
            roles=tuple(r for r in roles if r in ALL_ROLES),
            device_id=claims.get("device_id"),
        )

    def public_jwks(self) -> dict:
        """JWKS：全部版本公钥（轮换窗口内旧密钥仍可验签）。"""
        keys = []
        for kid, private in self._keys.private_keys.items():
            raw = private.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
            keys.append(
                {
                    "kty": "OKP",
                    "crv": "Ed25519",
                    "kid": kid,
                    "use": "sig",
                    "alg": "EdDSA",
                    "x": base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii"),
                }
            )
        return {"keys": keys}


class JwtTokenIntrospector:
    """生产形态：Ed25519 验签 + 有效期/签发方校验；失败返回 None（网关拒绝）。"""

    def __init__(self, manager: TokenManager) -> None:
        self._manager = manager

    async def introspect(self, token: str) -> Principal | None:
        identity = self._manager.verify(token)
        if identity is None:
            return None
        return Principal(
            subject=identity.subject,
            roles=identity.roles,
            device_id=identity.device_id,
            verified=True,
        )


class FallbackJwtIntrospector:
    """dev 环境：验签失败回退为演示主体（保证本地看板联调零配置）。

    生产/test 环境禁止装配此类——回退角色仅存在于 dev。
    """

    def __init__(self, manager: TokenManager, fallback_roles: tuple[str, ...]) -> None:
        self._jwt = JwtTokenIntrospector(manager)
        self._fallback_roles = fallback_roles

    async def introspect(self, token: str) -> Principal:
        principal = await self._jwt.introspect(token)
        if principal is not None:
            return principal
        return Principal(subject=token, roles=self._fallback_roles, verified=False)


class DevCredentialVerifier:
    """开发期密码凭据源：`user:pass:role1,role2;...` 环境变量种子账号。

    SP2-3 账号服务交付后替换为真实实现（CredentialVerifier 端口不变）。
    """

    def __init__(self, accounts: dict[str, tuple[str, tuple[str, ...]]]) -> None:
        self._accounts = accounts

    @classmethod
    def parse(cls, raw: str) -> "DevCredentialVerifier":
        accounts: dict[str, tuple[str, tuple[str, ...]]] = {}
        for entry in raw.split(";"):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(":")
            if len(parts) != 3 or not all(parts):
                raise ValueError(f"auth_dev_users 条目格式错误：{entry}（应为 user:pass:role1,role2）")
            username, password, roles_raw = parts
            roles = tuple(r.strip() for r in roles_raw.split(",") if r.strip())
            unknown = set(roles) - set(ALL_ROLES)
            if unknown:
                raise ValueError(f"auth_dev_users 角色非法：{sorted(unknown)}（可选 {ALL_ROLES}）")
            accounts[username] = (password, roles)
        return cls(accounts)

    async def verify(self, username: str, password: str) -> AuthIdentity | None:
        entry = self._accounts.get(username)
        if entry is None:
            return None
        expected_password, roles = entry
        if not secrets.compare_digest(password, expected_password):
            return None
        return AuthIdentity(subject=username, roles=roles)


class BcryptPasswordHasher:
    """bcrypt（cost=12，契约 4.1 要求 ≥12）。"""

    _ROUNDS = 12

    def hash(self, plain: str) -> str:
        return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=self._ROUNDS)).decode("ascii")

    def verify(self, plain: str, hashed: str) -> bool:
        try:
            return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))
        except ValueError:
            return False


class SqlCredentialVerifier:
    """SP2-3 正式凭据源：users 表 + bcrypt；无该标识时回退（dev 种子）验证器。

    账号冻结/注销抛 ACCOUNT_FROZEN（区分于密码错误，不累计防爆破失败）；
    不存在标识的请求执行一次虚拟 bcrypt 校验，抹平响应时序防枚举。
    """

    _DUMMY_HASH = "$2b$12$C6UzMDM.H6dfI/f/IKcEeO8s4e5qR9s0O7o1WrJh3zV7xY0y1u2a"  # 固定占位

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        hasher: PasswordHasher,
        fallback: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._hasher = hasher
        self._fallback = fallback

    async def verify(self, username: str, password: str) -> AuthIdentity | None:
        async with self._session_factory() as session:
            record = await SQLAlchemyAccountRepository(session).find_by_identifier(username)
        if record is None:
            if self._fallback is not None:
                return await self._fallback.verify(username, password)
            self._hasher.verify(password, self._DUMMY_HASH)  # 时序抹平，防枚举
            return None
        if record.deleted_at is not None or record.status != "active":
            raise AppError(ACCOUNT_FROZEN)
        if not self._hasher.verify(password, record.password_hash):
            return None
        return AuthIdentity(subject=record.id, roles=(record.role,))


class LogResetNotifier:
    """dev/test 重置令牌渠道：写日志并在内存留痕（联调/测试读取）。

    生产环境替换为邮件/短信发送器（ResetNotifier 端口不变）。
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, identifier: str, token: str) -> None:
        self.sent.append((identifier, token))
        logger.info("密码重置令牌已生成（dev 渠道），identifier=%s token=%s", identifier, token)


class Ed25519LicenseSigner:
    """阶段许可签名器（SP2-4 积分域）：复用签名密钥集对规范化字节 Ed25519 签名。

    返回 (signature_hex, kid)。客户端凭服务端公钥（JWKS）离线验签。
    """

    def __init__(self, keys: SigningKeys) -> None:
        self._keys = keys

    def sign(self, payload: bytes) -> tuple[str, str]:
        """对 payload 规范化字节签名，返回 (hex 签名, 密钥版本 kid)。"""
        key = self._keys.private_keys[self._keys.active_kid]
        signature = key.sign(payload)
        return signature.hex(), self._keys.active_kid