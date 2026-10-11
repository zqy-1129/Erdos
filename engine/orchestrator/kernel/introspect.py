"""图自省视图。

**刻意与被替换框架的字段形状一致**：`.nodes` 迭代出节点名（含入口/终点哨兵）、
`.next` 是挂起节点元组、`.tasks` 的长度表示有无待恢复挂起点。这样既有的结构化断言
可以一行不改地同时跑在两套实现上（见
`reports/引擎编排内核自研替换_技术设计与依赖矩阵_2026-10-11.md` §7.2）——
只改测试文件名而不改断言，才是"复用全量测试用例"的诚实口径。
"""

from dataclasses import dataclass
from typing import Any

from engine.orchestrator.kernel.spec import END, START, GraphSpec
from engine.orchestrator.kernel.threads import ThreadState


@dataclass(frozen=True, slots=True)
class GraphView:
    nodes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TaskView:
    name: str
    interrupts: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class StateView:
    values: dict[str, Any]
    next: tuple[str, ...]
    tasks: tuple[TaskView, ...]


def build_graph_view(spec: GraphSpec) -> GraphView:
    return GraphView(nodes=(START, *spec.nodes, END))


def build_state_view(state: ThreadState | None) -> StateView:
    if state is None:
        return StateView(values={}, next=(), tasks=())
    values = state.channels.values()
    pending = state.pending
    if pending is None:
        return StateView(values=values, next=(), tasks=())
    return StateView(
        values=values,
        next=(pending.node,),
        tasks=(TaskView(name=pending.node, interrupts=(pending.payload,)),),
    )
