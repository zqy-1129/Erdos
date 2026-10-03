"""敏感键脱敏公共工具（检查点/留痕共用，密钥域红线）。

双层词表：
- SENSITIVE_FRAGMENTS（默认，检查点用）：含裸 "token"——编排状态中裸 token 键即敏感；
- TRAIL_SENSITIVE_FRAGMENTS（留痕用）：不含裸 "token"，避免误伤 total_tokens
  等用量统计字段（F-002 用量估算依赖留痕 usage）；凭据类键（access_token/
  refresh_token/auth_token/key/secret/password/credential/authorization）仍剔除。
"""

from typing import Any

SENSITIVE_FRAGMENTS = (
    "key",
    "secret",
    "token",
    "password",
    "credential",
    "authorization",
)

TRAIL_SENSITIVE_FRAGMENTS = (
    "key",
    "secret",
    "password",
    "credential",
    "authorization",
    "access_token",
    "refresh_token",
    "auth_token",
)


def redact_sensitive(data: Any, fragments: tuple[str, ...] | None = None) -> Any:
    """递归剔除键名含敏感片段的字段（大小写不敏感，片段可注入）。"""
    rules = fragments or SENSITIVE_FRAGMENTS
    if isinstance(data, dict):
        return {
            k: redact_sensitive(v, rules)
            for k, v in data.items()
            if not any(fragment in k.lower() for fragment in rules)
        }
    if isinstance(data, list):
        return [redact_sensitive(item, rules) for item in data]
    return data