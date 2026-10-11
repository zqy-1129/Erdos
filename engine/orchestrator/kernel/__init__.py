"""自研编排内核（替换 LangGraph 的那一层）。

作用域刻意收窄：只支撑本仓真实存在的两张图——四阶段主图（带门禁挂起/恢复）与
求解内循环子图（带预算回边）。不支持并行超步、子图、reducer 与流式，
超出即 build 期报错（见 `spec.GraphSpec.validate`），而不是静默降级。
"""

from engine.orchestrator.kernel.channels import Channels
from engine.orchestrator.kernel.interrupt import Interrupt, Resume, interrupt
from engine.orchestrator.kernel.introspect import GraphView, StateView, TaskView
from engine.orchestrator.kernel.runtime import Graph, thread_id_of
from engine.orchestrator.kernel.spec import END, START, Entry, GraphSpec, RouterFn, SpecError
from engine.orchestrator.kernel.threads import Pending, ThreadState, ThreadStore

__all__ = [
    "END",
    "START",
    "Channels",
    "Entry",
    "Graph",
    "GraphSpec",
    "GraphView",
    "Interrupt",
    "Pending",
    "Resume",
    "RouterFn",
    "SpecError",
    "StateView",
    "TaskView",
    "ThreadState",
    "ThreadStore",
    "interrupt",
    "thread_id_of",
]
