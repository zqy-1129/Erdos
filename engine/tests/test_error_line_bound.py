"""写向行宽守护：错误出口也必须在契约单行上限内（W36 的剩余半边）。

缺陷形状（2026-10-12 评审 `ipc/server.py` 查出）：`handle_line` 只对**成功响应**过行宽降级，
五条 `encode_error` 出口全裸。宿主对超限行的处置是**整行丢弃**（`engine-host/rpc.ts:99`），
所以一条长错误消息的对外表现不是"错误难看"，而是"这条请求永远没有回应"——比报错更坏，
而且 `oversized_out` 计数也不会 +1（计数器只看得见成功出口）。

同族的第二条：JSON-RPC 的 `id` 只允许 String/Number/Null，而 `parse_request` 此前不校验，
错误响应会把 id **原样回显**——回显就是放大，一条"读向已放行"的行能让写向产出一行超限。

判据顺序（`_bounded_error`）：先砍消息保住 id（宿主靠 id 对上号）；连 id 都回显不起才退到
id=null（此时找回请求已无可能，改留可读的诊断前缀）。
"""

import ast
import io
import json
from pathlib import Path

import pytest

from engine.ipc.events import EventEmitter
from engine.ipc.limits import MAX_LINE_BYTES, LineGuard, clip_bytes, line_bytes
from engine.ipc.rpc import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    RpcError,
    encode_error,
    parse_request,
)
from engine.ipc.server import JsonRpcServer
from engine.ipc.state import EngineState

REPO_ROOT = Path(__file__).resolve().parents[2]
TRUNCATE_MARK = "…(truncated)"


def _server(guard: LineGuard) -> JsonRpcServer:
    state = EngineState()
    events = EventEmitter(sink=io.StringIO(), guard=guard)
    return JsonRpcServer(state, events)  # 未显式传 guard 时从 events 取，读写两侧共用一份计数


async def _call(server: JsonRpcServer, payload: dict) -> tuple[str, dict]:
    # 紧凑分隔符与真实调用方一致（`rpc.ts` 用 JSON.stringify，不带空格）——
    # 用默认分隔符会让"请求行字节数"与模板估算差几个字节，几条按字节反推的用例会静默测偏。
    line = await server.handle_line(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    return line, json.loads(line)


# ----------------------------------------------------------------------
# 行为：错误出口绝不越限
# ----------------------------------------------------------------------
async def test_long_handler_error_message_is_clipped_not_dropped() -> None:
    """ValueError 文本超限时：行仍在上限内、id 保住、错误码不变、计数 +1、并声明被截。"""
    guard = LineGuard()
    server = _server(guard)

    async def blow_up(params: dict) -> dict:  # noqa: ANN001 - 方法端口签名
        raise ValueError("参数非法：" + "结" * (MAX_LINE_BYTES // 3))  # CJK 每字 3 字节 → 远超上限

    server.register("blow_up", blow_up)
    line, parsed = await _call(
        server, {"jsonrpc": "2.0", "id": "req-77", "method": "blow_up", "params": {}}
    )

    assert line_bytes(line) <= MAX_LINE_BYTES, "错误响应越限=宿主整行丢弃=该请求永无回应"
    assert parsed["id"] == "req-77"  # 截的是消息不是路由：宿主仍能把失败对上号
    assert parsed["error"]["code"] == INVALID_PARAMS
    assert parsed["error"]["message"].endswith(TRUNCATE_MARK)
    assert guard.stats.oversized_out == 1


async def test_internal_error_from_arbitrary_exception_is_clipped() -> None:
    """兜底分支（`内部错误：{exc}`）同样受约束——异常文本的长度不由我们决定。"""
    guard = LineGuard()
    server = _server(guard)

    async def blow_up(params: dict) -> dict:  # noqa: ANN001
        raise RuntimeError("boom " + ("x" * (MAX_LINE_BYTES + 10)))

    server.register("blow_up", blow_up)
    line, parsed = await _call(
        server, {"jsonrpc": "2.0", "id": 5, "method": "blow_up", "params": {}}
    )

    assert line_bytes(line) <= MAX_LINE_BYTES
    assert parsed["error"]["code"] == -32603
    assert guard.stats.oversized_out == 1


async def test_oversized_id_on_an_error_exit_still_answers() -> None:
    """错误出口自己遇到"回显不起的 id"：发一条 id=null 的短错误，而不是发一条会被丢弃的长行。"""
    guard = LineGuard()
    server = _server(guard)
    request_overhead = line_bytes('{"jsonrpc":"2.0","id":"","method":"no_such"}')
    huge_id = "z" * (MAX_LINE_BYTES - request_overhead)  # 请求行恰好被读向放行
    request = {"jsonrpc": "2.0", "id": huge_id, "method": "no_such"}
    assert line_bytes(json.dumps(request, separators=(",", ":"))) <= MAX_LINE_BYTES, (
        "前提：这条测的是写向降级，不是读向拒绝"
    )

    line, parsed = await _call(server, request)

    assert line_bytes(line) <= MAX_LINE_BYTES
    assert parsed["error"]["code"] == METHOD_NOT_FOUND
    assert parsed["id"] is None  # id 与消息装不进同一行时，弃 id 保消息
    assert "no_such" in parsed["error"]["message"]
    assert guard.stats.oversized_out == 1


async def test_oversized_result_cannot_hide_behind_an_oversized_id() -> None:
    """成功响应超限 **且** id 长到回显不起：降级出来的错误信封本身也不能越限。

    过程记录（比结论值钱）：这条用例前两版都**量歪**过——第一版按"错误信封开销"反推 id 长度，
    结果请求行自己就被读向拒了（返回的是 INVALID_REQUEST 而不是预期的降级），我却把它读成
    "信封没越限"；第二版用默认分隔符估算 boilerplate，而请求是 compact 编码，差几个字节又落到
    另一个分支。教训不是"数字写错了"，而是**我以为的触发条件没被量过，就不算条件**——
    现在两个前提都写成断言摆在用例开头，改一字节就会红，而不是静默换分支。
    """
    guard = LineGuard()
    server = _server(guard)
    request_overhead = line_bytes('{"jsonrpc":"2.0","id":"","method":"big","params":{}}')
    empty_envelope_overhead = line_bytes(encode_error("", INTERNAL_ERROR, ""))
    assert request_overhead < empty_envelope_overhead, "前提：错误信封比请求更啰嗦"

    huge_id = "z" * (MAX_LINE_BYTES - request_overhead)  # 请求行恰好仍在读向上限内
    empty_envelope_overhead_with_id = line_bytes(encode_error(huge_id, INTERNAL_ERROR, ""))
    assert empty_envelope_overhead_with_id > MAX_LINE_BYTES, "前提：这个 id 回显不起"

    async def big_result(params: dict) -> dict:  # noqa: ANN001
        return {"blob": "结" * (MAX_LINE_BYTES // 3)}  # 结果远超单行上限

    server.register("big", big_result)
    line, parsed = await _call(
        server, {"jsonrpc": "2.0", "id": huge_id, "method": "big", "params": {}}
    )

    assert line_bytes(line) <= MAX_LINE_BYTES
    assert parsed["error"]["code"] == INTERNAL_ERROR
    assert parsed["id"] is None  # 连空消息的信封都装不下这个 id
    assert "events_replay" in parsed["error"]["message"]  # 出路必须留在诊断里
    assert guard.stats.oversized_out == 1


async def test_non_scalar_id_is_rejected_before_being_echoed() -> None:
    """对象 id 直接 INVALID_REQUEST（JSON-RPC 只允许 String/Number/Null）——不回显=不放大。"""
    guard = LineGuard()
    server = _server(guard)
    big = {"pad": "x" * (MAX_LINE_BYTES - 900)}  # 整行仍在上限内（读向放行），旧行为回显它就越限
    request = {"jsonrpc": "2.0", "id": big, "method": "no_such", "params": {}}
    assert line_bytes(json.dumps(request)) <= MAX_LINE_BYTES

    line, parsed = await _call(server, request)

    assert line_bytes(line) <= MAX_LINE_BYTES
    assert parsed["error"]["code"] == INVALID_REQUEST
    assert parsed["id"] is None  # 拒绝时不采用调用方给的 id（那本来就是回显通道）
    assert guard.stats.oversized_out == 0  # 读侧拦住，无需写侧降级


async def test_short_errors_are_untouched() -> None:
    """正向对照：这条守护不是"把所有错误都改写"——正常短消息逐字不动、不计数。"""
    guard = LineGuard()
    server = _server(guard)

    async def bad_params(params: dict) -> dict:  # noqa: ANN001
        raise ValueError("阶段顺序约束：当前应执行 analysis")

    server.register("start_stage_like", bad_params)
    _, parsed = await _call(
        server, {"jsonrpc": "2.0", "id": 3, "method": "start_stage_like", "params": {}}
    )

    assert parsed["error"]["message"] == "阶段顺序约束：当前应执行 analysis"
    assert parsed["id"] == 3
    assert guard.stats.oversized_out == 0


@pytest.mark.parametrize(
    ("raw_id", "accepted"),
    [("7", True), ('"a"', True), ("null", True), (None, True),
     ('[1]', False), ('{}', False), ("true", False), ('"\\u4e2d"', True)],
)
def test_parse_request_accepts_only_scalar_ids(raw_id: str, accepted: bool) -> None:
    """id 类型校验本身；无 id（通知）仍合法。"""
    line = f'{{"jsonrpc":"2.0","id":{raw_id},"method":"m"}}' if raw_id is not None else (
        '{"jsonrpc":"2.0","method":"m"}'
    )
    if accepted:
        parse_request(line)
    else:
        with pytest.raises(RpcError) as exc:
            parse_request(line)
        assert exc.value.code == INVALID_REQUEST


# ----------------------------------------------------------------------
# 字节截断本身：CJK 场景是上面那条守护成立的前提
# ----------------------------------------------------------------------
@pytest.mark.parametrize("budget", [0, 3, 7, 8, 9, 100, 4097, MAX_LINE_BYTES])
def test_clip_bytes_respects_the_byte_budget_and_keeps_valid_characters(budget: int) -> None:
    text = "题面正文很长" * 3000
    clipped = clip_bytes(text, budget)
    assert line_bytes(clipped) <= budget  # 预算是按 UTF-8 字节花的，不是按字符
    assert text.startswith(clipped.removesuffix(TRUNCATE_MARK))  # 没有半字符、没有编造内容


def test_clip_bytes_does_not_claim_truncation_when_there_is_none() -> None:
    assert clip_bytes("abc", 10) == "abc"  # 没超就不加标记（截断标记是"事实陈述"）
    assert clip_bytes("abc", 3) == "abc"
    assert clip_bytes("abcde", 4) == TRUNCATE_MARK[:4] or clip_bytes("abcde", 4) == ""


# ----------------------------------------------------------------------
# 结构守护：每条出口都走降级，而不是"看起来两侧都调了同一个 guard"
# ----------------------------------------------------------------------
def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        if isinstance(node.func, ast.Name):
            return node.func.id
    return None


def test_every_write_exit_in_handle_line_goes_through_the_bound() -> None:
    source = (REPO_ROOT / "engine" / "ipc" / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(REPO_ROOT / "engine" / "ipc" / "server.py"))
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    exits = 0
    for node in ast.walk(functions["handle_line"]):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        exits += 1
        assert _call_name(node.value) in {"_bounded_error", "_bounded_result"}, (
            f"handle_line 第 {node.lineno} 行绕过了写向行宽降级"
        )
    assert exits, "handle_line 没有返回出口？"

    def _uses(function: ast.AST, target: str) -> bool:
        return any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == target
            for node in ast.walk(function)
        )

    writers = sorted(name for name, fn in functions.items() if _uses(fn, "encode_error"))
    # 允许的直接编码点只有两个：错误出口的兜底层，与它调用的截断层。
    # handle_line / _bounded_result 都不许自己拼信封——那正是"看起来两侧都调了 guard、其实有一条没兜"的形状。
    assert set(writers) <= {"_bounded_error", "_degraded_error"}, (
        f"encode_error 出现在写向兜底之外：{writers}"
    )
    assert "_degraded_error" in writers, "截断层应当在兜底函数里，而不是散在各出口"
