"""积分域并发安全测试（SP2-4 资金域）：原子扣减防超扣 + 幂等并发。

验证：同账户多并发 reserve，余额不出现负数（超扣），流水无重复入账。
"""

import asyncio
from datetime import UTC, datetime

from app.domain.points.ports import BalanceType, ReserveRequest
from app.domain.points.service import PointsService
from app.repository.points import (
    SQLAlchemyGrantRepository,
    SQLAlchemyLedgerRepository,
    SQLAlchemyPointAccountRepository,
)
from app.repository.uow import UnitOfWork


class FakeSigner:
    def sign(self, payload: bytes) -> tuple[str, str]:
        return "deadbeef" * 8, "test-kid"


async def test_concurrent_reserve_no_overdraft(session_factory, settings) -> None:
    """同账户 20 并发预扣：总额不超过余额，无超扣、无重复流水。

    SQLite 单写者 + 原子 UPDATE ... WHERE balance >= n 保证扣减原子；
    每个并发用独立事务（独立会话），冲突时靠原子条件更新兜底。
    """
    async with UnitOfWork(session_factory) as uow:
        repo = SQLAlchemyPointAccountRepository(uow.session)
        await repo.get_or_create("u1", datetime.now(UTC))
        await repo.credit("u1", BalanceType.PURCHASED, 100, datetime.now(UTC))

    # 20 并发，各预扣 10（总额 200 > 余额 100），只能成功 ~10 笔
    async def one_reserve(i: int) -> bool:
        async with UnitOfWork(session_factory) as uow:
            svc = PointsService(
                SQLAlchemyPointAccountRepository(uow.session),
                SQLAlchemyLedgerRepository(uow.session),
                SQLAlchemyGrantRepository(uow.session),
                FakeSigner(),
                settings,
            )
            try:
                await svc.reserve(
                    ReserveRequest(f"exec-{i}", "u1", "t", "analysis", 10),
                    datetime.now(UTC),
                )
                return True
            except Exception:
                return False

    results = await asyncio.gather(*(one_reserve(i) for i in range(20)))
    succeeded = sum(results)

    async with session_factory() as s:
        bal = await SQLAlchemyPointAccountRepository(s).get("u1")
        items, total = await SQLAlchemyLedgerRepository(s).list_by_user("u1", 100, 0)
        reserves = [it for it in items if it.kind == "reserve"]

    assert succeeded == 10  # 100 分只能满足 10 笔 10 分预扣
    assert bal is not None and bal.purchased_balance == 0  # 余额扣净，不为负
    assert len(reserves) == 10  # 成功入账的流水数等于成功预扣数，无重复
    # 无超扣：所有成功流水 delta 之和 == 100
    assert sum(-it.delta for it in reserves) == 100


async def test_concurrent_same_exec_id_idempotent(session_factory, settings) -> None:
    """同 exec_id 并发预扣：只成功一次，其余幂等返回，不重复扣减。"""
    async with UnitOfWork(session_factory) as uow:
        repo = SQLAlchemyPointAccountRepository(uow.session)
        await repo.get_or_create("u1", datetime.now(UTC))
        await repo.credit("u1", BalanceType.PURCHASED, 100, datetime.now(UTC))

    async def one_reserve() -> bool:
        async with UnitOfWork(session_factory) as uow:
            svc = PointsService(
                SQLAlchemyPointAccountRepository(uow.session),
                SQLAlchemyLedgerRepository(uow.session),
                SQLAlchemyGrantRepository(uow.session),
                FakeSigner(),
                settings,
            )
            try:
                await svc.reserve(
                    ReserveRequest("exec-same", "u1", "t", "analysis", 30),
                    datetime.now(UTC),
                )
                return True
            except Exception:
                return False

    results = await asyncio.gather(*(one_reserve() for _ in range(10)))

    async with session_factory() as s:
        bal = await SQLAlchemyPointAccountRepository(s).get("u1")
        items, total = await SQLAlchemyLedgerRepository(s).list_by_user("u1", 100, 0)
        reserves = [it for it in items if it.kind == "reserve"]

    assert sum(results) >= 1  # 至少一笔成功
    assert bal is not None and bal.purchased_balance == 70  # 只扣一次 30
    assert len(reserves) == 1  # 同 exec_id 只一条流水
