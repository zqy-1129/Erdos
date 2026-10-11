"""内核运行器：单后继超步循环 + 挂起/恢复 + 线程态。

需要与被替换框架逐字对齐的三条语义（复刻错就是数据问题，不是崩溃）：

1. **挂起收场**：节点 `raise Interrupt` 时，此前已完成超步的通道更新留在快照里并作为
   返回值交出——`run_current_stage` 正是靠它拿到本阶段产出并落检查点。
2. **恢复即重放**：`Resume` 让被挂起的节点从头再跑一次，节点内的 `interrupt()` 这次
   交出 resume 值（见 `kernel/interrupt.py`）。
3. **END 之后再进来 = 从入口重路由**：`ainvoke({})` 不清通道、不续跑，而是按现有
   `stage_index` 重走入口路由。四阶段编排"一次调用跑一个阶段"完全建立在这条上。
"""

from collections.abc import Mapping
from typing import Any

from engine.orchestrator.kernel.interrupt import (
    Interrupt,
    Resume,
    fresh_execution,
    resumed_execution,
)
from engine.orchestrator.kernel.introspect import StateView, build_graph_view, build_state_view
from engine.orchestrator.kernel.spec import END, GraphSpec, SpecError
from engine.orchestrator.kernel.threads import Pending, ThreadState, ThreadStore


def thread_id_of(config: Mapping[str, Any] | None) -> str:
    configurable = (config or {}).get("configurable") or {}
    thread_id = configurable.get("thread_id")
    if not thread_id:
        raise SpecError("config 缺少 configurable.thread_id")
    return str(thread_id)


class Graph:
    """编译后的可执行图；实例持有一条条线程态（与被替换框架的 per-compile 内存 saver 同作用域）。"""

    def __init__(self, spec: GraphSpec, *, threads: ThreadStore | None = None) -> None:
        self._spec = spec
        self._threads = threads or ThreadStore()

    @property
    def spec(self) -> GraphSpec:
        return self._spec

    # ------------------------------------------------------------------
    async def ainvoke(self, input: Mapping[str, Any] | Resume | None, config: Mapping[str, Any]) -> dict:
        thread_id = thread_id_of(config)
        state = self._threads.load_or_create(thread_id, self._spec.channels)

        if isinstance(input, Resume):
            pending = state.pending
            if pending is None:
                raise SpecError(f"线程 {thread_id} 没有待恢复的挂起点（重复应答，或阶段尚未执行）")
            return await self._resume(state, pending.node, input.value)

        if input:
            state.channels.apply(dict(input))
        return await self._loop(state, self._spec.entry.resolve(state.channels.values()))

    # ------------------------------------------------------------------
    async def _loop(self, state: ThreadState, current: str) -> dict:
        while current != END:
            try:
                with fresh_execution():
                    update = await self._spec.nodes[current](state.channels.values())
            except Interrupt as exc:
                state.pending = Pending(node=current, payload=exc.payload)
                return state.channels.values()
            state.channels.apply(update)
            current = self._spec.next_node(current, state.channels.values())
        state.pending = None
        return state.channels.values()

    async def _resume(self, state: ThreadState, node: str, value: Any) -> dict:
        with resumed_execution(value):
            update = await self._spec.nodes[node](state.channels.values())
        state.channels.apply(update)
        state.pending = None
        return await self._loop(state, self._spec.next_node(node, state.channels.values()))

    # ------------------------------------------------------------------
    def get_state(self, config: Mapping[str, Any]) -> StateView:
        return build_state_view(self._threads.peek(thread_id_of(config)))

    def get_graph(self) -> Any:
        return build_graph_view(self._spec)
