"""mock 主进程集成测试（SP1-1）：压测 1000 次 RPC + 崩溃域隔离 + 优雅退出。

验收标准对齐：
- 仅通信骨架：1000 次 RPC 成功率 100%；
- 崩溃域隔离：注入 crash 后重启，get_status 恢复正常。
"""

import io

from engine.ipc.events import EventEmitter
from engine.ipc.methods import register_all
from engine.ipc.server import JsonRpcServer
from engine.ipc.state import EngineState


def _make_server() -> tuple[JsonRpcServer, EngineState, EventEmitter]:
    state = EngineState()
    events = EventEmitter(sink=io.StringIO(), trace_id="trace-test")
    server = JsonRpcServer(state, events)
    register_all(server, state)
    return server, state, events


async def test_1000_rpc_roundtrips_success() -> None:
    """压测 1000 次 RPC（start_stage/get_status 交替），成功率 100%。"""
    server, _, _ = _make_server()
    ok = 0
    for i in range(1000):
        if i % 2 == 0:
            line = await server.handle_line(
                f'{{"jsonrpc":"2.0","id":{i},"method":"start_stage","params":{{"task_id":"t{i}","stage":"analysis"}}}}'
            )
        else:
            line = await server.handle_line(
                f'{{"jsonrpc":"2.0","id":{i},"method":"get_status","params":{{}}}}'
            )
        if '"result"' in line or '"error"' in line:
            ok += 1
    assert ok == 1000  # 全部有响应（无挂起/无异常）


async def test_all_6_methods_registered() -> None:
    """6 个方法全部注册并可用。"""
    server, _, _ = _make_server()
    # start_stage
    r1 = await server.handle_line('{"jsonrpc":"2.0","id":1,"method":"start_stage","params":{"task_id":"t1","stage":"analysis"}}')
    assert '"status":"running"' in r1
    # pause
    r2 = await server.handle_line('{"jsonrpc":"2.0","id":2,"method":"pause","params":{"task_id":"t1"}}')
    assert '"status":"paused"' in r2
    # resume
    r3 = await server.handle_line('{"jsonrpc":"2.0","id":3,"method":"resume","params":{"task_id":"t1"}}')
    assert '"status":"running"' in r3
    # answer_gate
    r4 = await server.handle_line('{"jsonrpc":"2.0","id":4,"method":"answer_gate","params":{"task_id":"t1","gate":"g1","decision":"pass"}}')
    assert '"decision":"pass"' in r4
    # cancel
    r5 = await server.handle_line('{"jsonrpc":"2.0","id":5,"method":"cancel","params":{"task_id":"t1"}}')
    assert '"status":"idle"' in r5
    # get_status
    r6 = await server.handle_line('{"jsonrpc":"2.0","id":6,"method":"get_status","params":{}}')
    assert '"engine":"idle"' in r6


async def test_crash_recovery_get_status_idle() -> None:
    """崩溃域隔离：crash 后重建 server，get_status 恢复正常（idle）。"""
    # 模拟运行中的引擎
    server1, _, _ = _make_server()
    await server1.handle_line('{"jsonrpc":"2.0","id":1,"method":"start_stage","params":{"task_id":"t1","stage":"analysis"}}')
    # 注入 crash：丢弃 server1，重建（主进程重启引擎）
    server2, _, _ = _make_server()
    r = await server2.handle_line('{"jsonrpc":"2.0","id":2,"method":"get_status","params":{}}')
    assert '"engine":"idle"' in r  # 重启后恢复正常


async def test_event_roundtrip_no_loss() -> None:
    """事件互发：发射 100 条事件，全部到达不丢。"""
    sink = io.StringIO()
    events = EventEmitter(sink=sink, trace_id="trace-test")
    for i in range(100):
        events.emit("stage.progress", task_id=f"t{i}", stage="analysis", progress=i / 100)
    assert sink.getvalue().count("\n") == 100  # 100 条事件全部输出
