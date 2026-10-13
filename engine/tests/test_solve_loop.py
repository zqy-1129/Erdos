"""EN-LOOP（W11）求解内循环测试：幂等矩阵 + 循环三路径 + 收敛硬检查 + pipeline 接线。

覆盖（《任务执行手册》W11 验收）：
- OperationLog：find/record_done/first-wins/running 半写按未完成处理（崩溃点矩阵）；
- 坏代码自动修复（FakeLLM 脚本序列）：失败回注 → 修正 → 收敛，repair_count 递增；
- 修复预算耗尽 → awaiting_input（转人工，不静默失败、不无界循环）；
- 结果校验：非 JSON/NaN-Infinity 拒绝（parse_constant）→ 回注修复后收敛；
- 恢复不重放（DEC-005）：预登记 exec_seq **且请求内容一致** → 循环复用引用，沙箱执行计数为 0；
  同键换代码/失败不入账/旧库无摘要行按未知处理，都属 G53 的内容维度；
- pipeline 接线：tool_loop 分支产出结构化 results；stage_level 降级路径保持不变。
FakeLLM/脚本化 LLM 为确定性假模型（测试专用），不产生真实模型调用。
"""

import ast
import hashlib
import inspect
import json
import sqlite3
import textwrap
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from engine.adapters.openai_compat import ChatMessage, ChatResult, ToolCall, Usage
from engine.orchestrator.operations import OperationLog, OperationRecord, request_digest
from engine.orchestrator.pipeline import FakeLLM, StagePipeline
from engine.orchestrator.solve_loop import SolveLoop, artifact_sha256
from engine.sandbox.base import ExecutionResult
from engine.sandbox.subprocess_sandbox import SubprocessSandbox
from engine.tools import build_default_registry


class ScriptedLLM:
    """脚本化假 LLM：按队列返回 ChatResult（含 tool_calls），记录收到的 messages/tools。"""

    def __init__(self, results: list[ChatResult], emit_deltas: list[str] | None = None) -> None:
        self._queue = list(results)
        self._emit_deltas = emit_deltas or []
        self.calls: list[dict] = []

    async def __call__(
        self, messages: list[ChatMessage], tools: list[dict],
        on_delta: Callable[[str], None] | None = None,
    ) -> ChatResult:
        self.calls.append({
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "tools": tools,
        })
        if on_delta is not None:
            for d in self._emit_deltas:
                on_delta(d)  # 模拟适配器 token 增量回调
        return self._queue.pop(0)


class CountingSandbox:
    """计数沙箱替身：记录 execute 次数与代码，返回可编程结果。"""

    isolation_mode = "counter"  # 自报模式，供"执行体说了算"的断言用

    def __init__(self, results: list[ExecutionResult] | None = None) -> None:
        self.execute_count = 0
        self.codes: list[str] = []
        self._results = list(results or [])

    async def execute(self, code: str, files: dict, work_dir: Path) -> ExecutionResult:
        self.execute_count += 1
        self.codes.append(code)
        if self._results:
            return self._results.pop(0)
        return ExecutionResult(exit_code=0, stdout="ok", stderr="", artifacts=[])


def _usage() -> Usage:
    return Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5)


def _final(results: list[dict]) -> ChatResult:
    return ChatResult(
        content=json.dumps({"results": results}, ensure_ascii=False), usage=_usage(),
        finish_reason="stop",
    )


def _tool_call(call_id: str, name: str, code: str) -> ChatResult:
    return ChatResult(
        content="", usage=_usage(), finish_reason="tool_calls",
        tool_calls=[ToolCall(id=call_id, name=name, arguments_json=json.dumps({"code": code}))],
    )


def _digest(code: str, name: str = "execute_code") -> str:
    """与 `_tool_call` 同构的请求摘要（G53：预登记测试现场时必须给得出内容）。"""
    return request_digest(name, json.dumps({"code": code}))


def _loop(tmp_path: Path, llm: ScriptedLLM, sandbox: CountingSandbox | None = None, attempt: int = 1) -> SolveLoop:
    registry = build_default_registry(sandbox or CountingSandbox())
    return SolveLoop(
        llm=llm, registry=registry,
        operations=OperationLog(str(tmp_path / "ops.db")),
        task_id="t1", work_root=tmp_path / "tasks", attempt=attempt,
    )


# ----------------------------------------------------------------------
# 用量计量来源（G35）：内循环多次调用的"估算次数"必须单独可见
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_usage_counts_estimated_calls_separately(tmp_path: Path) -> None:
    """厂商没回 usage 时按文本估算——内循环是多次调用，压成一个布尔会丢信息。

    留痕判据因此是 `estimated_calls < calls`（部分是厂商计量），而不是"这阶段估没估"。
    """
    estimated = ChatResult(
        content=json.dumps({"results": [{"name": "slope", "value": 1.985, "unit": "1"}]},
                           ensure_ascii=False),
        usage=Usage(prompt_tokens=99, completion_tokens=11, total_tokens=110, estimated=True),
        finish_reason="stop",
    )
    outcome = await _loop(tmp_path, ScriptedLLM([estimated])).run("求解拟合")
    usage = outcome["usage"]
    assert usage["estimated_calls"] == usage["calls"] == 1, usage


@pytest.mark.asyncio
async def test_vendor_reported_usage_is_not_counted_as_estimated(tmp_path: Path) -> None:
    """正向对照：厂商回了 usage 就不该记成估算（否则这字段恒真、等于没有）。"""
    outcome = await _loop(
        tmp_path, ScriptedLLM([_final([{"name": "slope", "value": 1.985, "unit": "1"}])])
    ).run("求解拟合")
    usage = outcome["usage"]
    assert usage.get("estimated_calls", 0) == 0
    assert usage["calls"] == 1 and usage["prompt_tokens"] > 0


# ----------------------------------------------------------------------
# OperationLog（崩溃点矩阵）
# ----------------------------------------------------------------------
def test_operations_first_wins(tmp_path: Path) -> None:
    """同键重复 record_done：first-wins，不覆盖终态（重复结算不改写语义对齐）。"""
    ops = OperationLog(str(tmp_path / "ops.db"))
    ops.record_done("t1", "solving", 1, 1, "ref-A")
    ops.record_done("t1", "solving", 1, 1, "ref-B")  # 重放
    record = ops.find("t1", "solving", 1, 1)
    assert record is not None and record.result_ref == "ref-A"
    assert record.status == "done"


def test_operations_running_record_ignored(tmp_path: Path) -> None:
    """running 半写记录（崩溃点：执行后未记终态）→ find 返回 None（按需重执行）。"""
    ops = OperationLog(str(tmp_path / "ops.db"))
    ops._conn.execute(
        "INSERT INTO operations (task_id, stage, attempt, exec_seq, status, result_ref, created_at) "
        "VALUES ('t1','solving',1,1,'running',NULL,'2026-01-01')"
    )
    ops._conn.commit()
    assert ops.find("t1", "solving", 1, 1) is None


def test_operations_records_filter(tmp_path: Path) -> None:
    ops = OperationLog(str(tmp_path / "ops.db"))
    ops.record_done("t1", "solving", 1, 1, "r1")
    ops.record_done("t1", "solving", 1, 2, "r2")
    ops.record_done("t2", "analysis", 1, 1, "r3")
    assert len(ops.records("t1")) == 2
    assert len(ops.records("t1", stage="solving")) == 2
    assert len(ops.records("t2")) == 1


# ----------------------------------------------------------------------
# 循环三路径
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bad_code_auto_repair_converges(tmp_path: Path) -> None:
    """坏代码注入：失败回注 → 模型修正 → 收敛（repair=1, dispatch=2）。"""
    sandbox = CountingSandbox([
        ExecutionResult(exit_code=1, stdout="", stderr="IndexError: bad index"),
        ExecutionResult(exit_code=0, stdout="slope=2.5", stderr=""),
    ])
    llm = ScriptedLLM([
        _tool_call("c1", "execute_code", "raise IndexError('bad index')"),
        _tool_call("c2", "execute_code", "print('slope=2.5')"),
        _final([{"name": "slope", "value": 2.5, "unit": "1"}]),
    ])
    outcome = await _loop(tmp_path, llm, sandbox).run("求解拟合")
    assert outcome["status"] == "succeeded"
    assert outcome["repair_count"] == 1
    assert outcome["dispatch_count"] == 2
    assert outcome["results"] == [{"name": "slope", "value": 2.5, "unit": "1"}]
    assert sandbox.execute_count == 2
    # 工具失败观察回注到模型（tool 角色消息出现在第二次决策前）
    tool_roles = [m for c in llm.calls for m in c["messages"] if m["role"] == "tool"]
    assert tool_roles
    assert outcome["usage"]["calls"] == 3


@pytest.mark.asyncio
async def test_direct_converge_without_tools(tmp_path: Path) -> None:
    """无工具直接给出结果 → 收敛（dispatch=0）。"""
    llm = ScriptedLLM([_final([{"name": "answer", "value": 42}])])
    outcome = await _loop(tmp_path, llm).run("口算题")
    assert outcome["status"] == "succeeded"
    assert outcome["dispatch_count"] == 0
    assert outcome["results"][0]["value"] == 42


@pytest.mark.asyncio
async def test_repair_budget_exhausted_hands_off(tmp_path: Path) -> None:
    """修复预算耗尽 → awaiting_input（复用门禁人工语义），不无界循环。"""
    sandbox = CountingSandbox([
        ExecutionResult(exit_code=1, stdout="", stderr=f"err-{i}") for i in range(6)
    ])
    llm = ScriptedLLM(
        [_tool_call(f"c{i}", "execute_code", f"raise ValueError('e{i}')") for i in range(6)]
    )
    loop = SolveLoop(
        llm=llm, registry=build_default_registry(sandbox),
        operations=OperationLog(str(tmp_path / "ops.db")),
        task_id="t1", work_root=tmp_path / "tasks",
        max_repairs=3,
    )
    outcome = await loop.run("求解")
    assert outcome["status"] == "awaiting_input"
    assert outcome["repair_count"] == 4  # 第 4 次失败超阈值 → handoff
    assert any("预算耗尽" in x for x in outcome["limitations"])
    assert sandbox.execute_count == 4  # handoff 后不再执行


@pytest.mark.asyncio
async def test_invalid_final_json_repaired(tmp_path: Path) -> None:
    """最终输出非 JSON → 回注修复 → 下轮收敛。"""
    bad = ChatResult(content="我觉得答案是 42。", usage=_usage(), finish_reason="stop")
    llm = ScriptedLLM([bad, _final([{"name": "answer", "value": 42}])])
    outcome = await _loop(tmp_path, llm).run("求解")
    assert outcome["status"] == "succeeded"
    assert outcome["repair_count"] == 1
    # 修复反馈入栈（user 角色消息含校验失败提示）
    assert any(
        m["role"] == "user" and "校验失败" in (m.get("content") or "")
        for c in llm.calls for m in c["messages"]
    )


@pytest.mark.asyncio
async def test_nan_rejected_by_parse_constant(tmp_path: Path) -> None:
    """NaN/Infinity 结果拒绝（parse_constant）→ 回注修复 → 收敛。"""
    nan = ChatResult(
        content='{"results": [{"name": "x", "value": NaN}]}', usage=_usage(), finish_reason="stop"
    )
    llm = ScriptedLLM([nan, _final([{"name": "x", "value": 1.5}])])
    outcome = await _loop(tmp_path, llm).run("求解")
    assert outcome["status"] == "succeeded"
    assert outcome["repair_count"] == 1
    assert outcome["results"][0]["value"] == 1.5


@pytest.mark.asyncio
async def test_restore_reuses_completed_executions(tmp_path: Path) -> None:
    """DEC-005 恢复不重放：预登记同一请求的 exec_seq → 循环复用引用，沙箱计数 0。

    预登记必须带 `request_digest`——没有内容维度的记录无法证明"这条就是本次请求"（G53），
    按未知处理要重跑；这条用例测的是"证明得了"那一侧。
    """
    code = "print('would-rerun')"
    ops = OperationLog(str(tmp_path / "ops.db"))
    ops.record_done("t1", "solving", 1, 1, "tool:execute_code:c1", request_digest=_digest(code))
    sandbox = CountingSandbox()
    llm = ScriptedLLM([
        _tool_call("c1", "execute_code", code),
        _final([{"name": "x", "value": 1}]),
    ])
    loop = SolveLoop(
        llm=llm, registry=build_default_registry(sandbox),
        operations=ops, task_id="t1", work_root=tmp_path / "tasks",
    )
    outcome = await loop.run("恢复场景")
    assert outcome["status"] == "succeeded"
    assert sandbox.execute_count == 0  # 已完成执行未重放
    assert len(ops.records("t1")) == 1  # 未新增记录（复用不重复登记）
    assert ops.records("t1")[0].request_digest == _digest(code)


@pytest.mark.asyncio
async def test_tools_payload_exposed_to_llm(tmp_path: Path) -> None:
    """每次决策携带 Registry 工具清单（allowlist → prompt 面）。"""
    llm = ScriptedLLM([_final([{"name": "x", "value": 1}])])
    await _loop(tmp_path, llm).run("求解")
    assert llm.calls[0]["tools"]  # 非空工具清单
    names = [t["function"]["name"] for t in llm.calls[0]["tools"]]
    assert names == ["execute_code", "plot_figure", "search_references"]


# ----------------------------------------------------------------------
# pipeline 接线（降级路径保持）
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pipeline_tool_loop_branch(tmp_path: Path) -> None:
    """tool_loop 模式：solving 经 SolveLoop，产出结构化 results。"""
    llm = ScriptedLLM([_final([{"name": "result", "value": 7}])])
    pipeline = StagePipeline(
        llm=FakeLLM().chat,
        sandbox=None,  # tool_loop 分支不需要阶段级沙箱脚本
        registry=build_default_registry(CountingSandbox()),
        operations=OperationLog(str(tmp_path / "ops.db")),
        solve_llm=llm,
        tool_mode="tool_loop",
        work_root=tmp_path,
    )
    data = await pipeline.process("t9", "solving")
    assert data["mode"] == "tool_loop"
    assert data["status"] == "succeeded"
    assert data["results"] == [{"name": "result", "value": 7}]
    # 一轮都没派发 → 隔离模式如实记 unknown，不拿"管线手里的沙箱"（这里是 None）凑一个值
    assert data["isolation_mode"] == "unknown"


@pytest.mark.asyncio
async def test_tool_loop_isolation_mode_comes_from_the_executing_sandbox(tmp_path) -> None:  # noqa: ANN001
    """真派发过代码时，隔离模式必须来自跑代码的那个沙箱（执行体自报，经循环带出）。"""
    inner = CountingSandbox()
    pipeline = StagePipeline(
        llm=FakeLLM().chat,
        sandbox=None,
        registry=build_default_registry(inner),
        operations=OperationLog(str(tmp_path / "ops.db")),
        solve_llm=ScriptedLLM([_tool_call("c1", "execute_code", "print(1)"), _final([{"name": "r", "value": 1}])]),
        tool_mode="tool_loop",
        work_root=tmp_path,
    )
    data = await pipeline.process("t9b", "solving")

    assert inner.execute_count == 1
    assert data["dispatch_count"] == 1
    assert data["isolation_mode"] == "counter", (
        f"隔离模式没取自营沙箱：{data['isolation_mode']}"
    )


@pytest.mark.asyncio
async def test_pipeline_stage_level_fallback_unchanged(tmp_path: Path) -> None:
    """stage_level 降级路径保持 SP1-7 行为（硬编码脚本 + 复核），不走循环。

    两个判据都是"正向信号"而不是"字段缺失"：
    ① "没走循环"看执行体被调用几次（走循环会是多次派发），不是看 `mode` 键在不在——
       W23② 起两条路径都显式自述 mode，靠缺字段反推路径正是本次要消掉的形状；
    ② `isolation_mode` 必须是**真正跑代码的那个沙箱**自报的（这里是 Registry 里的
       CountingSandbox），而不是管线手里握着的 SubprocessSandbox——装配巧合下两者相同，
       合规字段不能靠巧合（DEC-006）。
    """
    inner = CountingSandbox()
    pipeline = StagePipeline(
        llm=FakeLLM().chat,
        sandbox=SubprocessSandbox(timeout=30),
        registry=build_default_registry(inner),
        operations=OperationLog(str(tmp_path / "ops.db")),
        solve_llm=ScriptedLLM([_final([{"name": "x", "value": 1}])]),
        tool_mode="stage_level",
        work_root=tmp_path,
    )
    data = await pipeline.process("t10", "solving")
    assert "exit_code" in data  # 既有阶段级产出字段
    assert data["mode"] == "stage_level"  # 自述路径，不靠调用方反推
    assert inner.execute_count == 1, "降级路径应恰执行一次（走循环会是多次派发）"
    assert data["isolation_mode"] == "counter", (
        f"隔离模式取自未执行的沙箱：{data['isolation_mode']}"
    )


# ----------------------------------------------------------------------
# W15 delta 流（model.delta）
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_solve_loop_delta_sink_receives_throttled_text(tmp_path: Path) -> None:
    """delta_sink 收到节流合并文本；model.delta 不进入 replay 缓冲（允许丢帧不补发）。"""
    import io

    from engine.ipc.events import EventEmitter

    llm = ScriptedLLM(
        [_final([{"name": "x", "value": 1}])],
        emit_deltas=["模型", "输出中"],
    )
    deltas: list[tuple[str, str]] = []
    loop = SolveLoop(
        llm=llm, registry=build_default_registry(CountingSandbox()),
        operations=OperationLog(str(tmp_path / "ops.db")),
        task_id="t-delta", work_root=tmp_path / "tasks",
        delta_sink=lambda task_id, delta: deltas.append((task_id, delta)),
    )
    outcome = await loop.run("流式求解")
    assert outcome["status"] == "succeeded"
    assert ("t-delta", "模型输出中") in deltas  # 50ms 内合并为一条

    events = EventEmitter(sink=io.StringIO())
    events.emit("model.delta", task_id="t1", delta="部分文本")
    events.emit("stage.progress", task_id="t1", stage="analysis", progress=1.0)
    replayed = events.replay(after_seq=0)
    assert [e["event"] for e in replayed] == ["stage.progress"]  # delta 不补发


# ----------------------------------------------------------------------
# EC-T4 哈希校验链：引用产物完整性（记录哈希 → 复用前校验 → 损坏重执行）
# ----------------------------------------------------------------------
class ArtifactSandbox:
    """写产物文件的沙箱替身：每次执行覆盖 out.txt（内容随次数变化）。

    与真实执行器一致：写文件前自行创建工作目录；fail_on_exec 指定第 N 次
    执行返回失败（exit_code 1，不产文件），用于损坏重执行失败分支。
    """

    def __init__(self, fail_on_exec: set[int] | None = None) -> None:
        self.execute_count = 0
        self._fail_on_exec = fail_on_exec or set()

    async def execute(self, code: str, files: dict, work_dir: Path) -> ExecutionResult:
        self.execute_count += 1
        work_dir.mkdir(parents=True, exist_ok=True)
        if self.execute_count in self._fail_on_exec:
            return ExecutionResult(exit_code=1, stdout="", stderr="boom", artifacts=[])
        (work_dir / "out.txt").write_text(f"v{self.execute_count}", encoding="utf-8")
        return ExecutionResult(exit_code=0, stdout="ok", stderr="", artifacts=["out.txt"])


def test_operations_migration_adds_hash_column(tmp_path: Path) -> None:
    """旧库（无 result_sha256 列）打开即迁移，record/find 携带哈希。"""
    db = tmp_path / "ops.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE operations (task_id TEXT NOT NULL, stage TEXT NOT NULL, "
        "attempt INTEGER NOT NULL, exec_seq INTEGER NOT NULL, status TEXT NOT NULL, "
        "result_ref TEXT, created_at TEXT NOT NULL, "
        "PRIMARY KEY (task_id, stage, attempt, exec_seq))"
    )
    conn.commit()
    conn.close()

    ops = OperationLog(str(db))
    ops.record_done("t1", "solving", 1, 1, "out.txt", result_sha256="abc")
    record = ops.find("t1", "solving", 1, 1)
    assert record is not None
    assert record.result_sha256 == "abc"


def test_operations_force_updates_hash_on_repair(tmp_path: Path) -> None:
    """first-wins 默认不覆盖（重放不改写）；force=True 刷新引用与哈希（修复路径）。"""
    ops = OperationLog(str(tmp_path / "ops.db"))
    ops.record_done("t1", "solving", 1, 1, "out.txt", result_sha256="h1")
    ops.record_done("t1", "solving", 1, 1, "out.txt", result_sha256="h2")
    first = ops.find("t1", "solving", 1, 1)
    assert first is not None and first.result_sha256 == "h1"  # first-wins
    ops.record_done("t1", "solving", 1, 1, "out.txt", result_sha256="h2", force=True)
    repaired = ops.find("t1", "solving", 1, 1)
    assert repaired is not None and repaired.result_sha256 == "h2"  # 修复刷新


def test_artifact_sha256_helpers(tmp_path: Path) -> None:
    """哈希助手：存在文件返回摘要，缺失/空引用返回 None。"""
    (tmp_path / "out.txt").write_bytes(b"payload")
    assert artifact_sha256(tmp_path, "out.txt") == hashlib.sha256(b"payload").hexdigest()
    assert artifact_sha256(tmp_path, "missing.txt") is None
    assert artifact_sha256(tmp_path, None) is None


async def test_restore_reuses_intact_artifact_without_replay(tmp_path: Path) -> None:
    """EC-T4：同一请求、记录带哈希且产物完好 → 复用引用，沙箱零重放（DEC-005）。

    恢复侧必须重放**同一段代码**才谈得上"不重放副作用"；换了代码还复用是 G53 那个缺陷，
    由 `test_restore_does_not_reuse_a_different_request` 反向钉住。
    """
    sandbox = ArtifactSandbox()
    ops = OperationLog(str(tmp_path / "ops.db"))
    llm = ScriptedLLM([
        _tool_call("c1", "execute_code", "print('a')"),
        _final([{"name": "x", "value": 1}]),
        _tool_call("c1", "execute_code", "print('a')"),
        _final([{"name": "x", "value": 1}]),
    ])
    loop = SolveLoop(llm=llm, registry=build_default_registry(sandbox),
                     operations=ops, task_id="t1", work_root=tmp_path / "tasks")
    await loop.run("第一次求解")
    first = ops.find("t1", "solving", 1, 1)
    assert first is not None and first.result_sha256
    first_sha = first.result_sha256
    assert sandbox.execute_count == 1

    resumed = SolveLoop(llm=llm, registry=build_default_registry(sandbox),
                        operations=ops, task_id="t1", work_root=tmp_path / "tasks")
    outcome = await resumed.run("恢复续跑")
    assert outcome["status"] == "succeeded"
    assert sandbox.execute_count == 1  # 产物完好：不重放
    reused = ops.find("t1", "solving", 1, 1)
    assert reused is not None and reused.result_sha256 == first_sha


async def test_restore_hash_mismatch_reexecutes_and_refreshes(tmp_path: Path) -> None:
    """EC-T4：产物被篡改 → 按损坏重执行，成功后刷新记录哈希（force）。

    恢复侧给出的是**同一段代码**，所以触发重执行的唯一原因就是哈希不一致（G53 之后
    "换了请求"也是一条独立理由，不能让它替这条用例背结果）。
    """
    sandbox = ArtifactSandbox()
    ops = OperationLog(str(tmp_path / "ops.db"))
    llm = ScriptedLLM([
        _tool_call("c1", "execute_code", "print('a')"),
        _final([{"name": "x", "value": 1}]),
        _tool_call("c1", "execute_code", "print('a')"),
        _final([{"name": "x", "value": 1}]),
    ])
    loop = SolveLoop(llm=llm, registry=build_default_registry(sandbox),
                     operations=ops, task_id="t1", work_root=tmp_path / "tasks")
    await loop.run("第一次求解")
    original = ops.find("t1", "solving", 1, 1)
    assert original is not None and original.result_sha256
    old_sha = original.result_sha256

    (tmp_path / "tasks" / "t1" / "out.txt").write_text("tampered", encoding="utf-8")

    resumed = SolveLoop(llm=llm, registry=build_default_registry(sandbox),
                        operations=ops, task_id="t1", work_root=tmp_path / "tasks")
    outcome = await resumed.run("恢复续跑")
    assert outcome["status"] == "succeeded"
    assert sandbox.execute_count == 2  # 损坏 → 重执行
    record = ops.find("t1", "solving", 1, 1)
    assert record is not None
    assert record.result_sha256 not in (None, old_sha)  # 修复成功后刷新哈希


async def test_restore_hash_mismatch_repair_failure_keeps_record(tmp_path: Path) -> None:
    """EC-T4：损坏重执行失败 → 记录保持原终态，修复预算路径接管。

    失败的重执行**不写账**（G53：登记即宣称副作用完成），所以原终态天然不被污染；
    下次恢复再次校验再次重执行，幂等收敛。
    """
    sandbox = ArtifactSandbox(fail_on_exec={2})  # 恢复后的重执行失败
    ops = OperationLog(str(tmp_path / "ops.db"))
    llm = ScriptedLLM([
        _tool_call("c1", "execute_code", "print('a')"),
        _final([{"name": "x", "value": 1}]),
        _tool_call("c1", "execute_code", "print('a')"),
        _final([{"name": "x", "value": 1}]),
    ])
    loop = SolveLoop(llm=llm, registry=build_default_registry(sandbox),
                     operations=ops, task_id="t1", work_root=tmp_path / "tasks")
    await loop.run("第一次求解")
    original = ops.find("t1", "solving", 1, 1)
    assert original is not None and original.result_sha256
    old_sha = original.result_sha256

    (tmp_path / "tasks" / "t1" / "out.txt").write_text("tampered", encoding="utf-8")

    resumed = SolveLoop(llm=llm, registry=build_default_registry(sandbox),
                        operations=ops, task_id="t1", work_root=tmp_path / "tasks")
    outcome = await resumed.run("恢复续跑")
    assert outcome["status"] == "succeeded"
    assert sandbox.execute_count == 2  # 损坏触发了重执行
    record = ops.find("t1", "solving", 1, 1)
    assert record is not None
    assert record.result_sha256 == old_sha  # 失败的重执行不污染记录（first-wins）
    assert outcome["repair_count"] == 1  # 失败走修复预算路径


def test_artifact_intact_rejects_escape_ref(tmp_path: Path) -> None:
    """引用越出工作目录（记录被篡改情形）→ 视为损坏，不读取工作目录外文件（AT-13）。"""
    loop = SolveLoop(
        llm=ScriptedLLM([]), registry=build_default_registry(ArtifactSandbox()),
        operations=OperationLog(str(tmp_path / "ops.db")),
        task_id="t1", work_root=tmp_path / "tasks",
    )
    outside = tmp_path / "escape.txt"
    outside.write_text("secret", encoding="utf-8")
    record = OperationRecord("t1", "solving", 1, 1, "done", "../escape.txt", "deadbeef")
    assert loop._artifact_intact(record) is False
    assert outside.read_text(encoding="utf-8") == "secret"  # 越界文件未被触碰（防越权读取）


# ----------------------------------------------------------------------
# §8 首 token 延迟预算（引擎侧切片）：适配器回调 → 节流 → delta_sink
# ----------------------------------------------------------------------
class TimingLLM(ScriptedLLM):
    """在适配器回调边界记录首个 token 时刻（延迟测量锚点）。"""

    def __init__(self, results: list[ChatResult], emit_deltas: list[str] | None = None) -> None:
        super().__init__(results, emit_deltas)
        self.first_token_at: float | None = None

    async def __call__(
        self, messages: list[ChatMessage], tools: list[dict],
        on_delta: Callable[[str], None] | None = None,
    ) -> ChatResult:
        timed = on_delta
        if on_delta is not None:
            def timed(delta: str) -> None:
                if self.first_token_at is None:
                    self.first_token_at = time.perf_counter()
                on_delta(delta)
        return await super().__call__(messages, tools, on_delta=timed)


async def test_first_token_latency_engine_side_budget(tmp_path: Path) -> None:
    """§8 首 token <500ms 口径的引擎侧切片：模型回调 → 节流 → delta_sink。

    500ms 全口径含厂商网络与真实模型出 token 时间（E2 固定机型+固定模型实测，
    属 11-05+ 窗口）；本测试钉住引擎自身开销上限——节流窗口 50ms + 下发开销，
    证明引擎侧不构成预算主体。
    """
    received: list[float] = []
    llm = TimingLLM([_final([{"name": "x", "value": 1}])], emit_deltas=["首", "token"])
    loop = SolveLoop(
        llm=llm, registry=build_default_registry(CountingSandbox()),
        operations=OperationLog(str(tmp_path / "ops.db")),
        task_id="t-latency", work_root=tmp_path / "tasks",
        delta_sink=lambda task_id, delta: received.append(time.perf_counter()),
    )
    outcome = await loop.run("延迟预算")
    assert outcome["status"] == "succeeded"
    assert llm.first_token_at is not None and received  # 首 token 与下沉均发生
    assert received[0] - llm.first_token_at < 0.5  # 引擎侧开销远低于 500ms 预算


# ----------------------------------------------------------------------
# G53 幂等账本的内容维度：复用必须能证明"这条记录就是本次请求"
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_restore_does_not_reuse_a_different_request(tmp_path: Path) -> None:
    """幂等键只有位置（attempt, exec_seq）没有内容时，恢复会把**上一个程序**的输出当成这次的结果。

    真实厂商重跑不会逐字给出同一段代码（只有 FakeLLM 会），而复用给模型的观察是
    "[系统] 复用已完成执行 #N"——它这次写的代码一次都没跑，数字却来自旧程序，
    且没有任何一处说出这一点（DEC-005 的"不重放"前提是先确认同一个副作用）。
    """
    ops = OperationLog(str(tmp_path / "ops.db"))
    sandbox = CountingSandbox()
    first = ScriptedLLM([
        _tool_call("c1", "execute_code", "print('A')"),
        _final([{"name": "x", "value": 1}]),
    ])
    await SolveLoop(llm=first, registry=build_default_registry(sandbox), operations=ops,
                    task_id="t1", work_root=tmp_path / "tasks").run("第一次求解")
    assert sandbox.execute_count == 1

    again = ScriptedLLM([
        _tool_call("c9", "execute_code", "print('B')"),  # 同位置、不同请求
        _final([{"name": "x", "value": 2}]),
    ])
    outcome = await SolveLoop(llm=again, registry=build_default_registry(sandbox), operations=ops,
                              task_id="t1", work_root=tmp_path / "tasks").run("恢复续跑")

    assert outcome["status"] == "succeeded"
    assert sandbox.execute_count == 2, "改了代码却没重新执行——模型拿到的是旧程序的数字"
    assert sandbox.codes == ["print('A')", "print('B')"]
    observations = [m["content"] for m in again.calls[1]["messages"] if m["role"] == "tool"]
    assert not any("复用已完成执行" in text for text in observations), observations


@pytest.mark.asyncio
async def test_failed_dispatch_leaves_no_done_record(tmp_path: Path) -> None:
    """失败的派发不得登记成 done：`status` 只有 done 一个写入方，登记即等于宣称副作用已完成。

    崩溃点矩阵的口径是"宁可重跑，不可把没做完的当做完"（operations.py：仅执行成功后登记）。
    把失败记成 done，下一次恢复就会把这条失败复用成"[系统] 复用已完成执行 #N"。
    """
    ops = OperationLog(str(tmp_path / "ops.db"))
    sandbox = ArtifactSandbox(fail_on_exec={1})  # 第 1 次派发失败、第 2 次成功
    llm = ScriptedLLM([
        _tool_call("c1", "execute_code", "print('bad')"),
        _tool_call("c2", "execute_code", "print('good')"),
        _final([{"name": "x", "value": 1}]),
    ])
    outcome = await SolveLoop(llm=llm, registry=build_default_registry(sandbox), operations=ops,
                              task_id="t1", work_root=tmp_path / "tasks").run("首次求解")
    assert outcome["status"] == "succeeded"
    assert outcome["repair_count"] == 1
    assert ops.find("t1", "solving", 1, 1) is None, "第 1 次派发失败了，账上却有一条 done"
    second = ops.find("t1", "solving", 1, 2)
    assert second is not None, "成功的那次必须登记（否则恢复白跑）"
    assert second.request_digest == _digest("print('good')")


@pytest.mark.asyncio
async def test_legacy_record_without_digest_reexecutes_then_converges(tmp_path: Path) -> None:
    """旧库行缺内容维度 → 第一次恢复重跑并在成功后补齐摘要，第二次恢复才复用。

    迁移不能把"读不出摘要"当成"摘要一致"：重跑一次沙箱是可以承受的代价，把没跑成的说成
    跑过了不可以。收敛性也要测——否则修复只是把复用永久改成重放。
    """
    ops = OperationLog(str(tmp_path / "ops.db"))
    ops.record_done("t1", "solving", 1, 1, "tool:execute_code:c1")  # 迁移前写法：无摘要
    sandbox = CountingSandbox()
    script = [_tool_call("c1", "execute_code", "print('A')"), _final([{"name": "x", "value": 1}])]
    first = ScriptedLLM(list(script))
    outcome = await SolveLoop(llm=first, registry=build_default_registry(sandbox), operations=ops,
                              task_id="t1", work_root=tmp_path / "tasks").run("恢复续跑")
    assert outcome["status"] == "succeeded"
    assert sandbox.execute_count == 1, "无摘要的记录必须按未知请求处理：重执行"
    refilled = ops.find("t1", "solving", 1, 1)
    assert refilled is not None and refilled.request_digest == _digest("print('A')")

    second = ScriptedLLM(list(script))
    await SolveLoop(llm=second, registry=build_default_registry(sandbox), operations=ops,
                    task_id="t1", work_root=tmp_path / "tasks").run("再次恢复")
    assert sandbox.execute_count == 1, "补齐摘要后同一请求应回到不重放（DEC-005）"


def test_request_digest_is_content_sensitive() -> None:
    """摘要判据：同请求稳定，换代码/换工具名即不同——厂商两次给出同一段代码才能复用。

    键序/空白不同会得到不同摘要（参数原文参与哈希），这是**刻意选的坏方向**：多跑一次沙箱
    可以承受，把没跑过的说成跑过了不可以。
    """
    a = request_digest("execute_code", json.dumps({"code": "print(1)"}))
    assert a == request_digest("execute_code", json.dumps({"code": "print(1)"}))
    assert a != request_digest("execute_code", json.dumps({"code": "print(2)"}))
    assert a != request_digest("plot_figure", json.dumps({"code": "print(1)"}))
    assert a != request_digest("execute_code", json.dumps({"code": "print(1)", "language": "python"}))
    # 语义相同但书写不同 → 不同摘要（宁可重跑）
    assert a != request_digest("execute_code", '{"code":"print(1)"}')


def test_operations_migration_adds_digest_column(tmp_path: Path) -> None:
    """旧库（既无 result_sha256 也无 request_digest）打开即迁移，旧行摘要读回 None 而非猜测。"""
    db = tmp_path / "ops.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE operations (task_id TEXT NOT NULL, stage TEXT NOT NULL, "
        "attempt INTEGER NOT NULL, exec_seq INTEGER NOT NULL, status TEXT NOT NULL, "
        "result_ref TEXT, created_at TEXT NOT NULL, "
        "PRIMARY KEY (task_id, stage, attempt, exec_seq))"
    )
    conn.execute(
        "INSERT INTO operations VALUES ('t1','solving',1,1,'done','out.txt','2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    ops = OperationLog(str(db))
    legacy = ops.find("t1", "solving", 1, 1)
    assert legacy is not None
    assert legacy.request_digest is None
    assert legacy.same_request(request_digest("execute_code", "{}")) is False  # 未知即不复用

    ops.record_done("t1", "solving", 1, 2, "out.txt", request_digest="d2")
    fresh = ops.records("t1", stage="solving")
    assert [r.request_digest for r in fresh] == [None, "d2"]



def test_dispatch_structure_pins_the_two_reuse_preconditions() -> None:
    """结构守护：复用的两条前置（同请求、成功才入账）不能被整段绕开（G53）。

    行为用例已经钉住语义，这里钉的是"写法退回去就红"——把 `record_done` 挪出成功分支，
    替身沙箱下某些用例照样绿（失败分支根本走不到），只有结构判据拦得住。
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(SolveLoop._dispatch)))
    calls: list[tuple[ast.Call, list[ast.If]]] = []

    def walk(node: ast.AST, ifs: list[ast.If]) -> None:
        if isinstance(node, ast.Call):
            calls.append((node, ifs))
        for child in ast.iter_child_nodes(node):
            next_ifs = ifs + [node] if isinstance(node, ast.If) else ifs
            walk(child, next_ifs)

    walk(tree, [])
    record_calls = [
        (call, guards)
        for call, guards in calls
        if isinstance(call.func, ast.Attribute) and call.func.attr == "record_done"
    ]
    assert len(record_calls) == 1
    guards = record_calls[0][1]
    assert any("result.ok" in ast.unparse(g.test) for g in guards), (
        "record_done 不在成功分支里：失败也会被登记成 done，下次恢复把失败说成已完成执行"
    )
    assert any(
        isinstance(n.func, ast.Attribute) and n.func.attr == "same_request"
        for n, _ in calls
    ), "复用前不再比对请求内容：幂等键退回只按位置判定"
    # 复用观察文本只有一处来源，且必须由同请求分支给出（第二处=开始凭位置编故事）
    reuse_literals = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr) and any(
            isinstance(p, ast.Constant) and isinstance(p.value, str) and "复用已完成执行" in p.value
            for p in node.values
        )
    ]
    assert len(reuse_literals) == 1
