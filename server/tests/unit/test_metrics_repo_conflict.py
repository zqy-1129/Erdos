"""撞键兜底形状测试：presence 分钟桶与用户日统计都不得回滚调用方事务。

竞态要两个执行体交错才会出现（单会话里造不出"判空之后被插队"的确定性时序），所以这里
用替身把"第一次 flush 撞键"钉住，被测的是仓储自己的控制流——同 `test_monitoring_repo.py`。

判据两层：
- 会话回滚必须为 0、保存点回滚必须为 1。调用方（`api/v1/auth.py` 注册、
  `api/v1/account.py` 注销、`infra/presence_aggregator.py` 整批落库）都在同一个会话里
  已经写了别的行，整会话回滚会把它们一起抹掉。
- `add()` 必须发生在保存点内部。只把 flush 包进保存点，失败的实例仍挂在会话上，
  后续每一步都以 PendingRollbackError 收场。
"""

from datetime import UTC, date, datetime
from types import SimpleNamespace

from sqlalchemy.exc import IntegrityError

from app.repository.metrics import (
    SQLAlchemyOnlineTrendRepository,
    SQLAlchemyUsersTrendRepository,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
STAT_DATE = date(2026, 10, 1)


class _Savepoint:
    def __init__(self, owner: "_ConflictSession") -> None:
        self._owner = owner

    async def __aenter__(self) -> "_Savepoint":
        self._owner.savepoint_depth += 1
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        self._owner.savepoint_depth -= 1
        if exc_type is not None:
            self._owner.savepoint_rollbacks += 1
        return False


class _ConflictSession:
    """第一次 SELECT 查空、第一次 flush 撞唯一键的会话替身。"""

    def __init__(self, existing_row: SimpleNamespace) -> None:
        self.existing_row = existing_row
        self.selects = 0
        self.flushes = 0
        self.session_rollbacks = 0
        self.savepoint_rollbacks = 0
        self.savepoint_depth = 0
        self.adds_outside_savepoint = 0

    async def execute(self, _stmt):
        self.selects += 1
        row = None if self.selects == 1 else self.existing_row

        class _Result:
            def scalar_one_or_none(self) -> SimpleNamespace | None:
                return row

            def scalar_one(self) -> SimpleNamespace:
                return row

        return _Result()

    def add(self, _obj) -> None:
        if self.savepoint_depth == 0:
            self.adds_outside_savepoint += 1

    async def flush(self) -> None:
        self.flushes += 1
        if self.flushes == 1:
            raise IntegrityError(
                "INSERT INTO presence_minute_agg ...",
                {},
                Exception("UNIQUE constraint failed"),
            )

    def begin_nested(self) -> _Savepoint:
        return _Savepoint(self)

    async def rollback(self) -> None:
        self.session_rollbacks += 1


async def test_online_trend_upsert_conflict_keeps_caller_transaction() -> None:
    row = SimpleNamespace(minute_ts=NOW, online_count=3)
    session = _ConflictSession(row)
    repo = SQLAlchemyOnlineTrendRepository(session)  # type: ignore[arg-type]

    created = await repo.upsert_minute(NOW, 7)

    assert session.session_rollbacks == 0, "撞键不得回滚调用方事务"
    assert session.savepoint_rollbacks == 1
    assert session.adds_outside_savepoint == 0, "新行必须连同插入一起放进保存点"
    assert created is False, "撞键后是转更新，不是又建了一行"
    assert row.online_count == 7, "撞键要把这次的值写进去而不是丢掉"


async def test_users_daily_delta_conflict_keeps_caller_transaction() -> None:
    row = SimpleNamespace(
        stat_date=STAT_DATE, total_users=10, new_users=4, active_users=2
    )
    session = _ConflictSession(row)
    repo = SQLAlchemyUsersTrendRepository(session)  # type: ignore[arg-type]

    await repo.apply_daily_delta(STAT_DATE, total_delta=1, new_delta=1, active_delta=0)

    assert session.session_rollbacks == 0, "注册/注销已写的账号与流水不能被这次撞键带走"
    assert session.savepoint_rollbacks == 1
    assert session.adds_outside_savepoint == 0
    assert (row.total_users, row.new_users, row.active_users) == (11, 5, 2)
