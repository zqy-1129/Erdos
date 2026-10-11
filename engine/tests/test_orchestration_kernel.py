"""自研编排内核的语义单测（S1~S11 逐条）。

这里测的是**内核本身**（合成小图），不是业务编排器；业务层的双实现比对见
`test_orchestrator_parity.py`。每条用例标出依赖矩阵里的语义编号，便于评审时逐条核对
"这条语义究竟被哪条断言兜住"（命名即判据）。
"""

import pytest

from engine.orchestrator.kernel import (
    END,
    START,
    Entry,
    Graph,
    GraphSpec,
    Interrupt,
    Resume,
    SpecError,
    interrupt,
)
from engine.orchestrator.kernel.interrupt import fresh_execution, resumed_execution

CFG = {"configurable": {"thread_id": "t1"}}


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


async def _node_ok(state: dict) -> dict:
    return {}


def _recorder(log: list[str], name: str, update: dict | None = None):
    async def node(state: dict) -> dict:
        log.append(name)
        return update or {}

    return node


# ----------------------------------------------------------------------
# S1/S2 —— 拓扑与入口路由
# ----------------------------------------------------------------------
async def test_s1_linear_graph_runs_each_node_once() -> None:
    log: list[str] = []
    spec = GraphSpec(
        name="linear",
        channels=("hits",),
        nodes={"a": _recorder(log, "a", {"hits": 1}), "b": _recorder(log, "b")},
        entry=Entry(node="a"),
        static={"a": "b", "b": END},
    )
    result = await Graph(spec).ainvoke({"hits": 0}, CFG)

    assert log == ["a", "b"]
    assert result["hits"] == 1


async def test_s2_entry_router_selects_by_state() -> None:
    log: list[str] = []

    def route(state: dict) -> str:
        return str(state["stage"])

    spec = GraphSpec(
        name="routed",
        channels=("stage",),
        nodes={"x": _recorder(log, "x"), "y": _recorder(log, "y")},
        entry=Entry(router=route, router_map={"x": "x", "y": "y"}),
        static={"x": END, "y": END},
    )
    await Graph(spec).ainvoke({"stage": "y"}, CFG)
    assert log == ["y"]


async def test_s2_entry_router_unknown_label_fails_loud() -> None:
    def route(state: dict) -> str:
        return "nope"

    spec = GraphSpec(
        name="bad-route",
        channels=("stage",),
        nodes={"x": _node_ok},
        entry=Entry(router=route, router_map={"x": "x"}),
        static={"x": END},
    )
    with pytest.raises(SpecError, match="未登记的分支"):
        await Graph(spec).ainvoke({"stage": "x"}, CFG)


async def test_s2_runtime_error_from_node_propagates_unwrapped() -> None:
    """跳级红线：节点抛的 RuntimeError 必须原样上抛，不被内核包装成其它类型。"""

    async def node(state: dict) -> dict:
        raise RuntimeError("阶段顺序约束：期望 solving，实际 analysis")

    spec = GraphSpec(
        name="order",
        channels=("i",),
        nodes={"n": node},
        entry=Entry(node="n"),
        static={"n": END},
    )
    with pytest.raises(RuntimeError, match="阶段顺序约束"):
        await Graph(spec).ainvoke({"i": 9}, CFG)


# ----------------------------------------------------------------------
# S3 —— 通道 last-write-wins
# ----------------------------------------------------------------------
async def test_s3_channel_update_overwrites_whole_key() -> None:
    async def first(state: dict) -> dict:
        return {"results": {"a": 1}}

    async def second(state: dict) -> dict:
        return {"results": {"b": 2}}

    spec = GraphSpec(
        name="channels",
        channels=("results",),
        nodes={"first": first, "second": second},
        entry=Entry(node="first"),
        static={"first": "second", "second": END},
    )
    result = await Graph(spec).ainvoke({"results": {}}, CFG)
    # 整键覆盖（无 reducer）：想保留上游值必须由节点自己复制——现网节点正是这么写的。
    # 若内核改成 append 合并，results 会跨阶段累积，属于行为差异。
    assert result["results"] == {"b": 2}


async def test_s3_undeclared_channel_rejected() -> None:
    async def typo(state: dict) -> dict:
        return {"result": 1}  # 声明的是 results

    spec = GraphSpec(
        name="typo",
        channels=("results",),
        nodes={"n": typo},
        entry=Entry(node="n"),
        static={"n": END},
    )
    with pytest.raises(SpecError, match="未声明通道"):
        await Graph(spec).ainvoke({}, CFG)


# ----------------------------------------------------------------------
# S7/S8 —— 线程态跨调用 + END 后从入口重入（最容易做错的一条）
# ----------------------------------------------------------------------
async def test_s8_after_end_the_next_ainvoke_re_enters_from_entry() -> None:
    """复刻"一次调用跑一个阶段"：END 后再 `ainvoke({})` 必须重走入口路由、带着既有通道值。"""
    seen: list[str] = []

    def route(state: dict) -> str:
        return f"s{int(state.get('index', 0))}"

    def make(name: str):
        async def node(state: dict) -> dict:
            seen.append(name)
            # 与四阶段主图同形：末阶段 pass 后索引钳在最后一个，不越界
            return {"index": min(int(state.get("index", 0)) + 1, 1)}

        return node

    spec = GraphSpec(
        name="reentry",
        channels=("index",),
        nodes={"s0": make("s0"), "s1": make("s1")},
        entry=Entry(router=route, router_map={"s0": "s0", "s1": "s1"}),
        static={"s0": END, "s1": END},
    )
    graph = Graph(spec)

    await graph.ainvoke({"index": 0}, CFG)
    await graph.ainvoke({}, CFG)
    await graph.ainvoke({}, CFG)

    assert seen == ["s0", "s1", "s1"]  # index 在线程态里带着走，不是每次都从 0 开始


async def test_s7_threads_are_isolated_by_thread_id() -> None:
    async def bump(state: dict) -> dict:
        return {"index": int(state.get("index", 0)) + 1}

    spec = GraphSpec(
        name="threads",
        channels=("index",),
        nodes={"n": bump},
        entry=Entry(node="n"),
        static={"n": END},
    )
    graph = Graph(spec)
    await graph.ainvoke({"index": 0}, _cfg("a"))
    result_b = await graph.ainvoke({}, _cfg("b"))
    result_a = await graph.ainvoke({}, _cfg("a"))

    assert result_b["index"] == 1  # 线程 b 没有种子：从默认 0 起
    assert result_a["index"] == 2  # 线程 a 带着上一次的 1


async def test_missing_thread_id_rejected() -> None:
    spec = GraphSpec(
        name="nothread",
        channels=("x",),
        nodes={"n": _node_ok},
        entry=Entry(node="n"),
        static={"n": END},
    )
    with pytest.raises(SpecError, match="thread_id"):
        await Graph(spec).ainvoke({}, {})


# ----------------------------------------------------------------------
# S5/S6 —— 挂起与恢复重放（gate 节点形状与四阶段主图一致：interrupt 是第一句）
# ----------------------------------------------------------------------
def _gate_graph():
    calls: list[str] = []

    async def stage(state: dict) -> dict:
        results = dict(state.get("results") or {})
        if "analysis" in results:
            return {"results": results}  # 恢复/重入路径：阶段产物已在，跳过执行不重放副作用
        calls.append("stage")
        return {"results": {**results, "analysis": {"data": 1}}}

    async def gate(state: dict) -> dict:
        payload = interrupt({"stage": "analysis", "message": "等待门禁评审"})
        calls.append("gate")
        return {"decision": payload}

    async def finish(state: dict) -> dict:
        calls.append("finish")
        return {}

    spec = GraphSpec(
        name="gate",
        channels=("results", "decision"),
        nodes={"stage": stage, "gate": gate, "finish": finish},
        entry=Entry(node="stage"),
        static={"stage": "gate", "gate": "finish", "finish": END},
    )
    return Graph(spec), calls


async def test_s5_interrupt_returns_state_of_completed_supersteps() -> None:
    graph, calls = _gate_graph()
    result = await graph.ainvoke({"results": {}}, CFG)

    assert calls == ["stage"]  # 阶段跑完即挂起，门禁节点未产出
    assert result["results"] == {"analysis": {"data": 1}}  # 落检查点靠这份快照
    assert "decision" not in result


async def test_s5_pending_interrupt_is_visible_in_state_view() -> None:
    graph, _ = _gate_graph()
    await graph.ainvoke({"results": {}}, CFG)
    state = graph.get_state(CFG)

    assert state.next == ("gate",)
    assert len(state.tasks) == 1
    assert state.tasks[0].interrupts == ({"stage": "analysis", "message": "等待门禁评审"},)


async def test_s5_second_ainvoke_while_pending_does_not_replay_the_stage() -> None:
    graph, calls = _gate_graph()
    await graph.ainvoke({"results": {}}, CFG)
    await graph.ainvoke({}, CFG)

    assert calls == ["stage"]  # 阶段守卫命中：副作用不重放（DEC-005 同口径）
    assert graph.get_state(CFG).next == ("gate",)


async def test_s6_resume_replays_the_node_from_the_top() -> None:
    graph, calls = _gate_graph()
    await graph.ainvoke({"results": {}}, CFG)
    result = await graph.ainvoke(Resume(value={"decision": "pass"}), CFG)

    assert calls == ["stage", "gate", "finish"]
    assert result["decision"] == {"decision": "pass"}  # interrupt() 交出的是 resume 值
    assert graph.get_state(CFG).next == ()  # 已走到 END


async def test_s6_resume_without_pending_is_rejected() -> None:
    graph, _ = _gate_graph()
    with pytest.raises(SpecError, match="没有待恢复的挂起点"):
        await graph.ainvoke(Resume(value="pass"), CFG)


def test_s6_interrupt_without_resume_context_suspends() -> None:
    with pytest.raises(Interrupt) as caught:
        interrupt({"stage": "analysis"})
    assert caught.value.payload == {"stage": "analysis"}


def test_s6_resume_value_is_handed_out_only_once() -> None:
    with resumed_execution("v"):
        assert interrupt("p1") == "v"
        with pytest.raises(SpecError, match="第二个挂起点"):
            interrupt("p2")


def test_s6_nested_fresh_execution_does_not_inherit_outer_resume() -> None:
    """重放后的后续节点必须重新挂起，而不是复用同一个 resume 值。"""
    with resumed_execution("v"), fresh_execution():
        with pytest.raises(Interrupt):
            interrupt("next-stage")


# ----------------------------------------------------------------------
# S9 —— 自省视图形状
# ----------------------------------------------------------------------
async def test_s9_graph_view_nodes_include_sentinels() -> None:
    graph, _ = _gate_graph()
    nodes = {n for n in graph.get_graph().nodes if n not in (START, "__end__")}
    assert nodes == {"stage", "gate", "finish"}
    assert START in graph.get_graph().nodes


async def test_s9_state_view_for_unknown_thread_is_empty() -> None:
    graph, _ = _gate_graph()
    state = graph.get_state(_cfg("never-run"))
    assert state.next == ()
    assert state.tasks == ()
    assert state.values == {}


# ----------------------------------------------------------------------
# build 期结构校验
# ----------------------------------------------------------------------
def test_unknown_target_rejected() -> None:
    with pytest.raises(SpecError, match="未登记目标"):
        GraphSpec(
            name="dangling",
            channels=("x",),
            nodes={"a": _node_ok},
            entry=Entry(node="a"),
            static={"a": "ghost"},
        )


def test_fan_out_rejected() -> None:
    """同一节点既有静态边又有条件路由 = 两个后继；内核明确不支持，静默串行会改变语义。"""
    with pytest.raises(SpecError, match="fan-out 不支持"):
        GraphSpec(
            name="fanout",
            channels=("x",),
            nodes={"a": _node_ok, "b": _node_ok},
            entry=Entry(node="a"),
            static={"a": "b"},
            conditional={"a": (lambda s: "b", {"b": "b"})},
        )


def test_unreachable_node_rejected() -> None:
    with pytest.raises(SpecError, match="不可达"):
        GraphSpec(
            name="island",
            channels=("x",),
            nodes={"a": _node_ok, "island": _node_ok},
            entry=Entry(node="a"),
            static={"a": END},
        )


def test_node_without_successor_rejected() -> None:
    with pytest.raises(SpecError, match="缺少后继"):
        GraphSpec(
            name="no-successor",
            channels=("x",),
            nodes={"a": _node_ok},
            entry=Entry(node="a"),
            static={},
        )


def test_empty_conditional_map_rejected() -> None:
    with pytest.raises(SpecError, match="没有可选分支"):
        GraphSpec(
            name="empty-route",
            channels=("x",),
            nodes={"a": _node_ok},
            entry=Entry(node="a"),
            conditional={"a": (lambda s: "x", {})},
        )


def test_entry_without_node_or_router_rejected() -> None:
    with pytest.raises(SpecError, match="入口"):
        GraphSpec(name="no-entry", channels=("x",), nodes={"a": _node_ok}, entry=Entry())


def test_two_interrupt_sites_in_one_node_rejected_at_build() -> None:
    async def node(state: dict) -> dict:
        interrupt("a")
        interrupt("b")
        return {}

    with pytest.raises(SpecError, match="2 个挂起点"):
        GraphSpec(
            name="two-interrupts",
            channels=("x",),
            nodes={"node": node},
            entry=Entry(node="node"),
            static={"node": END},
        )


# ----------------------------------------------------------------------
# S10 —— 带回边的循环图（求解内循环的形状）
# ----------------------------------------------------------------------
async def test_s10_loop_graph_terminates_on_budget() -> None:
    visits: list[str] = []

    async def decide(state: dict) -> dict:
        visits.append("decide")
        count = int(state.get("dispatch_count", 0))
        return {"dispatch_count": count, "want_tool": count < 2}

    async def dispatch(state: dict) -> dict:
        visits.append("dispatch")
        return {"dispatch_count": int(state.get("dispatch_count", 0)) + 1}

    async def finalize(state: dict) -> dict:
        visits.append("finalize")
        return {"status": "succeeded"}

    spec = GraphSpec(
        name="loop",
        channels=("dispatch_count", "want_tool", "status"),
        nodes={"decide": decide, "dispatch": dispatch, "finalize": finalize},
        entry=Entry(node="decide"),
        static={"dispatch": "decide", "finalize": END},
        conditional={
            "decide": (
                lambda s: "dispatch" if s.get("want_tool") else "finalize",
                {"dispatch": "dispatch", "finalize": "finalize"},
            ),
        },
    )
    result = await Graph(spec).ainvoke({"dispatch_count": 0}, CFG)

    assert visits == ["decide", "dispatch", "decide", "dispatch", "decide", "finalize"]
    assert result["dispatch_count"] == 2
    assert result["status"] == "succeeded"
