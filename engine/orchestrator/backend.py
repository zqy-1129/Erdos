"""编排图的后端选择层（被替换框架 ↔ 自研内核）。

分界线刻意画在这里：**业务编排逻辑（节点体、阶段顺序约束、门禁决策、恢复定位）
单源在 `graph.py`/`solve_loop.py`**，本层只负责"把这些节点装配成一张可跑的图"以及
挂起/恢复两个原语。这样默认值从 langgraph 翻到 native 时，进生产的仍然是那套被既有用例
钉住的逻辑，唯一变的是框架层——而框架层正是双轨比对要盯的东西。

（曾考虑把现网实现原样复制一份到 `graph_langgraph.py` 以换取"回滚路径字节不变"，
但那样翻默认值时上线的是 200 行重复重写的编排逻辑，风险更大；回滚面已由
`ERDOS_ORCH_IMPL` 环境变量提供，不需要靠复制来获得。）
"""

import os
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from engine.orchestrator.kernel import END, START

IMPL_ENV = "ERDOS_ORCH_IMPL"
IMPL_LANGGRAPH = "langgraph"
IMPL_NATIVE = "native"
VALID_IMPLS = (IMPL_LANGGRAPH, IMPL_NATIVE)

NodeFn = Callable[..., Any]
RouterFn = Callable[..., str]
SuspendFn = Callable[[Any], Any]


def orchestration_impl() -> str:
    """当前编排后端；默认 langgraph（P5 翻默认值时只改这一处默认值）。"""
    value = (os.environ.get(IMPL_ENV) or IMPL_LANGGRAPH).strip().lower()
    if value not in VALID_IMPLS:
        raise ValueError(f"{IMPL_ENV} 只能是 {VALID_IMPLS} 之一，收到 {value!r}")
    return value


class CompiledGraph(Protocol):
    """已编译图的统一调用面（两套后端各自实现，调用方不感知差异）。"""

    async def run(self, seed: Mapping[str, Any] | None) -> dict:
        """从入口跑一次至挂起或终点，返回通道快照。"""

    async def resume(self, value: Any) -> dict:
        """把 resume 值交回挂起点并续跑至挂起或终点，返回通道快照。"""

    def raw(self) -> Any:
        """图自省对象（`get_graph()`/`get_state()` 同形，供既有结构化断言复用）。"""


def build_stage_graph(
    *,
    task_id: str,
    schema: Any,
    channels: tuple[str, ...],
    static: Mapping[str, str],
    entry_map: Mapping[str, str],
    route: RouterFn,
    make_nodes: Callable[[SuspendFn], Mapping[str, NodeFn]],
    impl: str | None = None,
) -> CompiledGraph:
    """装配四阶段主图；`make_nodes(suspend)` 用后端提供的挂起原语构造节点体。"""
    chosen = impl or orchestration_impl()
    if chosen == IMPL_LANGGRAPH:
        from engine.orchestrator.langgraph_backend import LangGraphStageGraph

        return LangGraphStageGraph(
            task_id=task_id, schema=schema, static=static,
            entry_map=entry_map, route=route, make_nodes=make_nodes,
        )
    from engine.orchestrator.native_backend import NativeStageGraph

    return NativeStageGraph(
        task_id=task_id, channels=channels, static=static,
        entry_map=entry_map, route=route, make_nodes=make_nodes,
    )


def build_loop_graph(
    *,
    schema: Any,
    channels: tuple[str, ...],
    entry: str,
    nodes: Mapping[str, NodeFn],
    static: Mapping[str, str],
    conditional: Mapping[str, tuple[RouterFn, Mapping[str, str]]],
    impl: str | None = None,
) -> CompiledGraph:
    """装配求解内循环（无挂起、无线程态；跑一次到收敛）。"""
    chosen = impl or orchestration_impl()
    if chosen == IMPL_LANGGRAPH:
        from engine.orchestrator.langgraph_backend import LangGraphLoopGraph

        return LangGraphLoopGraph(
            schema=schema, entry=entry, nodes=nodes, static=static, conditional=conditional
        )
    from engine.orchestrator.native_backend import NativeLoopGraph

    return NativeLoopGraph(
        channels=channels, entry=entry, nodes=nodes, static=static, conditional=conditional
    )


__all__ = [
    "END",
    "IMPL_ENV",
    "IMPL_LANGGRAPH",
    "IMPL_NATIVE",
    "START",
    "VALID_IMPLS",
    "CompiledGraph",
    "build_loop_graph",
    "build_stage_graph",
    "orchestration_impl",
]
