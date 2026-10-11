"""NDJSON 单行上限守护（G25/W36）：契约 `transport.limits.max_line_bytes` 的引擎侧落地。

缺陷形状（两个方向都是"看起来有人管，实际没人管"）：
- 读向：引擎 `ipc/server.py` 裸 `readline()`，对超长行零上限、零拒绝；
- 写向：宿主读到 >1MiB 的行会**整行丢弃**并记协议错误（`client/main/engine-host/rpc.ts:99`），
  所以引擎发出超长的后果不是报错而是"事件/响应静默消失"——比崩更难查。

这里锁五件事：上限三处同源（引擎常量 ⇔ 契约 ⇔ 客户端字面量）、超限请求被拒但**不断链**、
事件永不发超限行且降级只砍内容不砍路由、极端装不下时宁可不发也要计数、计数经
`get_status.protocol` 暴露给宿主而不是烂在引擎内存里。

用例把 `limits.MAX_LINE_BYTES` 临时压到 4KiB 再测降级算法：同一份代码路径，但不用为每个
断言序列化几 MB 字符串（本批第一版就是这么把整条套件拖到分钟级的）。真实上限本身另有专测。
"""

import io
import json
import re
from pathlib import Path

import pytest

from engine.ipc import limits as limits_module
from engine.ipc.events import REQUIRED_FIELDS, EventEmitter
from engine.ipc.limits import LineGuard, bound_event_payload, line_bytes
from engine.ipc.methods import register_all
from engine.ipc.rpc import INTERNAL_ERROR, INVALID_REQUEST
from engine.ipc.server import JsonRpcServer
from engine.ipc.state import EngineState

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "contracts" / "engine-rpc.schema.json"
CLIENT_RPC = REPO_ROOT / "client" / "main" / "engine-host" / "rpc.ts"
TEST_LIMIT = 4096  # 行为用例用的压缩上限（判定逻辑与真实上限同一条路径）


@pytest.fixture
def small_limit(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(limits_module, "MAX_LINE_BYTES", TEST_LIMIT)
    return TEST_LIMIT


def _lines(buf: io.StringIO) -> list[str]:
    return [line for line in buf.getvalue().splitlines() if line]


def _emitter() -> tuple[EventEmitter, io.StringIO, LineGuard]:
    guard = LineGuard()
    buf = io.StringIO()
    return EventEmitter(sink=buf, trace_id="trace-guard", guard=guard), buf, guard


def _gate_failed(events: EventEmitter, reason_len: int, task_id: str = "t9") -> str:
    return events.emit(
        "gate.failed", task_id=task_id, gate="gate_solving", reason="违" * reason_len
    )


# ----------------------------------------------------------------------
# 上限三处同源（G32 同族：两侧各写一遍、改一处漏一处）
# ----------------------------------------------------------------------
def test_limit_is_single_sourced_across_engine_contract_and_client() -> None:
    contract = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert contract["transport"]["limits"]["max_line_bytes"] == limits_module.MAX_LINE_BYTES

    literal = re.search(
        r"MAX_LINE_BYTES\s*=\s*(\d+)\s*\*\s*(\d+)", CLIENT_RPC.read_text(encoding="utf-8")
    )
    assert literal, "客户端上限写法变了，本守护要同步改写而不是删掉"
    assert int(literal.group(1)) * int(literal.group(2)) == limits_module.MAX_LINE_BYTES, (
        "引擎与客户端各持一个字面量、契约才是单源：任一侧改动必须同时改两处"
    )


def test_boundaries_under_the_limit_are_untouched(small_limit: int) -> None:
    """正向对照：没超限就不该截断、不该计数——否则"守护在跑"是假象。"""
    events, buf, guard = _emitter()
    line = events.emit("stage.progress", task_id="t1", stage="solving", progress=0.5)
    assert "truncated" not in json.loads(line)
    assert guard.stats.as_dict() == {"oversized_in": 0, "oversized_out": 0, "truncated_events": 0}
    assert len(_lines(buf)) == 1


# ----------------------------------------------------------------------
# 读向：超限请求行拒绝但不断链（EC-S6）
# ----------------------------------------------------------------------
async def test_oversized_request_is_rejected_and_counted(small_limit: int) -> None:
    events, _, _ = _emitter()
    server = JsonRpcServer(EngineState(), events)

    payload = json.loads(await server.handle_line("y" * (small_limit + 10)))
    assert payload["error"]["code"] == INVALID_REQUEST
    assert "上限" in payload["error"]["message"]
    assert server.guard.stats.oversized_in == 1


async def test_real_limit_rejects_oversized_request() -> None:
    """真实 1MiB 上限本身也要跑一次（压缩上限测的是算法，不是数值）。"""
    events, _, _ = _emitter()
    server = JsonRpcServer(EngineState(), events)
    payload = json.loads(await server.handle_line("y" * (limits_module.MAX_LINE_BYTES + 16)))
    assert payload["error"]["code"] == INVALID_REQUEST


async def test_link_survives_oversized_request(small_limit: int) -> None:
    """EC-S6 的要害不是"报错"而是"会话继续"：超限行之后的正常请求必须照旧被服务。"""
    state = EngineState()
    events, _, guard = _emitter()
    server = JsonRpcServer(state, events)
    register_all(server, state)

    await server.handle_line("x" * (small_limit + 1))
    answer = json.loads(
        await server.handle_line(
            json.dumps({"jsonrpc": "2.0", "id": "r2", "method": "get_status", "params": {}})
        )
    )
    assert "result" in answer, f"超限行之后链路断了：{answer}"
    assert answer["result"]["protocol"]["oversized_in"] == 1, "计数须经协议面暴露，不是烂在内存里"
    assert guard.stats.oversized_in == 1


# ----------------------------------------------------------------------
# 写向：事件永不发超限行
# ----------------------------------------------------------------------
def test_event_line_is_bounded_and_keeps_routing_fields(small_limit: int) -> None:
    events, buf, guard = _emitter()
    line = _gate_failed(events, reason_len=small_limit * 4)

    assert line and line == _lines(buf)[-1]
    assert line_bytes(line) <= small_limit, "发出去的行必须自己就满足上限（宿主丢整行=静默消失）"
    payload = json.loads(line)
    assert REQUIRED_FIELDS["gate.failed"] <= set(payload), "降级只许砍内容，不许砍路由与必填字段"
    assert payload["truncated"] is True
    assert payload["task_id"] == "t9" and payload["gate"] == "gate_solving"
    assert payload["reason"].endswith("(truncated)")
    assert guard.stats.truncated_events == 1


def test_untouchable_routing_fields_are_never_truncated(small_limit: int) -> None:
    """`task_id` 截断会把事件投给错的任务、`seq` 截断会让宿主无法去重：这两类绝不砍。"""
    events, _, _ = _emitter()
    task_id = "t" * 600  # 小于上限但远超内容字段预算
    line = _gate_failed(events, reason_len=small_limit * 4, task_id=task_id)
    assert line, "内容字段可砍到装得下，不该走到放弃发送"
    payload = json.loads(line)
    assert payload["task_id"] == task_id and payload["gate"] == "gate_solving"


def test_unsendable_event_is_dropped_not_oversized(small_limit: int) -> None:
    """路由字段本身撑爆上限时：宁可不发并计数，也不发一条宿主必丢的坏行。"""
    events, buf, guard = _emitter()
    line = _gate_failed(events, reason_len=small_limit * 2, task_id="t" * (small_limit * 2))
    assert line == ""
    assert _lines(buf) == []
    assert guard.stats.oversized_out == 1


def test_dropped_event_does_not_enter_replay_buffer(small_limit: int) -> None:
    """被放弃的事件不得进回放缓冲——否则 events_replay 会补发一条从未成立的事件。"""
    events, _, _ = _emitter()
    events.emit(
        "artifact.ready", task_id="t" * (small_limit * 2), artifact="paper", sha256="0" * 64
    )
    assert events.replay(after_seq=0) == []


def test_truncation_terminates_when_field_is_near_the_mark(small_limit: int) -> None:
    """回归本批踩过的死循环：字段已接近标记长度时"砍一半+加标记"不再变短，必须清空而不是原地打转。"""
    payload = {"gate": "g", "reason": "x" * (limits_module._MIN_FIELD_BUDGET + 1)}
    out, degraded = bound_event_payload(  # 极小上限逼出边界
        payload | {"trace_id": "t" * small_limit, "event": "gate.failed"}, set()
    )
    assert degraded is True
    assert line_bytes(json.dumps(out, ensure_ascii=False, separators=(",", ":"))) <= small_limit


def test_bound_payload_prefers_longest_content_field(small_limit: int) -> None:
    """截断顺序判据：先砍最长的内容字段，短字段与路由字段原样保留。"""
    payload = {
        "trace_id": "trace-1", "event": "gate.failed", "timestamp": "2026-10-11T00:00:00+08:00",
        "seq": 1, "task_id": "t1", "gate": "gate_writing",
        "reason": "长" * (small_limit * 2), "detail_short": "keepme",
    }
    out, degraded = bound_event_payload(payload, REQUIRED_FIELDS["gate.failed"])
    assert degraded is True
    assert out["detail_short"] == "keepme" and out["task_id"] == "t1"
    assert line_bytes(json.dumps(out, ensure_ascii=False, separators=(",", ":"))) <= small_limit


# ----------------------------------------------------------------------
# 写向：响应降级
# ----------------------------------------------------------------------
async def test_oversized_response_degrades_to_error_envelope(small_limit: int) -> None:
    events, _, _ = _emitter()
    server = JsonRpcServer(EngineState(), events)

    async def _huge(params: dict) -> dict:
        return {"blob": "z" * (small_limit + 1000)}

    server.register("events_replay", _huge)  # 真实触发点就是 events_replay 一次取太多事件
    response = await server.handle_line(
        json.dumps({"jsonrpc": "2.0", "id": "r9", "method": "events_replay", "params": {}})
    )

    assert line_bytes(response) <= small_limit
    payload = json.loads(response)
    assert payload["id"] == "r9" and payload["error"]["code"] == INTERNAL_ERROR
    assert "limit" in payload["error"]["message"], "降级响应要给出路（分页取），不只是拒绝"
    assert server.guard.stats.oversized_out == 1


async def test_get_status_shape_stays_contract_compatible_after_guard(small_limit: int) -> None:
    """装守护不得改变方法集与响应形状（双侧契约守护的前提），只新增 protocol 计数。"""
    state = EngineState()
    events, _, guard = _emitter()
    server = JsonRpcServer(state, events)
    runtime = register_all(server, state)

    result = json.loads(
        await server.handle_line(
            json.dumps({"jsonrpc": "2.0", "id": "a", "method": "get_status", "params": {}})
        )
    )["result"]
    assert {"engine", "task", "protocol"} <= set(result)
    assert guard.stats.as_dict() == {"oversized_in": 0, "oversized_out": 0, "truncated_events": 0}
    await runtime.drain()
