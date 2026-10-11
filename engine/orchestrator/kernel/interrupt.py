"""挂起/恢复：门禁人工干预的控制流。

对齐被替换框架的关键语义（也是最容易复刻错的一条）：**被中断的节点从头重放**，
第二次进入 `interrupt()` 时不再挂起，而是直接交出 resume 值。现网两个门禁节点都把
`interrupt()` 写在节点体第一句，所以重放没有副作用；这个语义由内核显式保证，
而不是靠"恰好写在第一行"的巧合——否则以后有人在 interrupt 前加一行落库，
行为就会静默变成"落两次"。
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from engine.orchestrator.kernel.spec import SpecError

_UNSET = object()


class Interrupt(Exception):
    """节点请求挂起（携带对外可见的挂起载荷，如 {"stage": ..., "message": ...}）。"""

    def __init__(self, payload: Any = None) -> None:
        super().__init__("graph interrupted")
        self.payload = payload


@dataclass(slots=True)
class Resume:
    """`ainvoke` 的恢复输入：把 resume 值交回被挂起的节点。"""

    value: Any = None


@dataclass(slots=True)
class _ResumeContext:
    value: Any = _UNSET
    consumed: bool = False


_RESUME: ContextVar[_ResumeContext | None] = ContextVar("erdos_resume", default=None)


def interrupt(payload: Any = None) -> Any:
    """挂起当前节点；恢复重放时返回 resume 值。"""
    ctx = _RESUME.get()
    if ctx is not None and ctx.value is not _UNSET and not ctx.consumed:
        ctx.consumed = True
        return ctx.value
    if ctx is not None and ctx.consumed:
        raise SpecError("同一节点执行中出现第二个挂起点；内核只支持每节点 1 个 interrupt()")
    raise Interrupt(payload)


@contextmanager
def fresh_execution() -> Iterator[None]:
    """首次进入节点：没有 resume 值，`interrupt()` 应当挂起。"""
    token = _RESUME.set(_ResumeContext())
    try:
        yield
    finally:
        _RESUME.reset(token)


@contextmanager
def resumed_execution(value: Any) -> Iterator[None]:
    """恢复重放：`interrupt()` 交出 value（且只交一次）。"""
    token = _RESUME.set(_ResumeContext(value=value))
    try:
        yield
    finally:
        _RESUME.reset(token)
