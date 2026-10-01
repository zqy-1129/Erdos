"""通用仓储基类：承载跨域通用的技术性数据操作（add/get/list/delete）。

领域专属查询逻辑放在各域的仓储实现中，禁止在上层直接拼 SQL。
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.repository.models import Base


class SQLAlchemyRepository[ModelT: Base]:
    """SQLAlchemy 异步仓储基类（可直接实例化，也可按域继承扩展）。

    会话由外部（UnitOfWork/请求作用域）注入，仓储自身不管理会话生命周期，
    事务提交统一收敛到 UnitOfWork，保证「事务边界单提交」。
    """

    def __init__(self, session: AsyncSession, model: type[ModelT]) -> None:
        self._session = session
        self._model = model

    @property
    def session(self) -> AsyncSession:
        return self._session

    async def add(self, entity: ModelT) -> None:
        """登记新增（flush 以便获取自增主键与尽早暴露约束冲突）。"""
        self._session.add(entity)
        await self._session.flush()

    async def get(self, ident: Any) -> ModelT | None:
        """按主键加载实体。"""
        return await self._session.get(self._model, ident)

    async def list(self, *, limit: int, offset: int = 0) -> tuple[list[ModelT], int]:
        """分页列表：返回 (items, total)。"""
        if limit < 1 or offset < 0:
            raise ValueError("limit 必须 ≥1 且 offset 必须 ≥0")
        items = (
            (await self._session.execute(select(self._model).order_by(self._model.id)
                                         .limit(limit).offset(offset)))
            .scalars()
            .all()
        )
        total = int(
            (await self._session.execute(select(func.count()).select_from(self._model))).scalar_one()
        )
        return list(items), total

    async def delete(self, entity: ModelT) -> None:
        """登记删除（提交由 UnitOfWork 统一执行）。"""
        await self._session.delete(entity)
        await self._session.flush()