"""通道存储：graph 状态的键集与合并语义。

被替换框架对本项目用到的两张图提供的是 **last-write-wins**（无 reducer）：节点返回
`{"results": <整份新 dict>}`，同名键整体覆盖。现网节点都先 `dict(state[...])` 复制再改，
所以整体覆盖是正确语义；这里刻意不做深拷贝、不做 append 合并——加了 reducer 会让
`results` 串阶段，属于行为差异而非加固。
"""

from collections.abc import Iterable, Mapping
from typing import Any

from engine.orchestrator.kernel.spec import SpecError


class Channels:
    """声明式键集 + 覆盖式更新。"""

    __slots__ = ("_declared", "_values")

    def __init__(self, declared: Iterable[str], values: Mapping[str, Any] | None = None) -> None:
        self._declared = frozenset(declared)
        self._values: dict[str, Any] = dict(values or {})
        unknown = set(self._values) - self._declared
        if unknown:
            raise SpecError(
                f"初始状态含未声明通道：{sorted(unknown)}（已声明：{sorted(self._declared)}）"
            )

    def values(self) -> dict[str, Any]:
        """交给节点/路由/快照的副本；值对象本身不深拷贝（与既有实现一致）。"""
        return dict(self._values)

    def apply(self, update: Mapping[str, Any] | None) -> None:
        if not update:
            return
        for key in update:
            if key not in self._declared:
                # 静默丢弃未声明键 = 产物字段漂移的源头（本项目反复踩"两处各写一遍"），
                # 所以 fail-loud：拼错的键要在第一次跑过时就被抓出来。
                raise SpecError(
                    f"节点写入未声明通道：{key!r}（已声明：{sorted(self._declared)}）"
                )
        self._values.update(update)
