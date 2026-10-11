"""自研内核后端（`engine/orchestrator/kernel/` 的适配层）。

与 `langgraph_backend.py` 严格对称：同样的中性拓扑描述进来，同样的 `CompiledGraph`
调用面出去。两套后端可互换这一条本身由 `test_orchestrator_backend.py` 守护。
"""

from collections.abc import Callable, Mapping
from typing import Any

from engine.orchestrator.backend import CompiledGraph, NodeFn, RouterFn, SuspendFn
from engine.orchestrator.kernel import Entry, Graph, GraphSpec, Resume, interrupt
from engine.orchestrator.kernel.threads import ThreadStore

# 内循环没有跨调用续状态，thread_id 只是内核线程态表的键；一个 SolveLoop 一个图实例。
_LOOP_THREAD_ID = "solve-loop"


class NativeStageGraph(CompiledGraph):
    """四阶段主图：内核 GraphSpec + ThreadStore（线程态）+ kernel.interrupt。"""

    def __init__(
        self,
        *,
        task_id: str,
        channels: tuple[str, ...],
        static: Mapping[str, str],
        entry_map: Mapping[str, str],
        route: RouterFn,
        make_nodes: Callable[[SuspendFn], Mapping[str, NodeFn]],
    ) -> None:
        self._config: dict[str, Any] = {"configurable": {"thread_id": task_id}}
        spec = GraphSpec(
            name="stage-orchestrator",
            channels=channels,
            nodes=dict(make_nodes(interrupt)),
            entry=Entry(router=route, router_map=dict(entry_map)),
            static=dict(static),
        )
        self._graph = Graph(spec, threads=ThreadStore())

    async def run(self, seed: Mapping[str, Any] | None) -> dict:
        return await self._graph.ainvoke(dict(seed or {}), self._config)

    async def resume(self, value: Any) -> dict:
        return await self._graph.ainvoke(Resume(value), self._config)

    def raw(self) -> Any:
        return self._graph


class NativeLoopGraph(CompiledGraph):
    """求解内循环：带预算回边的循环图，无挂起点。"""

    def __init__(
        self,
        *,
        channels: tuple[str, ...],
        entry: str,
        nodes: Mapping[str, NodeFn],
        static: Mapping[str, str],
        conditional: Mapping[str, tuple[RouterFn, Mapping[str, str]]],
    ) -> None:
        spec = GraphSpec(
            name="solve-loop",
            channels=channels,
            nodes=dict(nodes),
            entry=Entry(node=entry),
            static=dict(static),
            conditional={k: (v[0], dict(v[1])) for k, v in conditional.items()},
        )
        self._graph = Graph(spec)

    async def run(self, seed: Mapping[str, Any] | None) -> dict:
        return await self._graph.ainvoke(
            dict(seed or {}), {"configurable": {"thread_id": _LOOP_THREAD_ID}}
        )

    async def resume(self, value: Any) -> dict:
        raise NotImplementedError("求解内循环没有挂起点，不支持 resume")

    def raw(self) -> Any:
        return self._graph
