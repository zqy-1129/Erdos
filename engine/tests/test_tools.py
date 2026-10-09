"""EN-REG（W9）Tool Registry 测试：dispatch 三路径 + 三件套 + 事件/留痕/脱敏。

覆盖（《任务执行手册》W9 验收）：
- dispatch 三路径：未知工具 TOOL_UNKNOWN / 参数校验 TOOL_ARGS_INVALID / 成功；
- 未注册工具被拒且留痕 + 事件回流（tool.call + tool.result）；
- prompt 注入抵抗：allowlist 外工具名（含注入诱导）一律拒绝；
- execute_code 集成（SubprocessSandbox 真跑）：成功/异常收敛/语言守卫/产物越界；
- plot_figure：生成脚本含 Agg+中文字体回退链；假沙箱验证产物引用；
- search_references 占位 NOT_AVAILABLE；
- 脱敏：sk-/Bearer 形状凭据在事件与摘要出口被掩码；summary 截断 2000。
"""

import io
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from engine.adapters.openai_compat import ToolCall
from engine.ipc.events import EventEmitter
from engine.sandbox.subprocess_sandbox import SubprocessSandbox
from engine.tools import build_default_registry
from engine.tools.base import ToolContext, ToolResult, ToolSpec
from engine.tools.plot_tools import PlotFigureArgs, SeriesSpec, build_plot_script
from engine.tools.registry import ToolRegistry, clip, mask_secrets
from engine.trail.recorder import TrailRecorder
from engine.trail.store import TrailStore


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(task_id="t1", stage="solving", work_dir=tmp_path / "work")


def _registry(tmp_path: Path, sandbox=None, with_trail: bool = True):
    """构造带事件捕获与留痕的 Registry（execute_code 用真沙箱，plot 可注入假沙箱）。"""
    sink = io.StringIO()
    events = EventEmitter(sink=sink, trace_id="trace-9")
    trail = TrailRecorder(TrailStore(str(tmp_path / "audit.db"))) if with_trail else None
    registry = build_default_registry(sandbox or SubprocessSandbox(timeout=30), events=events, trail=trail)
    return registry, sink, events, trail


# ----------------------------------------------------------------------
# 注册表基础
# ----------------------------------------------------------------------
def test_duplicate_register_rejected(tmp_path: Path) -> None:
    """重复注册拒绝（防 allowlist 覆盖）。"""
    registry, _, _, _ = _registry(tmp_path, with_trail=False)

    class Empty(BaseModel):
        pass

    async def noop(args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True)

    with pytest.raises(ValueError, match="重复注册"):
        registry.register(ToolSpec(name="execute_code", description="x", args_model=Empty, danger_level="low", executor=noop))


def test_tool_payloads_schema_from_args_model(tmp_path: Path) -> None:
    """parameters 由 pydantic args_model 单源生成（execute_code 含 code 属性）。"""
    registry, _, _, _ = _registry(tmp_path, with_trail=False)
    payloads = registry.tool_payloads()
    names = [p["function"]["name"] for p in payloads]
    assert names == ["execute_code", "plot_figure", "search_references"]
    code_tool = next(p for p in payloads if p["function"]["name"] == "execute_code")
    assert "code" in code_tool["function"]["parameters"]["properties"]
    assert code_tool["type"] == "function"


def test_summary_clip() -> None:
    assert clip("a" * 3000, 2000) == "a" * 2000
    assert clip("短文本", 2000) == "短文本"


def test_mask_secrets_patterns() -> None:
    text = "key=sk-abcdef123456 and Bearer eyJhbGciOi.abc123 done"
    masked = mask_secrets(text)
    assert "sk-abcdef123456" not in masked
    assert "eyJhbGciOi.abc123" not in masked
    assert "sk-***" in masked and "Bearer ***" in masked


# ----------------------------------------------------------------------
# dispatch 三路径 + 事件 + 留痕
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unknown_tool_rejected_with_trail_and_events(tmp_path: Path) -> None:
    """未注册工具（含注入诱导名）→ TOOL_UNKNOWN + 留痕 + 双事件，不抛异常。"""
    registry, sink, _, trail = _registry(tmp_path)
    ctx = _ctx(tmp_path)
    result = await registry.dispatch(
        ToolCall(id="call_x", name="delete_all_files; rm -rf", arguments_json="{}"), ctx
    )
    assert result.ok is False
    assert result.error is not None and result.error.code == "TOOL_UNKNOWN"
    assert "execute_code" in result.error.message  # 可用工具清单回注

    events = [json.loads(x) for x in sink.getvalue().splitlines() if x]
    assert [e["event"] for e in events] == ["tool.call", "tool.result"]
    assert events[1]["ok"] is False
    records = trail._store.events("t1") if trail else []
    assert any(r.event_type == "tool_call" for r in records)


@pytest.mark.asyncio
async def test_invalid_json_args(tmp_path: Path) -> None:
    registry, _, _, _ = _registry(tmp_path)
    result = await registry.dispatch(
        ToolCall(id="c1", name="execute_code", arguments_json="{not json"), _ctx(tmp_path)
    )
    assert result.error is not None and result.error.code == "TOOL_ARGS_INVALID"


@pytest.mark.asyncio
async def test_args_validation_error_no_input_leak(tmp_path: Path) -> None:
    """校验失败信息只含类型/位置，不回显输入明文（防注入外泄）。"""
    registry, _, _, _ = _registry(tmp_path)
    secret_code = 'import os; os.system("echo secret-value-xyz")'
    result = await registry.dispatch(
        ToolCall(id="c2", name="execute_code",
                 arguments_json=json.dumps({"code": secret_code, "language": 12345})),
        _ctx(tmp_path),
    )
    assert result.error is not None and result.error.code == "TOOL_ARGS_INVALID"
    assert "secret-value-xyz" not in result.error.message


@pytest.mark.asyncio
async def test_executor_exception_converges_to_tool_failed(tmp_path: Path) -> None:
    """执行器异常收敛为 TOOL_FAILED 结构化错误（不炸编排循环）。"""
    registry = ToolRegistry()

    class Boom(BaseModel):
        x: int = 0

    async def boom(args: BaseModel, ctx: ToolContext) -> ToolResult:
        raise RuntimeError("boom-with-sk-abcdef123456")

    registry.register(ToolSpec(name="boom", description="x", args_model=Boom, danger_level="low", executor=boom))
    result = await registry.dispatch(ToolCall(id="c3", name="boom", arguments_json="{}"), _ctx(tmp_path))
    assert result.ok is False
    assert result.error is not None and result.error.code == "TOOL_FAILED"
    assert "sk-abcdef123456" not in result.error.message  # 异常文本也脱敏


# ----------------------------------------------------------------------
# execute_code 集成（真沙箱）
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_execute_code_happy_path(tmp_path: Path) -> None:
    """真沙箱执行：stdout 进摘要、退出码 0 → ok。"""
    registry, _, _, _ = _registry(tmp_path)
    ctx = _ctx(tmp_path)
    ctx.work_dir.mkdir(parents=True, exist_ok=True)
    result = await registry.dispatch(
        ToolCall(id="c4", name="execute_code",
                 arguments_json=json.dumps({"code": "print('slope=2.5')"})),
        ctx,
    )
    assert result.ok is True
    assert "slope=2.5" in result.summary
    assert result.result_ref is None


@pytest.mark.asyncio
async def test_execute_code_failure_structured(tmp_path: Path) -> None:
    """代码抛错 → ok=False + 退出码与错误尾部回注（模型可自修复）。"""
    registry, _, _, _ = _registry(tmp_path)
    ctx = _ctx(tmp_path)
    ctx.work_dir.mkdir(parents=True, exist_ok=True)
    result = await registry.dispatch(
        ToolCall(id="c5", name="execute_code",
                 arguments_json=json.dumps({"code": "raise IndexError('bad index')"})),
        ctx,
    )
    assert result.ok is False
    assert result.error is not None and result.error.code == "TOOL_FAILED"
    assert "IndexError" in result.error.message


@pytest.mark.asyncio
async def test_execute_code_language_guard(tmp_path: Path) -> None:
    """非 python 语言 → TOOL_ARGS_INVALID（不执行）。"""
    registry, _, _, _ = _registry(tmp_path)
    result = await registry.dispatch(
        ToolCall(id="c6", name="execute_code",
                 arguments_json=json.dumps({"code": "echo hi", "language": "bash"})),
        _ctx(tmp_path),
    )
    assert result.error is not None and "bash" in result.error.message


@pytest.mark.asyncio
async def test_execute_code_artifact_path_escape_denied(tmp_path: Path) -> None:
    """产物相对路径越界（../）→ result_ref 不外引（逃逸防护 AT-13）。"""
    from engine.sandbox.base import ExecutionResult

    class EvilSandbox:
        async def execute(self, code: str, files: dict, work_dir: Path) -> ExecutionResult:
            return ExecutionResult(exit_code=0, stdout="done", stderr="", artifacts=["../evil.png"])

    registry, _, _, _ = _registry(tmp_path, sandbox=EvilSandbox())
    result = await registry.dispatch(
        ToolCall(id="c7", name="execute_code", arguments_json=json.dumps({"code": "pass"})),
        _ctx(tmp_path),
    )
    assert result.ok is True
    assert result.result_ref is None


# ----------------------------------------------------------------------
# plot_figure
# ----------------------------------------------------------------------
def test_plot_script_contains_cjk_font_and_agg() -> None:
    """生成脚本：Agg 后端 + 中文字体回退链 + unicode_minus（PRD 已知坑）。"""
    args = PlotFigureArgs(
        title="误差分析", x_label="迭代", y_label="误差",
        series=[SeriesSpec(label="残差", values=[1.0, 2.0])],
    )
    script = build_plot_script(args)
    assert 'matplotlib.use("Agg")' in script
    assert "font.sans-serif" in script and "SimHei" in script
    assert "axes.unicode_minus" in script


@pytest.mark.asyncio
async def test_plot_figure_via_fake_sandbox(tmp_path: Path) -> None:
    """假沙箱捕获脚本并返回产物 → ok + result_ref=figure.png。"""
    from engine.sandbox.base import ExecutionResult

    captured: dict[str, str] = {}

    class FakeSandbox:
        async def execute(self, code: str, files: dict, work_dir: Path) -> ExecutionResult:
            captured["code"] = code
            return ExecutionResult(exit_code=0, stdout="figure.png\n", stderr="", artifacts=["figure.png"])

    registry, _, _, _ = _registry(tmp_path, sandbox=FakeSandbox())
    ctx = _ctx(tmp_path)
    ctx.work_dir.mkdir(parents=True, exist_ok=True)
    result = await registry.dispatch(
        ToolCall(id="c8", name="plot_figure",
                 arguments_json=json.dumps({"title": "结果对比", "series": [{"label": "A", "values": [1, 2]}]})),
        ctx,
    )
    assert result.ok is True
    assert result.result_ref == "figure.png"
    assert "结果对比" in captured["code"]


@pytest.mark.asyncio
async def test_plot_figure_duplicate_labels_rejected(tmp_path: Path) -> None:
    """图例标签重复 → 校验失败回注。"""
    registry, _, _, _ = _registry(tmp_path)
    result = await registry.dispatch(
        ToolCall(id="c9", name="plot_figure",
                 arguments_json=json.dumps({"title": "t", "series": [
                     {"label": "A", "values": [1]}, {"label": "A", "values": [2]}]})),
        _ctx(tmp_path),
    )
    assert result.error is not None and result.error.code == "TOOL_ARGS_INVALID"


# ----------------------------------------------------------------------
# search_references 占位
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_search_references_not_available(tmp_path: Path) -> None:
    """EN-19A 接入前：结构化 NOT_AVAILABLE（不抛异常）。"""
    registry, _, _, _ = _registry(tmp_path)
    result = await registry.dispatch(
        ToolCall(id="c10", name="search_references",
                 arguments_json=json.dumps({"query": "线性规划 灵敏度"})),
        _ctx(tmp_path),
    )
    assert result.ok is False
    assert result.error is not None and result.error.code == "NOT_AVAILABLE"


# ----------------------------------------------------------------------
# 出口脱敏
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_args_summary_masked_in_events_and_trail(tmp_path: Path) -> None:
    """args 含 sk- 形状凭据 → 事件 args_summary 与留痕均被掩码。"""
    registry, sink, _, trail = _registry(tmp_path, with_trail=True)

    class Echo(BaseModel):
        text: str = ""

    async def echo(args: Echo, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, summary=f"echo {args.text}")

    registry.register(ToolSpec(name="echo", description="x", args_model=Echo, danger_level="low", executor=echo))
    result = await registry.dispatch(
        ToolCall(id="c11", name="echo",
                 arguments_json=json.dumps({"text": "my key is sk-abcdef123456"})),
        _ctx(tmp_path),
    )
    assert "sk-abcdef123456" not in result.summary
    events = [json.loads(x) for x in sink.getvalue().splitlines() if x]
    call_event = next(e for e in events if e["event"] == "tool.call")
    assert "sk-abcdef123456" not in call_event["args_summary"]
    records = trail._store.events("t1")
    detail = next(r.detail for r in records if r.event_type == "tool_call")
    assert "sk-abcdef123456" not in json.dumps(detail, ensure_ascii=False)
