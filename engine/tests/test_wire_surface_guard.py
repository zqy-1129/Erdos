"""方法 result 的**出口字段面**守护（G49① 的延伸，2026-10-13）。

`test_contract_alignment.py` 比的是方法名、事件名、事件必填字段与 transport——**没有一条比方法
result 的字段集**。于是引擎可以在响应里悄悄多带字段：契约的 `result` 没有
`additionalProperties: false`，多带不报错，而客户端一旦开始按它做行为分支，这个字段就
**事实上已冻结、纸面上不存在**（G49 就是这么查出来的：`get_status` 的
`orchestrator`/`protocol`/`last_error` + `answer_gate` 的 `action`，共四处）。

本文件**不替 CT/ARCH 决定**"补进契约还是收掉"（那是 G49②），只做两件引擎侧能自证的事：
① 把这四个字段登记成**带理由的例外白名单**，第五个悄悄出现立刻红；
② 白名单**条目失效也红**（字段被收掉或补进契约后必须删条目，否则它变杂物间——
    与 `test_silent_exception_guard.py` 同一把尺）。
另加一条 `action` 的**两侧值集互证**：引擎实际会发的那几个值 ≡ 客户端类型面声明的那几个，
谁单方面加值都要红（客户端在 `decision=pass` 分支只认其中两个，这个差集是**刻意的**，
所以比的是"全集相等"而不是"每处校验相等"）。
"""

import ast
import asyncio
import io
import json
import re
from pathlib import Path

from engine.ipc.events import EventEmitter
from engine.ipc.methods import register_all
from engine.ipc.server import JsonRpcServer
from engine.ipc.state import EngineState

REPO = Path(__file__).resolve().parents[2]
METHODS = REPO / "engine" / "ipc" / "methods.py"
GRAPH = REPO / "engine" / "orchestrator" / "graph.py"
SCHEMA = REPO / "contracts" / "engine-rpc.schema.json"
CLIENT_TYPES = REPO / "client" / "shared" / "ipc.ts"
CLIENT_CONTROLLER = REPO / "client" / "main" / "task-controller.ts"

# 契约未声明、但两端已在使用的出口字段（G49② 的登记处）。
# 每条都要写清"谁在按它做事"——只有引用没有后果的字段该被收掉，不该进这张表。
REGISTERED_EXTRA: dict[str, dict[str, str]] = {
    "get_status": {
        "orchestrator": "编排状态快照；客户端 `task-controller.ts` 据 `current_stage` 拒越序推进、"
        "`engine-slice.ts`/`workspace.tsx` 读它收敛视图",
        "protocol": "W36/G25 的行长越限计数（oversized_in/oversized_out/truncated_events），"
        "越限不得静默消失，宿主页要能看见",
        "last_error": "阶段失败归因（`TypeName: detail`），回归驱动按类型重映射 FailureModule",
    },
    "answer_gate": {
        "action": "编排器给下一步动作（next_stage|retry_stage|complete）；"
        "客户端 `task-controller.ts` 在 pass 分支按它收敛，非白名单值直接抛错",
    },
}

_CONTRACT = json.loads(SCHEMA.read_text(encoding="utf-8"))


def _contract_result_keys(method: str) -> set[str]:
    return set(_CONTRACT["methods"][method]["result"].get("properties", {}))


def _handler_return_keys(source: str) -> dict[str, set[str]]:
    """每个 `async def <method>(params)` 处理器**可能**返回的字典顶层键（含各分支）。

    取"所有分支的并集"而不是"某一条路径"：`orchestrator`/`last_error` 这类是条件字段，
    只测一条路径会把它们漏成"没发"，从而在登记表里留下永远不该存在的条目。
    """
    out: dict[str, set[str]] = {}

    def keys_of(node: ast.Dict) -> set[str]:
        return {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}

    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        params = [a.arg for a in fn.args.args]
        if params != ["params"]:
            continue
        found: set[str] = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
                found |= keys_of(node.value)
        if found:
            out[fn.name] = found
    return out


async def _drive(**kwargs) -> set[str]:
    """真走一遍 NDJSON 出口，返回 `get_status.result` 的键集（条件字段要靠行为观测）。"""
    from engine.orchestrator.graph import StageOrchestrator

    state = EngineState()
    if kwargs.get("with_orchestrator"):
        state.orchestrator = StageOrchestrator("t-obs", checkpoint=None)
    server = JsonRpcServer(state, EventEmitter(sink=io.StringIO()))

    async def bad_runner(task_id: str, stage: str) -> dict:
        raise RuntimeError("注入的阶段失败")

    runtime = register_all(server, state, runner=bad_runner if kwargs.get("fail_stage") else None)
    if kwargs.get("fail_stage"):
        await server.handle_line(
            json.dumps({"jsonrpc": "2.0", "id": "i", "method": "start_stage",
                        "params": {"task_id": "t-obs", "stage": "analysis"}})
        )
        # `last_error` 由后台阶段任务写，不等收尾就取快照会把条件字段取丢——
        # 这正是"用静态判据冒充行为观测"会犯错的地方，所以这里显式 drain。
        await runtime.drain()
    resp = json.loads(await server.handle_line(
        json.dumps({"jsonrpc": "2.0", "id": "s", "method": "get_status", "params": {}})
    ))
    return set(resp.get("result", {}))


def _observed_get_status_keys() -> set[str]:
    """三个场景的并集：空载 / 挂了编排器 / 阶段失败——正好覆盖三个条件字段的出现条件。"""
    union: set[str] = set()
    for kwargs in ({"with_orchestrator": False, "fail_stage": False},
                   {"with_orchestrator": True, "fail_stage": False},
                   {"with_orchestrator": False, "fail_stage": True}):
        union |= asyncio.run(_drive(**kwargs))
    return union


def _emitted_keys() -> dict[str, set[str]]:
    """每个方法的出口键集：AST 可见的静态取，`get_status` 靠行为观测（返回的是变量不是字面量）。"""
    emitted = _handler_return_keys(METHODS.read_text(encoding="utf-8"))
    emitted["get_status"] = emitted.get("get_status", set()) | _observed_get_status_keys()
    return emitted


def test_registered_extras_cover_every_uncontracted_result_field() -> None:
    """出口字段只能来自"契约声明 + 已登记例外"，第五个悄悄出现就红。"""
    emitted = _emitted_keys()
    assert emitted, "没扫到任何 RPC 处理器的返回字典——扫描根或形状判据走错了"

    problems: list[str] = []
    for method, keys in sorted(emitted.items()):
        allowed = _contract_result_keys(method) | set(REGISTERED_EXTRA.get(method, {}))
        problems += [
            f"{method}.{k}：契约未声明且未登记例外（要么补契约=G49②，要么收掉，别悄悄多发）"
            for k in sorted(keys - allowed)
        ]
    assert not problems, "\n".join(problems)


def test_no_registered_extra_is_stale_or_contract_pinned() -> None:
    """登记表双向都要成立：条目必须**仍在发**（失效即红），也必须**仍未进契约**（进了就该删条目）。

    这一条把"等 CT/ARCH"从无限期变成有信号的：一旦有人把 `orchestrator` 补进契约，
    本用例就红着提醒"把登记一起撤掉"，G49② 不会挂在口头。
    """
    emitted = _emitted_keys()
    for method, extras in REGISTERED_EXTRA.items():
        for field, reason in extras.items():
            assert field in emitted.get(method, set()), f"{method}.{field} 已不再发出，请从登记表删条目"
            assert field not in _contract_result_keys(method), (
                f"{method}.{field} 已进契约，请从登记表删条目（理由原文：{reason}）"
            )
            assert len(reason) >= 20, f"{method}.{field} 的登记理由过短，空话不算放行"


def test_the_field_checker_actually_bites() -> None:
    """正向对照：守护必须能被测坏，否则它只是在给自己发 ✅。"""
    snippet = 'async def get_status(params):\n    return {"engine": 1, "task": 2, "brand_new": 3}\n'
    assert _handler_return_keys(snippet) == {"get_status": {"engine", "task", "brand_new"}}

    # 非处理器签名（内部协程）不计入，避免把私有函数当出口而误报
    assert _handler_return_keys('async def _helper(x):\n    return {"a": 1}\n') == {}


def test_get_status_wire_result_has_no_unregistered_field() -> None:
    """行为侧一条：真实走一遍 NDJSON 出口，键集与静态判据对得上（静态判据不是纸上谈兵）。"""
    keys = asyncio.run(_drive())
    allowed = _contract_result_keys("get_status") | set(REGISTERED_EXTRA["get_status"])
    assert keys <= allowed, f"出口出现未登记字段：{keys - allowed}"
    assert {"engine", "task", "protocol"} <= keys, keys
    # 条件字段确实有出现条件（不是登记表里的空头条目）
    assert "orchestrator" in _observed_get_status_keys()
    assert "last_error" in _observed_get_status_keys()


def test_action_value_set_is_identical_on_both_sides() -> None:
    """`action` 的取值集两侧必须完全相等（G49②③ 之前，先把"各写一遍"接上机器比对）。

    引擎侧从 `graph.py` 的 `return {"action": "…"}` 取实际会发的值；客户端侧从
    `client/shared/ipc.ts` 的联合类型取声明的值。**校验点不等于值集**：
    客户端在 `decision=pass` 的分支里只接受其中两个（驳回时不校验），那是刻意的，
    所以这里比"全集相等"，并另断一句 pass 分支的白名单必须是引擎值集的子集。
    """
    engine = set(re.findall(r'"action":\s*"([a-z_]+)"', GRAPH.read_text(encoding="utf-8")))
    types = CLIENT_TYPES.read_text(encoding="utf-8")
    declared = re.search(r"action\?:\s*([^;]+);", types)
    assert declared, "客户端类型面里 `action` 的声明形状变了——本用例与 G49 的登记理由都要一起改"
    client = {v.strip().strip('"') for v in declared.group(1).split("|")}

    assert engine == {"next_stage", "retry_stage", "complete"}, engine
    assert client == engine, f"两侧值集不等：引擎 {sorted(engine)} / 客户端 {sorted(client)}"

    controller = CLIENT_CONTROLLER.read_text(encoding="utf-8")
    assert '["next_stage", "complete"]' in controller, (
        "pass 分支的白名单形状变了——那是刻意只认两个（驳回时不校验），改这里要连用例与登记一起改"
    )
