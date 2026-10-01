"""Ed25519 签名管理测试：签发/验签、密钥轮换、JWKS 与工程边界。"""

import base64
import json

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.domain.auth.ports import AuthIdentity
from app.infra.auth import (
    Ed25519TokenManager,
    SigningKeys,
    build_signing_keys,
)


def _hex(key: Ed25519PrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    ).hex()


def _manager(kids: dict[str, Ed25519PrivateKey]) -> Ed25519TokenManager:
    return Ed25519TokenManager(
        SigningKeys(active_kid=next(iter(kids)), private_keys=kids),
        issuer="erdos-server",
        ttl_seconds=900,
    )


def _identity() -> AuthIdentity:
    return AuthIdentity(subject="u-1", roles=("user",), device_id="dev-1")


def test_issue_and_verify_roundtrip() -> None:
    manager = _manager({"v1": Ed25519PrivateKey.generate()})
    token = manager.issue_access(_identity())
    verified = manager.verify(token)
    assert verified is not None
    assert verified.subject == "u-1"
    assert verified.roles == ("user",)
    assert verified.device_id == "dev-1"


def test_verify_rejects_tampered_or_wrong_key() -> None:
    manager = _manager({"v1": Ed25519PrivateKey.generate()})
    token = manager.issue_access(_identity())
    header, _payload, signature = token.split(".")
    forged_payload = json.dumps({"iss": "erdos-server", "sub": "evil", "iat": 0, "exp": 9**9})
    forged = f"{header}.{base64.urlsafe_b64encode(forged_payload.encode()).rstrip(b'=').decode()}.{signature}"
    assert manager.verify(forged) is None

    other = _manager({"other": Ed25519PrivateKey.generate()})
    assert other.verify(token) is None


def test_verify_expired_token_rejected() -> None:
    key = Ed25519PrivateKey.generate()
    manager = _manager({"v1": key})
    expired = jwt.encode(
        {"iss": "erdos-server", "sub": "u-1", "roles": ["user"], "iat": 1, "exp": 2, "jti": "x"},
        key,
        algorithm="EdDSA",
        headers={"kid": "v1"},
    )
    assert manager.verify(expired) is None


def test_key_rotation_old_service_verifies_new_tokens() -> None:
    """轮换场景：新签名密钥签发、旧服务仅持旧密钥时仍能验签新令牌（kid 回退多版本）。"""
    old = Ed25519PrivateKey.generate()
    new = Ed25519PrivateKey.generate()
    issuer = _manager({"v1": old, "v2": new})  # active=v1 签发出老 kid 不应发生
    token = issuer.issue_access(_identity())
    assert jwt.get_unverified_header(token)["kid"] == "v1"

    # 新服务：active=v2 签发；旧 pubkey(v1) 仍可验签
    rotated = Ed25519TokenManager(
        SigningKeys(active_kid="v2", private_keys={"v1": old, "v2": new}),
        issuer="erdos-server",
        ttl_seconds=900,
    )
    fresh = rotated.issue_access(_identity())
    assert jwt.get_unverified_header(fresh)["kid"] == "v2"
    # 仅持 v2 私钥的新服务验签 v1 旧令牌（仍在期内）
    assert rotated.verify(token) is not None


def test_jwks_contains_all_versions() -> None:
    old = Ed25519PrivateKey.generate()
    new = Ed25519PrivateKey.generate()
    manager = Ed25519TokenManager(
        SigningKeys(active_kid="v2", private_keys={"v1": old, "v2": new}),
        issuer="erdos-server",
        ttl_seconds=900,
    )
    jwks = manager.public_jwks()
    assert [k["kid"] for k in jwks["keys"]] == ["v1", "v2"]
    for key in jwks["keys"]:
        assert key["kty"] == "OKP"
        assert key["crv"] == "Ed25519"
        assert len(key["x"]) == 43  # 32 字节 Base64URL 无填充


def test_build_signing_keys_dev_fallback_warns() -> None:
    keys = build_signing_keys("", "dev")
    assert keys.active_kid == "dev-1"
    assert keys.private_keys  # 非空


def test_build_signing_keys_prod_without_keys_raises() -> None:
    with pytest.raises(RuntimeError):
        build_signing_keys("", "prod")


def test_build_signing_keys_multi_version_parse() -> None:
    k1 = Ed25519PrivateKey.generate()
    k2 = Ed25519PrivateKey.generate()
    keys = build_signing_keys(f"v1:{_hex(k1)},v2:{_hex(k2)}", "prod")
    assert keys.active_kid == "v1"
    assert set(keys.private_keys) == {"v1", "v2"}


def test_build_signing_keys_bad_format_raises() -> None:
    with pytest.raises(ValueError):
        build_signing_keys("v1:nothex", "prod")