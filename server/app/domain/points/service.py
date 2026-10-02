"""积分域服务（SP2-4 资金域核心）：预扣-确认-退还状态机 + 阶段许可签发。

资金域红线（对齐《数据模型设计》与执行计划 SP2-4）：
- 状态机 reserved → confirmed/refunded，终态不可迁移；
- reserve/confirm/refund 全部以 exec_id 幂等；
- 余额扣减原子（仓储层 WHERE balance ≥ n 防超扣）；
- 双余额（购买/月度）与扣减顺序（优先月度，再购买）；
- 许可 Ed25519 签名（含 exec_id/阶段/积分/有效期）。
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from app.core.config import Settings
from app.core.errors import ACCOUNT_FROZEN, BAD_REQUEST, CONFLICT, NOT_FOUND, AppError
from app.domain.points.ports import (
    AccountBalance,
    BalanceType,
    GrantRecord,
    GrantRepository,
    LedgerKind,
    LedgerRecord,
    LedgerRepository,
    LedgerStatus,
    PointAccountRepository,
    ReserveRequest,
    ReserveResult,
    SettleResult,
    is_terminal,
)


class LicenseSigner(Protocol):
    """阶段许可签名端口（Ed25519 对规范化字节签名）。"""

    def sign(self, payload: bytes) -> tuple[str, str]:
        """返回 (signature_hex, key_version/kid)。"""
        ...


@dataclass(frozen=True, slots=True)
class _Deduction:
    """一次余额扣减结果：扣减后的余额视图 + 实际扣减的余额类型。"""

    balance: AccountBalance
    balance_type: BalanceType


class PointsService:
    """积分服务用例编排：预扣/确认/退还 + 注册赠分入账 + 许可签发。"""

    def __init__(
        self,
        accounts: PointAccountRepository,
        ledgers: LedgerRepository,
        grants: GrantRepository,
        signer: LicenseSigner,
        settings: Settings,
    ) -> None:
        self._accounts = accounts
        self._ledgers = ledgers
        self._grants = grants
        self._signer = signer
        self._settings = settings

    # ------------------------------------------------------------------
    # 注册赠分入账（消费 SP2-3 埋好的 points.registration_grant 事件）
    # ------------------------------------------------------------------
    async def grant_registration(self, user_id: str, points: int, now: datetime) -> None:
        """注册赠分入账：exec_id=register:{user_id} 幂等，同用户只入账一次。"""
        if points <= 0:
            raise ValueError("赠分额度必须为正")
        exec_id = f"register:{user_id}"
        existing = await self._ledgers.find(user_id, exec_id, LedgerKind.GRANT.value)
        if existing is not None:
            return  # 幂等：已赠过
        await self._accounts.get_or_create(user_id, now)
        await self._ledgers.append(
            LedgerRecord(
                id="",
                user_id=user_id,
                exec_id=exec_id,
                delta=points,
                balance_type=BalanceType.PURCHASED.value,
                kind=LedgerKind.GRANT.value,
                status=LedgerStatus.CONFIRMED.value,  # 赠分直接终态
                source="register_gift",
                task_id=None,
                stage=None,
                created_at=now,
            )
        )
        await self._accounts.credit(user_id, BalanceType.PURCHASED, points, now)

    # ------------------------------------------------------------------
    # 预扣（reserve）：许可签发 + 余额冻结
    # ------------------------------------------------------------------
    async def reserve(self, req: ReserveRequest, now: datetime) -> ReserveResult:
        """预扣：余额原子扣减 + 流水 + 许可签发（exec_id 幂等）。

        重复预扣（同 exec_id）返回已存在的许可与流水，不重复扣减。
        """
        if req.points <= 0:
            raise AppError(BAD_REQUEST, detail="预扣积分必须为正数")

        # 幂等：同 exec_id 已预扣 -> 直接返回既有结果
        existing_ledger = await self._ledgers.find(
            req.user_id, req.exec_id, LedgerKind.RESERVE.value
        )
        if existing_ledger is not None:
            grant = await self._grants.find(req.exec_id)
            if grant is None:
                raise AppError(CONFLICT, detail="预扣流水存在但许可缺失，数据不一致")
            balance = await self._require_account(req.user_id, now)
            return ReserveResult(
                balance=balance,
                ledger=existing_ledger,
                grant=grant,
                already_reserved=True,
            )

        balance = await self._require_account(req.user_id, now)
        if balance.frozen:
            raise AppError(ACCOUNT_FROZEN, detail="账号欠费已冻结，无法启动新任务")

        # 双余额扣减：整笔从单一余额类型扣（月度优先，不足则整笔扣购买），
        # 流水 balance_type 单一明确，符合 point_ledgers 唯一约束语义。
        deducted = await self._deduct(balance, req.points, now)
        if deducted is None:
            raise AppError(CONFLICT, detail="积分余额不足")

        ledger = await self._ledgers.append(
            LedgerRecord(
                id="",
                user_id=req.user_id,
                exec_id=req.exec_id,
                delta=-req.points,
                balance_type=deducted.balance_type.value,
                kind=LedgerKind.RESERVE.value,
                status=LedgerStatus.RESERVED.value,
                source="stage",
                task_id=req.task_id,
                stage=req.stage,
                created_at=now,
            )
        )
        if ledger is None:
            # 并发竞态：同 exec_id 已被另一事务预扣，本事务已回滚；
            # 回退扣减（重读）并走幂等返回路径。
            existing = await self._ledgers.find(
                req.user_id, req.exec_id, LedgerKind.RESERVE.value
            )
            grant = await self._grants.find(req.exec_id)
            if existing is None or grant is None:
                raise AppError(CONFLICT, detail="并发预扣冲突，数据不一致")
            balance = await self._require_account(req.user_id, now)
            return ReserveResult(
                balance=balance,
                ledger=existing,
                grant=grant,
                already_reserved=True,
            )

        expires_at = now + timedelta(
            seconds=self._settings.license_ttl_seconds
        )
        grant = await self._issue_grant(req, deducted.balance, now, expires_at)
        return ReserveResult(
            balance=deducted.balance, ledger=ledger, grant=grant, already_reserved=False
        )

    # ------------------------------------------------------------------
    # 确认（confirm）：阶段完成，预扣转为实际消耗
    # ------------------------------------------------------------------
    async def confirm(self, user_id: str, exec_id: str, now: datetime) -> SettleResult:
        """确认消耗：reserved → confirmed（终态）。重复确认幂等返回既有终态。"""
        ledger = await self._ledgers.find(user_id, exec_id, LedgerKind.RESERVE.value)
        if ledger is None:
            raise AppError(NOT_FOUND, detail="预扣流水不存在")
        if is_terminal(ledger.status):
            if ledger.status == LedgerStatus.REFUNDED.value:
                raise AppError(CONFLICT, detail="该预扣已退还，无法确认")
            # confirmed 终态：幂等返回
            balance = await self._require_account(user_id, now)
            return SettleResult(balance=balance, ledger=ledger)
        updated = await self._ledgers.transition(
            user_id, exec_id, LedgerKind.RESERVE.value, LedgerStatus.CONFIRMED.value
        )
        if updated is None:
            # 并发下已被迁移：重读终态
            ledger = await self._ledgers.find(user_id, exec_id, LedgerKind.RESERVE.value)
            if ledger is None or not is_terminal(ledger.status):
                raise AppError(CONFLICT, detail="状态迁移冲突")
            balance = await self._require_account(user_id, now)
            return SettleResult(balance=balance, ledger=ledger)
        balance = await self._require_account(user_id, now)
        return SettleResult(balance=balance, ledger=updated)

    # ------------------------------------------------------------------
    # 退还（refund）：阶段取消，预扣返还余额
    # ------------------------------------------------------------------
    async def refund(self, user_id: str, exec_id: str, now: datetime) -> SettleResult:
        """退还：reserved → refunded（终态）+ 余额返还。重复退还幂等。"""
        ledger = await self._ledgers.find(user_id, exec_id, LedgerKind.RESERVE.value)
        if ledger is None:
            raise AppError(NOT_FOUND, detail="预扣流水不存在")
        if is_terminal(ledger.status):
            if ledger.status == LedgerStatus.CONFIRMED.value:
                raise AppError(CONFLICT, detail="该预扣已确认消耗，无法退还")
            balance = await self._require_account(user_id, now)
            return SettleResult(balance=balance, ledger=ledger)
        updated = await self._ledgers.transition(
            user_id, exec_id, LedgerKind.RESERVE.value, LedgerStatus.REFUNDED.value
        )
        if updated is None:
            ledger = await self._ledgers.find(user_id, exec_id, LedgerKind.RESERVE.value)
            if ledger is None or not is_terminal(ledger.status):
                raise AppError(CONFLICT, detail="状态迁移冲突")
            balance = await self._require_account(user_id, now)
            return SettleResult(balance=balance, ledger=ledger)
        # 返还余额（按原扣减类型）
        balance = await self._accounts.credit(
            user_id,
            BalanceType(ledger.balance_type),
            abs(ledger.delta),
            now,
        )
        return SettleResult(balance=balance, ledger=updated)

    # ------------------------------------------------------------------
    # 内部：双余额扣减（整笔单一类型，月度优先）
    # ------------------------------------------------------------------
    async def _deduct(
        self, balance: AccountBalance, points: int, now: datetime
    ) -> _Deduction | None:
        """按「月度优先，购买兜底」顺序扣减；任一类型余额充足即整笔扣除，否则返回 None。"""
        if balance.monthly_balance >= points:
            new_balance = await self._accounts.debit(
                balance.user_id, BalanceType.MONTHLY, points, now
            )
            if new_balance is not None:
                return _Deduction(balance=new_balance, balance_type=BalanceType.MONTHLY)
        if balance.purchased_balance >= points:
            new_balance = await self._accounts.debit(
                balance.user_id, BalanceType.PURCHASED, points, now
            )
            if new_balance is not None:
                return _Deduction(balance=new_balance, balance_type=BalanceType.PURCHASED)
        return None

    async def _require_account(self, user_id: str, now: datetime) -> AccountBalance:
        balance = await self._accounts.get(user_id)
        if balance is None:
            raise AppError(NOT_FOUND, detail="积分账户不存在")
        return balance

    async def _issue_grant(
        self,
        req: ReserveRequest,
        deducted: AccountBalance,
        now: datetime,
        expires_at: datetime,
    ) -> GrantRecord:
        payload = {
            "exec_id": req.exec_id,
            "user_id": req.user_id,
            "task_id": req.task_id,
            "stage": req.stage,
            "points": req.points,
            "issued_at": int(now.timestamp()),
            "expires_at": int(expires_at.timestamp()),
        }
        signature, kid = self._signer.sign(self._canonical(payload))
        return await self._grants.append(
            GrantRecord(
                exec_id=req.exec_id,
                user_id=req.user_id,
                task_id=req.task_id,
                stage=req.stage,
                points=req.points,
                signature=signature,
                key_version=kid,
                issued_at=now,
                expires_at=expires_at,
                status="active",
            )
        )

    @staticmethod
    def _canonical(payload: dict) -> bytes:
        """规范化序列化：键排序 + 紧凑 JSON（签名确定性，跨端验签一致）。"""
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
