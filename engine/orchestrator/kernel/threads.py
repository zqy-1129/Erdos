"""线程态存储：`thread_id`（= task_id）→ 通道快照 + 待恢复挂起点。

这一层是被替换框架里最容易被误判"可以顺手删掉"的部分：它是**内存图通道**的续状态，
与磁盘上的业务检查点（`engine/checkpoint/store.py`）是两层不同的东西、承担不同职责——
`run_current_stage()` 每次调用都触发一次全新的 `ainvoke`，靠这里把 `stage_index`/`results`
带到下一次。删掉它不会报错，只会让每个阶段都从 analysis 重来。

状态是**就地可变**的（不是写时复制）：一个超步完成后就应当可见，这样后续节点抛错时
已完成的超步不会凭空消失——与被替换框架的检查点时机一致。
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from engine.orchestrator.kernel.channels import Channels


@dataclass(slots=True)
class Pending:
    """挂起中的节点名与对外载荷（恢复时重放该节点）。"""

    node: str
    payload: Any = None


@dataclass(slots=True)
class ThreadState:
    channels: Channels
    pending: Pending | None = None


class ThreadStore:
    """进程内线程态；一张图一个实例，与既有 `InMemorySaver()` 的作用域一致。"""

    def __init__(self) -> None:
        self._states: dict[str, ThreadState] = {}

    def load_or_create(self, thread_id: str, declared: Iterable[str]) -> ThreadState:
        state = self._states.get(thread_id)
        if state is None:
            state = ThreadState(channels=Channels(declared))
            self._states[thread_id] = state
        return state

    def peek(self, thread_id: str) -> ThreadState | None:
        return self._states.get(thread_id)
