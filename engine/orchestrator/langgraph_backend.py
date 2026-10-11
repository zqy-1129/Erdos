"""LangGraph 后端（过渡期实现；自研内核接管后本文件与 `pyproject` 里的依赖一起删除）。

本文件是仓内**唯一**允许出现 `langgraph` import 的地方（P7 起依赖边界守护按"全仓零出现"
判定）。除装配与两个原语（挂起、恢复）之外不含任何业务语义——阶段顺序、门禁决策、
恢复定位都在 `graph.py`/`solve_loop.py`，两套后端共用同一份。
"""

from collections.abc import Callable, Mapping
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END as LG_END
from langgraph.graph import START as LG_START
from langgraph.graph import StateGraph
from langgraph.types import Command
from langgraph.types import interrupt as lg_interrupt

from engine.orchestrator.backend import END, START, CompiledGraph, NodeFn, RouterFn, SuspendFn

# 中性哨兵 → 框架哨兵（当前两侧字面量相同，显式映射以免依赖这个巧合）
_SENTINELS = {END: LG_END, START: LG_START}


def _lg(target: str) -> str:
    return _SENTINELS.get(target, target)


class LangGraphStageGraph(CompiledGraph):
    """四阶段主图：StateGraph + InMemorySaver（线程态）+ interrupt/Command。"""

    def __init__(
        self,
        *,
        task_id: str,
        schema: Any,
        static: Mapping[str, str],
        entry_map: Mapping[str, str],
        route: RouterFn,
        make_nodes: Callable[[SuspendFn], Mapping[str, NodeFn]],
    ) -> None:
        builder = StateGraph(schema)
        for name, node in make_nodes(lg_interrupt).items():
            builder.add_node(name, node)
        for source, target in static.items():
            builder.add_edge(source, _lg(target))
        builder.add_conditional_edges(
            LG_START, route, {label: _lg(target) for label, target in entry_map.items()}
        )
        # 框架侧 ainvoke 的重载签名（RunnableConfig/Command[T]）与本层中性调用面不匹配；
        # 本文件在自研内核接管后整体删除，此处按 Any 收口，行为由双轨用例把关。
        self._graph: Any = builder.compile(checkpointer=InMemorySaver())
        self._config: dict[str, Any] = {"configurable": {"thread_id": task_id}}

    async def run(self, seed: Mapping[str, Any] | None) -> dict:
        return await self._graph.ainvoke(dict(seed or {}), self._config)

    async def resume(self, value: Any) -> dict:
        return await self._graph.ainvoke(Command(resume=value), self._config)

    def raw(self) -> Any:
        return self._graph


class LangGraphLoopGraph(CompiledGraph):
    """求解内循环：无 checkpointer、无挂起点，单进程内跑到收敛。"""

    def __init__(
        self,
        *,
        schema: Any,
        entry: str,
        nodes: Mapping[str, NodeFn],
        static: Mapping[str, str],
        conditional: Mapping[str, tuple[RouterFn, Mapping[str, str]]],
    ) -> None:
        builder = StateGraph(schema)
        for name, node in nodes.items():
            builder.add_node(name, node)
        builder.add_edge(LG_START, entry)
        for source, target in static.items():
            builder.add_edge(source, _lg(target))
        for source, (router, route_map) in conditional.items():
            builder.add_conditional_edges(
                source, router, {label: _lg(target) for label, target in route_map.items()}
            )
        self._graph: Any = builder.compile()

    async def run(self, seed: Mapping[str, Any] | None) -> dict:
        return await self._graph.ainvoke(dict(seed or {}))

    async def resume(self, value: Any) -> dict:
        raise NotImplementedError("求解内循环没有挂起点，不支持 resume")

    def raw(self) -> Any:
        return self._graph
