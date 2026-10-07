"""EN-LOOP（W11）求解内循环测试：幂等矩阵 + 循环三路径 + 收敛硬检查 + pipeline 接线。

覆盖（《任务执行手册》W11 验收）：
- OperationLog：find/record_done/first-wins/running 半写按未完成处理（崩溃点矩阵）；
- 坏代码自动修复（FakeLLM 脚本序列）：失败回注 → 修正 → 收敛，repair_count 递增；
- 修复预算耗尽 → awaiting_input（转人工，不静默失败、不无界循环）；
- 结果校验：非 JSON/NaN-Infinity 拒绝（parse_constant）→ 回注修复后收敛；
- 恢复不重放（DEC-005）：预登记 exec_seq → 循环复用引用，沙箱执行计数为 0；
- pipeline 接线：tool_loop 分支产出结构化 results；stage_level 降级路径保持不变。
FakeLLM/脚本化 LLM 为确定性假模型（测试专用），不产生真实模型调用。
"""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from engine.adapters.openai_compat import ChatMessage, ChatResult, ToolCall, Usage
from engine.orchestrator.operations import OperationLog, OperationRecord
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


def _loop(tmp_path: Path, llm: ScriptedLLM, sandbox: CountingSandbox | None = None, attempt: int = 1) -> SolveLoop:
    registry = build_default_registry(sandbox or CountingSandbox())
    return SolveLoop(
        llm=llm, registry=registry,
        operations=OperationLog(str(tmp_path / "ops.db")),
        task_id="t1", work_root=tmp_path / "tasks", attempt=attempt,
    )


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
    """DEC-005 恢复不重放：预登记 exec_seq=1 → 循环复用引用，沙箱计数 0。"""
    ops = OperationLog(str(tmp_path / "ops.db"))
    ops.record_done("t1", "solving", 1, 1, "tool:execute_code:c1")
    sandbox = CountingSandbox()
    llm = ScriptedLLM([
        _tool_call("c1", "execute_code", "print('would-rerun')"),
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


@pytest.mark.asyncio
async def test_pipeline_stage_level_fallback_unchanged(tmp_path: Path) -> None:
    """stage_level 降级路径保持 SP1-7 行为（硬编码脚本 + 复核），不走循环。"""
    pipeline = StagePipeline(
        llm=FakeLLM().chat,
        sandbox=SubprocessSandbox(timeout=30),
        registry=build_default_registry(CountingSandbox()),
        operations=OperationLog(str(tmp_path / "ops.db")),
        solve_llm=ScriptedLLM([_final([{"name": "x", "value": 1}])]),
        tool_mode="stage_level",
        work_root=tmp_path,
    )
    data = await pipeline.process("t10", "solving")
    assert "exit_code" in data  # 既有阶段级产出字段
    assert "mode" not in data  # 未进入 tool_loop


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
    """EC-T4：记录带哈希且产物完好 → 复用引用，沙箱零重放（DEC-005）。"""
    sandbox = ArtifactSandbox()
    ops = OperationLog(str(tmp_path / "ops.db"))
    llm = ScriptedLLM([
        _tool_call("c1", "execute_code", "print('a')"),
        _final([{"name": "x", "value": 1}]),
        _tool_call("c2", "execute_code", "print('b')"),
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
    """EC-T4：产物被篡改 → 按损坏重执行，成功后刷新记录哈希（force）。"""
    sandbox = ArtifactSandbox()
    ops = OperationLog(str(tmp_path / "ops.db"))
    llm = ScriptedLLM([
        _tool_call("c1", "execute_code", "print('a')"),
        _final([{"name": "x", "value": 1}]),
        _tool_call("c2", "execute_code", "print('b')"),
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
    """EC-T4：损坏重执行失败 → 记录保持原终态（first-wins 保底），修复预算路径接管。

    记录不被失败的重执行污染：下次恢复再次校验再次重执行，幂等收敛。
    """
    sandbox = ArtifactSandbox(fail_on_exec={2})  # 恢复后的重执行失败
    ops = OperationLog(str(tmp_path / "ops.db"))
    llm = ScriptedLLM([
        _tool_call("c1", "execute_code", "print('a')"),
        _final([{"name": "x", "value": 1}]),
        _tool_call("c2", "execute_code", "print('b')"),
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
