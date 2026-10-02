"""权益快照签发服务（SP2-6）：聚合订阅+积分状态，Ed25519 签名，客户端离线验签。

快照 payload 对齐《数据模型设计》entitlement_snapshots.payload：
  { subscribed, sub_end_at, purchased_balance, monthly_balance, frozen }
签名对 payload 规范化字节（sort_keys + 紧凑 JSON）Ed25519 签名，
客户端凭服务端公钥（JWKS）验签；issued_at 为防回拨依据。
"""

import json
from datetime import datetime
from typing import Protocol

from app.domain.entitlement.ports import (
    EntitlementSnapshotRecord,
    SnapshotPayload,
    SnapshotRepository,
    SnapshotResult,
)
from app.domain.points.ports import PointAccountRepository


class SnapshotSigner(Protocol):
    """快照签名端口（Ed25519 对规范化字节签名 + 验签）。"""

    def sign(self, payload: bytes) -> tuple[str, str]:
        """返回 (signature_hex, key_version/kid)。"""
        ...

    def verify(self, payload: bytes, signature_hex: str, kid: str | None = None) -> bool:
        """验签，返回是否通过。"""
        ...


class SubscriptionStateProvider(Protocol):
    """订阅状态提供方（适配 Billing 域 subscription 仓储）。"""

    async def active_until(self, user_id: str) -> tuple[bool, datetime | None]:
        """返回 (是否活跃订阅, 最近到期时间)。"""
        ...


class EntitlementService:
    """权益快照用例：聚合订阅+积分 → 签发签名快照。"""

    def __init__(
        self,
        snapshots: SnapshotRepository,
        signer: SnapshotSigner,
        subscriptions: SubscriptionStateProvider,
        accounts: PointAccountRepository,
    ) -> None:
        self._snapshots = snapshots
        self._signer = signer
        self._subscriptions = subscriptions
        self._accounts = accounts

    async def issue_snapshot(self, user_id: str, now: datetime) -> SnapshotResult:
        """签发权益快照：聚合订阅与积分状态，签名并落库。"""
        subscribed, sub_end_at = await self._subscriptions.active_until(user_id)
        account = await self._accounts.get(user_id)
        purchased = account.purchased_balance if account else 0
        monthly = account.monthly_balance if account else 0
        frozen = account.frozen if account else False

        payload = SnapshotPayload(
            subscribed=subscribed,
            sub_end_at=sub_end_at.isoformat() if sub_end_at else None,
            purchased_balance=purchased,
            monthly_balance=monthly,
            frozen=frozen,
        ).as_dict()

        canonical = _canonical(payload)
        signature, kid = self._signer.sign(canonical)

        record = await self._snapshots.append(
            EntitlementSnapshotRecord(
                id="",
                user_id=user_id,
                payload=payload,
                signature=signature,
                key_version=kid,
                issued_at=now,
            )
        )
        return SnapshotResult(
            payload=payload,
            signature=signature,
            key_version=kid,
            issued_at=now,
            snapshot_id=record.id,
        )

    def verify_snapshot(self, payload: dict, signature_hex: str, kid: str | None = None) -> bool:
        """验签快照 payload（验收标准：篡改任一字段验签失败）。"""
        return self._signer.verify(_canonical(payload), signature_hex, kid)


def _canonical(payload: dict) -> bytes:
    """规范化序列化：键排序 + 紧凑 JSON（签名确定性，跨端验签一致）。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
