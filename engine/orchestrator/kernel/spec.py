"""自研编排内核——图的声明式定义与 build 期校验。

刻意保持"小"：内核只服务两张真实存在的图（四阶段主图、求解内循环子图），
都是**单后继**拓扑（静态边与条件路由二选一）。并行超步、子图、map-reduce、
reducer 注册表一律不支持——一旦写出 fan-out，build 期直接报错，而不是静默
降级成串行（改变执行语义却不出错，是最坏的那种漂移）。
"""

import ast
import inspect
import textwrap
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass

END = "__end__"
START = "__start__"

NodeFn = Callable[[dict], Awaitable[dict]]
RouterFn = Callable[[dict], str]


class SpecError(ValueError):
    """图定义不合法（build 期或运行期检出的编排契约违反）。"""


@dataclass(frozen=True, slots=True)
class Entry:
    """入口：固定节点或条件路由（二者取其一）。"""

    node: str | None = None
    router: RouterFn | None = None
    router_map: Mapping[str, str] | None = None

    def resolve(self, state: dict) -> str:
        if self.node is not None:
            return self.node
        if self.router is None or self.router_map is None:
            raise SpecError("入口既未指定节点，也未指定完整路由")
        label = self.router(state)
        target = self.router_map.get(label)
        if target is None:
            raise SpecError(f"入口路由返回未登记的分支：{label!r}（可用：{sorted(self.router_map)}）")
        return target


class GraphSpec:
    """节点表 + 后继关系；`validate()` 在构建时执行，运行期不再复查结构。"""

    def __init__(
        self,
        *,
        name: str,
        channels: Iterable[str],
        nodes: Mapping[str, NodeFn],
        entry: Entry,
        static: Mapping[str, str] | None = None,
        conditional: Mapping[str, tuple[RouterFn, Mapping[str, str]]] | None = None,
    ) -> None:
        self.name = name
        self.channels = tuple(channels)
        self.nodes = dict(nodes)
        self.entry = entry
        self.static = dict(static or {})
        self.conditional = {k: (v[0], dict(v[1])) for k, v in (conditional or {}).items()}
        self.validate()

    # ------------------------------------------------------------------
    def validate(self) -> None:
        if not self.nodes:
            raise SpecError(f"{self.name}: 没有任何节点")
        for node in (*self.static, *self.conditional):
            self._require_node(node)

        for node, target in self.static.items():
            if node in self.conditional:
                raise SpecError(f"{self.name}: 节点 {node} 同时有静态边与条件路由（fan-out 不支持）")
            self._require_target(node, target)

        for node, (_router, route_map) in self.conditional.items():
            if not route_map:
                raise SpecError(f"{self.name}: 节点 {node} 的条件路由没有可选分支")
            for label, target in route_map.items():
                self._require_target(node, target, label=label)

        self._validate_entry()

        unreachable = sorted(set(self.nodes) - self._reachable())
        if unreachable:
            raise SpecError(f"{self.name}: 节点从入口不可达：{unreachable}")

        # 可达但没有后继的节点会在运行期走到一半卡住；结构上就能查出，不留到线上
        dead_ends = sorted(
            node
            for node in self._reachable()
            if node not in self.static and node not in self.conditional
        )
        if dead_ends:
            raise SpecError(f"{self.name}: 节点缺少后继（既无静态边也无条件路由）：{dead_ends}")

        for node, count in self._interrupt_sites().items():
            if count > 1:
                raise SpecError(
                    f"{self.name}: 节点 {node} 含 {count} 个挂起点；内核只支持每节点 1 个 interrupt()"
                )

    def _validate_entry(self) -> None:
        if self.entry.node is not None:
            self._require_node(self.entry.node)
            return
        if self.entry.router is None or self.entry.router_map is None:
            raise SpecError(f"{self.name}: 入口未指定节点或路由")
        for label, target in self.entry.router_map.items():
            self._require_target(START, target, label=f"入口:{label}")

    def _entry_targets(self) -> set[str]:
        if self.entry.node is not None:
            return {self.entry.node}
        if self.entry.router_map is not None:
            return set(self.entry.router_map.values())
        return set()

    def _reachable(self) -> set[str]:
        """从入口可达的节点闭包（含回边，故用工作表而非树遍历）。"""
        seen: set[str] = set()
        frontier = list(self._entry_targets())
        while frontier:
            node = frontier.pop()
            if node in seen:
                continue
            seen.add(node)
            if node in self.static:
                if self.static[node] != END:
                    frontier.append(self.static[node])
            elif node in self.conditional:
                _router, route_map = self.conditional[node]
                frontier.extend(t for t in route_map.values() if t != END)
            # 既无静态边又无条件路由：不展开，交给后面的 dead_ends 报错
        return seen

    def _require_node(self, name: str) -> None:
        if name not in self.nodes:
            raise SpecError(f"{self.name}: 未知节点 {name!r}（已登记：{sorted(self.nodes)}）")

    def _require_target(self, node: str, target: str, label: str = "") -> None:
        if target == END:
            return
        if target not in self.nodes:
            suffix = f"，分支 {label!r}" if label else ""
            raise SpecError(f"{self.name}: 节点 {node}{suffix} 指向未登记目标 {target!r}")

    def _interrupt_sites(self) -> dict[str, int]:
        """静态计数每个节点里的 `interrupt()` 调用；取不到源码的动态节点跳过。"""
        counts: dict[str, int] = {}
        for node, fn in self.nodes.items():
            try:
                source = textwrap.dedent(inspect.getsource(fn))
            except (OSError, TypeError):  # 测试替身等动态构造的节点：无法静态检出，交给运行期兜底
                continue
            found = 0
            for child in ast.walk(ast.parse(source)):
                if not isinstance(child, ast.Call):
                    continue
                func = child.func
                if isinstance(func, ast.Name) and func.id == "interrupt":
                    found += 1
                elif isinstance(func, ast.Attribute) and func.attr == "interrupt":
                    found += 1
            counts[node] = found
        return counts

    # ------------------------------------------------------------------
    def next_node(self, node: str, state: dict) -> str:
        """节点执行完成后的后继（先应用通道更新，再求路由——与 superstep 语义一致）。"""
        if node in self.static:
            return self.static[node]
        router, route_map = self.conditional[node]
        label = router(state)
        target = route_map.get(label)
        if target is None:
            raise SpecError(f"{self.name}: 节点 {node} 的路由返回未登记分支 {label!r}")
        return target
