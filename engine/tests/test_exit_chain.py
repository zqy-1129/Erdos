"""退出链的取消档（KeyboardInterrupt / SIGBREAK）此前零覆盖——它只 `t.cancel()` 就丢了 task_id。

进程随即退出，检查点只能说明"做到哪"，说明不了"为什么停"。所以本文件钉三件事：

1. 取消时 stderr 点名 **哪个 task_id、哪个阶段**未完成（宿主与事后复盘唯一能读到的记录）；
2. **状态只由产生它的那一侧写一次**：task 状态是 `_run_stage` 的 CancelledError 分支写的，
   drain 不得再写一遍（两处写同一事实就是下一个漂移点），drain 只补引擎级 status 归位；
3. 非取消档（stdin EOF）语义不变：等阶段跑完、不写取消记录——把 EOF 也当取消就是谎报中断。

进程级信号演练（真发 CTRL_BREAK_EVENT 给引擎子进程）仍未覆盖，登记在报告 §未覆盖。
"""

import asyncio
import io
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from engine.adapters.key_store import KeyStore
from engine.ipc.events import EventEmitter
from engine.ipc.methods import EngineRuntime, register_all
from engine.ipc.server import JsonRpcServer
from engine.ipc.state import EngineState

RUNTIME_INFO = {
    "protocol_version": 2,
    "engine_version": "0.1.0",
    "tool_mode": "tool_loop",
    "isolation_mode": "docker",
}


async def _call(server: JsonRpcServer, rid: str, method: str, params: dict) -> dict:
    line = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    return json.loads(await server.handle_line(line))


class _Harness:
    """一套装配好的运行时：可控在跑任务 + 事件出口 + 状态。"""

    def __init__(self, runner) -> None:  # noqa: ANN001 - StageRunner 签名由各用例给
        self.state = EngineState()
        self.sink = io.StringIO()
        self.events = EventEmitter(sink=self.sink)
        self.server = JsonRpcServer(self.state, self.events)
        self.runtime = register_all(
            self.server, self.state, runner=runner, events=self.events,
            key_store=KeyStore(), runtime_info=RUNTIME_INFO,
        )


@pytest.mark.asyncio
async def test_cancel_drain_names_the_interrupted_task(capsys) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_runner(task_id: str, stage: str) -> dict:
        started.set()
        await release.wait()  # 永不等满：本用例就是要在它挂着的时候取消
        return {"stage": stage}

    h = _Harness(slow_runner)
    await _call(h.server, "s1", "start_stage", {"task_id": "tk-cancel", "stage": "analysis"})
    await asyncio.wait_for(started.wait(), timeout=5)

    await h.runtime.drain(cancel=True)

    err = capsys.readouterr().err
    assert "tk-cancel" in err, f"取消记录没点名任务：{err!r}"
    assert "analysis" in err, f"取消记录没说阶段：{err!r}"
    # task 状态由 _run_stage 的 CancelledError 分支写（唯一写者），drain 不重复写
    assert h.state.task is not None and h.state.task.status == "cancelled"
    assert h.state.status == "idle", "没东西在跑却报 running 会让宿主误判"


@pytest.mark.asyncio
async def test_eof_drain_does_not_report_a_cancellation(capsys) -> None:
    """非取消档（EOF）等阶段自然收尾：不得出现取消记录，也不得改引擎状态语义。"""

    async def good_runner(task_id: str, stage: str) -> dict:
        return {"stage": stage, "insights": ["要点"], "usage": {"prompt_tokens": 3}}

    h = _Harness(good_runner)
    await _call(h.server, "s2", "start_stage", {"task_id": "tk-eof", "stage": "analysis"})

    await h.runtime.drain()

    err = capsys.readouterr().err
    assert "退出链取消" not in err, f"EOF 路径被写成了取消：{err!r}"
    assert h.state.task is not None and h.state.task.status == "done"


@pytest.mark.asyncio
async def test_cancel_note_survives_a_state_without_the_matching_task(capsys) -> None:
    """state.task 与被取消的 id 不同（单任务引擎的真实分支）→ 阶段记 "?"，不崩、不冒名改写。"""
    runtime = EngineRuntime()  # 未注入 state：register_all 之外的构造形态（嵌入式/测试）
    live = asyncio.create_task(asyncio.sleep(30))
    runtime.running["tk-orphan"] = live

    await runtime.drain(cancel=True)

    assert live.cancelled(), "取消档必须真的把在跑任务停掉"
    assert "退出链取消" not in capsys.readouterr().err, "没有 state 时不该凭空编造点名记录"


@pytest.mark.asyncio
async def test_note_marks_unknown_stage_instead_of_a_wrong_one(capsys) -> None:
    """正向对照：id 对不上时宁可写 "?"，也不把别的任务的阶段当成被中断的那个。"""
    h = _Harness(
        lambda task_id, stage: asyncio.sleep(30)  # noqa: ARG005 - 只要任务挂在跑即可
    )
    h.state.start_stage("tk-other", "modeling")
    live = asyncio.create_task(asyncio.sleep(30))
    h.runtime.running["tk-mismatch"] = live

    await h.runtime.drain(cancel=True)

    err = capsys.readouterr().err
    assert "tk-mismatch" in err and "阶段 ?" in err, err
    assert h.state.task is not None and h.state.task.task_id == "tk-other"
    assert h.state.task.status == "running", "不得把不相干任务改成 cancelled（那是编造归因）"


def test_signal_stop_routes_into_the_cancel_drain() -> None:
    """装配守护（与 `test_entry_wiring` 同形）：信号停机必须走进取消档。

    SIGTERM/SIGBREAK 才是用户"停引擎"的真实路径；它一旦退回 EOF 档的 `drain()`，在跑任务就被
    `os._exit(0)` 直接打死——点名记录与 CancelledError 分支都不执行，而单测仍是绿的（单测直接
    调 `drain(cancel=True)`，绕过了 main 的接线）。所以这里守**接线本身**。
    """
    import ast
    import inspect

    import engine.__main__ as entry

    source = inspect.getsource(entry.main)
    drain_calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "drain"
    ]
    assert len(drain_calls) == 3, f"退出链应有三处 drain（handler 排程 / serve 之后 / Ctrl+C）：{len(drain_calls)}"
    assert all(c.keywords and any(k.arg == "cancel" for k in c.keywords) for c in drain_calls), (
        "存在不带 cancel= 的 drain()：EOF 档与取消档会退化成同一条路径"
    )
    # 关键一条：信号 handler 自己就得排取消，不能只等 serve() 返回（宿主保持 stdin 打开时
    # 它返回不了，300ms 后 os._exit 会抢掉整条取消链）。
    handler = source[source.index("def _graceful"):source.index("signal.signal(signal.SIGTERM")]
    assert "runtime.drain(cancel=True)" in handler, "_graceful 不再自己排取消：点名记录会永远写不出来"
    assert "call_soon_threadsafe" in handler, "handler 未经线程安全入口排程（循环正卡在 selector 上）"
    assert 'stop_requested["cancel"] = True' in source, "_graceful 不再置取消标记"


# ----------------------------------------------------------------------
# 进程级演练：真发停机信号给引擎子进程（上一批登记的 ⚠️ 未覆盖，本批补上）
# ----------------------------------------------------------------------
def _send(proc, msg: dict) -> None:  # noqa: ANN001 - subprocess.Popen
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def _await(q, pred, what: str, timeout: float = 45.0) -> dict:  # noqa: ANN001
    """读到第一个满足 pred 的 NDJSON 行（事件与响应混在一条流上，按内容挑）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = q.get(timeout=1.0)
        if line is None:
            raise AssertionError(f"引擎 stdout 提前关闭，未等到：{what}")
        msg = json.loads(line)
        if pred(msg):
            return msg
    raise AssertionError(f"超时未等到：{what}")


def _drive_to_solving(proc, q, task_id: str) -> None:  # noqa: ANN001
    """把真实引擎进程推进到 solving 在跑：编排器有阶段顺序约束，不能直接跳 solving。

    顺带这条就是跨进程的四阶段驱动用例：task_create → analysis → 门禁 pass →
    modeling → 门禁 pass → start_stage(solving)。
    """
    _send(proc, {"jsonrpc": "2.0", "id": "c1", "method": "task_create", "params": {
        "task_id": task_id, "title": "停机演练", "problem_text": "给定数据点求 y=ax+b"}})
    _await(q, lambda m: m.get("id") == "c1" and "result" in m, "task_create 响应")

    n = 0
    for stage in ("analysis", "modeling"):
        n += 1
        _send(proc, {"jsonrpc": "2.0", "id": f"s{n}", "method": "start_stage", "params": {
            "task_id": task_id, "stage": stage}})
        _await(q, lambda m, s=stage: m.get("event") == "stage.progress"
               and m.get("stage") == s and float(m.get("progress", 0)) >= 1.0, f"{stage} 执行完成")
        n += 1
        _send(proc, {"jsonrpc": "2.0", "id": f"g{n}", "method": "answer_gate", "params": {
            "task_id": task_id, "gate": f"gate_{stage}", "decision": "pass", "feedback": ""}})
        _await(q, lambda m, i=f"g{n}": m.get("id") == i and ("result" in m or "error" in m),
               f"{stage} 门禁应答")

    n += 1
    _send(proc, {"jsonrpc": "2.0", "id": f"s{n}", "method": "start_stage", "params": {
        "task_id": task_id, "stage": "solving"}})
    # 0.05 = 任务已建且尚未收尾：solving 走真实 subprocess 沙箱，这几百毫秒就是取消窗口
    _await(q, lambda m: m.get("event") == "stage.progress" and m.get("stage") == "solving"
           and float(m.get("progress", 0)) < 1.0, "solving 在跑")


def _engine_lines(task_id: str) -> list[str]:
    return [
        "",  # 空首行 = 无 Key 模式（FakeLLM + 真实 subprocess 沙箱）
    ]


def _stop(proc) -> None:  # noqa: ANN001 - subprocess.Popen
    """发本平台"停引擎"信号：与引擎注册的处理器一一对应。

    Windows 的 `Popen.send_signal()` 不认 SIGBREAK（值 21），必须直接
    `os.kill(pid, CTRL_BREAK_EVENT)`；POSIX 侧走 SIGTERM。两个分支命中同一个 `_graceful`。
    """
    if sys.platform == "win32":
        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
    else:
        proc.send_signal(signal.SIGTERM)


def test_signal_stop_names_the_interrupted_stage_in_a_real_process(tmp_path: Path) -> None:
    """SIGTERM/SIGBREAK 的真实形态：点名记录必须在 300ms 硬上限内落到 stderr。

    这条测的正是 `_note_cancelled` 的**时序**：点名若排在 `await gather(...)` 之后，
    取消一个正在跑真实沙箱的阶段要几百毫秒，`os._exit(0)` 会先发生——单测全绿而真机上
    永远查不到这条记录。进程级用例是这类"只在真机成立"的时序唯一能抓住的地方。
    """
    from test_entry_wiring import _close_pipes, _reader, _spawn  # 复用踩出来的 env 剥离与 spawn

    proc = _spawn(_engine_lines("t-sig"), home=tmp_path / "home", close_stdin=False)
    q = _reader(proc)
    _drive_to_solving(proc, q, "t-sig")

    _stop(proc)
    stderr_text = proc.stderr.read() if proc.stderr else ""
    rc = proc.wait(timeout=30)
    _close_pipes(proc)

    assert rc == 0, f"信号停机应以码 0 退出，实际 {rc}；stderr={stderr_text[:400]!r}"
    assert "退出链取消在跑任务" in stderr_text, f"真实进程里没有点名记录：{stderr_text[:400]!r}"
    assert "t-sig" in stderr_text and "阶段 solving" in stderr_text, stderr_text[:400]


def test_no_signal_means_no_cancellation_record(tmp_path: Path) -> None:
    """正向对照：不发信号（EOF 档）时不得出现点名记录，否则上一条是恒真的。"""
    from test_entry_wiring import _close_pipes, _reader, _spawn

    proc = _spawn(_engine_lines("t-plain"), home=tmp_path / "home", close_stdin=False)
    q = _reader(proc)
    _drive_to_solving(proc, q, "t-plain")

    assert proc.stdin is not None
    proc.stdin.close()  # EOF 档：等当前阶段跑完再退，不该被记成"取消"
    stderr_text = proc.stderr.read() if proc.stderr else ""
    rc = proc.wait(timeout=60)
    _close_pipes(proc)

    assert rc == 0, f"EOF 收尾应以码 0 退出，实际 {rc}；stderr={stderr_text[:300]!r}"
    assert "退出链取消" not in stderr_text, f"EOF 档被写成了取消：{stderr_text[:400]!r}"
