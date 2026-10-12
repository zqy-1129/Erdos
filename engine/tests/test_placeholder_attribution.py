"""占位工具的归因面守护（G48 三条出路里的 ③，2026-10-12）。

`search_references` 在 EN-19A 之前恒返回 `NOT_AVAILABLE`，却与真工具一起进模型的工具列表；
`solve_loop` 的 `dispatch_count` 在派发**之前**自增、不分成败，于是 8 次预算可被一个永不成功的
工具烧光，终态写「修复/分发预算耗尽」——**把"引擎宣告了自己满足不了的能力"报成模型或沙箱的问题**。

本批只取**归因**这一条出路：单列 `unavailable_count`，预算耗尽时在 `limitations` 里写明其中几次
是引擎尚未实现的能力。**预算语义一行未动**——出路①（不向模型宣告未实现能力）改的是模型可见输入，
属口径；出路②（NOT_AVAILABLE 不计预算）改的是循环终止条件，属判定通道行为。两条都要 EN/QA 择一，
不擅自选边实现，所以这里同时把"现状"钉成用例（见下面那条 `does_not_change_budget_accounting`）。
"""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from engine.adapters.openai_compat import ChatMessage, ChatResult, ToolCall, Usage
from engine.orchestrator.operations import OperationLog
from engine.orchestrator.solve_loop import LOOP_CHANNELS, SolveLoop
from engine.sandbox.base import ExecutionResult
from engine.tools import build_default_registry
from engine.tools.base import TOOL_NOT_AVAILABLE, ToolContext
from engine.tools.search_tools import SearchReferencesArgs, search_references


class ScriptedLLM:
    """按队列返回 ChatResult 的确定性假模型（测试专用，不产生真实调用）。"""

    def __init__(self, results: list[ChatResult]) -> None:
        self._queue = list(results)

    async def __call__(
        self, messages: list[ChatMessage], tools: list[dict],
        on_delta: Callable[[str], None] | None = None,
    ) -> ChatResult:
        return self._queue.pop(0)


class CountingSandbox:
    """真执行会计数的沙箱替身；结果队列可编程（空则成功）。"""

    isolation_mode = "counter"

    def __init__(self, results: list[ExecutionResult] | None = None) -> None:
        self.execute_count = 0
        self._results = list(results or [])

    async def execute(self, code: str, files: dict, work_dir: Path) -> ExecutionResult:
        self.execute_count += 1
        return self._results.pop(0) if self._results else ExecutionResult(
            exit_code=0, stdout="ok", stderr="", artifacts=[]
        )


def _usage() -> Usage:
    return Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5)


def _call(call_id: str, name: str, args: dict) -> ChatResult:
    return ChatResult(
        content="", usage=_usage(), finish_reason="tool_calls",
        tool_calls=[ToolCall(id=call_id, name=name, arguments_json=json.dumps(args))],
    )


def _search(call_id: str) -> ChatResult:
    return _call(call_id, "search_references", {"query": "线性规划 参考论文", "top_k": 3})


def _bad_code(call_id: str) -> ChatResult:
    return _call(call_id, "execute_code", {"code": f"raise ValueError('{call_id}')"})


def _loop(tmp_path: Path, llm: ScriptedLLM, sandbox: CountingSandbox) -> SolveLoop:
    return SolveLoop(
        llm=llm, registry=build_default_registry(sandbox),
        operations=OperationLog(str(tmp_path / "ops.db")),
        task_id="t-g48", work_root=tmp_path / "tasks",
    )


def _exhaustion_note(outcome: dict) -> str:
    return next(str(x) for x in outcome["limitations"] if "预算耗尽" in x)


@pytest.mark.asyncio
async def test_placeholder_only_burn_says_the_engine_lacks_the_capability(
    tmp_path: Path,
) -> None:
    """全是占位失败的耗尽：归因必须点名"引擎尚未实现的能力"，不能只报两个预算数字。"""
    llm = ScriptedLLM([_search(f"s{i}") for i in range(4)])

    outcome = await _loop(tmp_path, llm, CountingSandbox()).run("求解")

    note = _exhaustion_note(outcome)
    assert "尚未实现的能力" in note, f"归因仍然只说预算，读者会去找模型或沙箱：{note}"
    assert "4 次" in note and "非模型或沙箱故障" in note


@pytest.mark.asyncio
async def test_placeholder_burn_does_not_change_budget_accounting(tmp_path: Path) -> None:
    """反向承诺：本批只加归因，没顺手改终止条件——占位失败照旧计 dispatch 与 repair。

    这条是把"出路②尚未实施"钉成现状：真去做②时必须改这条用例，而不是让它静默跟着变绿。
    """
    llm = ScriptedLLM([_search(f"s{i}") for i in range(4)])

    outcome = await _loop(tmp_path, llm, CountingSandbox()).run("求解")

    assert outcome["status"] == "awaiting_input"
    assert outcome["dispatch_count"] == 4 and outcome["repair_count"] == 4
    assert outcome["results"] == []


@pytest.mark.asyncio
async def test_real_tool_failure_does_not_claim_missing_capability(tmp_path: Path) -> None:
    """正向对照（防过度声明）：真工具跑挂时不得出现"尚未实现的能力"。"""
    sandbox = CountingSandbox([
        ExecutionResult(exit_code=1, stdout="", stderr=f"boom-{i}") for i in range(6)
    ])
    llm = ScriptedLLM([_bad_code(f"c{i}") for i in range(6)])

    outcome = await _loop(tmp_path, llm, sandbox).run("求解")

    assert "尚未实现的能力" not in _exhaustion_note(outcome)
    assert sandbox.execute_count == 4  # 真执行了四次，失败来自代码而不是能力缺失


@pytest.mark.asyncio
async def test_mixed_burn_counts_only_placeholder_failures(tmp_path: Path) -> None:
    """混合场景：4 次里 2 次占位失败 → 只报 2 次，另两次仍归因到执行失败。"""
    sandbox = CountingSandbox([
        ExecutionResult(exit_code=1, stdout="", stderr="boom"),
        ExecutionResult(exit_code=1, stdout="", stderr="boom"),
    ])
    llm = ScriptedLLM([_search("s0"), _search("s1"), _bad_code("c2"), _bad_code("c3")])

    outcome = await _loop(tmp_path, llm, sandbox).run("求解")

    assert "2 次" in _exhaustion_note(outcome)


def test_unavailable_channel_is_declared_for_both_backends() -> None:
    """通道从 `SolveState` 注解导出：漏声明时 native 内核 fail-loud，但 langgraph 档可能静默丢计数。"""
    assert "unavailable_count" in LOOP_CHANNELS


def test_placeholder_error_code_and_attribution_share_one_source(tmp_path: Path) -> None:
    """占位执行器返回的码 ≡ 归因分支比的码（两份字面量各写一遍=静默失配的来源）。"""
    ctx = ToolContext(task_id="t1", stage="solving", work_dir=tmp_path)
    # `top_k` 必须显式给：pydantic 的默认值写作 `Field(3, ...)`，本仓没开 mypy 插件，
    # 类型上看不出它有默认值 → 省略就是 call-arg 错误（CI 的 mypy 步骤就是这么红的）。
    result = asyncio.run(search_references(SearchReferencesArgs(query="q", top_k=3), ctx))

    assert result.error is not None
    assert result.error.code == TOOL_NOT_AVAILABLE == "NOT_AVAILABLE"
