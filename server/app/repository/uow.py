"""事务边界（UnitOfWork）：一个业务用例一个事务、一次提交。

进入上下文创建会话；正常退出自动提交，异常自动回滚；
会话生命周期与本类绑定，杜绝会话泄漏。
"""

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class UnitOfWork:
    """业务用例级事务边界。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory
        self._session: AsyncSession | None = None

    @property
    def session(self) -> AsyncSession:
        """当前事务会话（仅限在 with 块内访问）。"""
        if self._session is None:
            raise RuntimeError("UnitOfWork 尚未进入上下文，禁止访问会话")
        return self._session

    async def __aenter__(self) -> Self:
        self._session = self._factory()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        assert self._session is not None
        try:
            if exc_type is None:
                await self._session.commit()  # 单提交语义
            else:
                await self._session.rollback()
        finally:
            await self._session.close()
        return False