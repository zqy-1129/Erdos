"""阶段级求解也走 Registry（W23 · EN-TRAIL-COMPLETE 第二批 (a)，对齐报告 §8.6 登记的前置）。

§8.6 的原话是"阶段级求解走 Registry 会把求解 stdout 截到 2000 字符＝判定通道行为变化"。
这句话把两件事捆在一起了，本文件只解开该解的那半：

- **留痕/事件面（缺陷，已修）**：降级通道确实跑了代码，但审计里 `tool_call` 一条也没有——
  复核的人读到的是"这题一次工具都没用"。修法是让它也经 `registry.dispatch`，与模型发起的
  调用共用同一条实现；**不在编排层另写一份留痕**（一句约定盖两种库）。
- **模型可见面（不动）**：复核提示词与 writing 引用的是 `ToolResult.data`（未截断的
  `ExecutionResult.facts()`），不是给模型看的 `summary`（脱敏 + 截到 2000）。
  放宽那个上限才会真的改判定通道，所以这里把"两份视图各是各的"钉成断言。

另外钉三条容易被"顺手重构"掉的边界：有无 Registry 的阶段产出一字不差（行为保持的可执行
定义）、分发拿不到执行事实时必须报错而不是拿空 stdout 继续"复核"、超时与非零退出的归因不丢。
"""

import asyncio
import io
import json
from pathlib import Path
from typing import Any

import pytest

from engine.ipc.events import EventEmitter
from engine.orchestrator.pipeline import _SOLVE_SCRIPT, StagePipeline
from engine.sandbox.base import ExecutionResult
from engine.tools import build_default_registry
from engine.tools.base import SyntheticToolCall, ToolContext
from engine.tools.code_tools import ExecuteCodeArgs
from engine.tools.registry import SUMMARY_LIMIT, ToolRegistry
from engine.trail.recorder import TrailRecorder
from engine.trail.store import EventType, TrailStore

TASK_ID = "t-solve"
SHORT_STDOUT = "slope=1.9925\nintercept=2.0950"
_UNSET = object()


def _run(coro: Any) -> Any:
    """本仓不起 pytest-asyncio 事件循环插件，测试里直接跑（与既有测试文件同形）。"""
    return asyncio.run(coro)


class _StubSandbox:
    """沙箱替身：按预设回吐一份 ExecutionResult，并记录被真正调用了几次。"""

    isolation_mode = "stub"

    def __init__(self, result: ExecutionResult) -> None:
        self._result = result
        self.calls = 0

    async def execute(self, code: str, files: dict[str, str], work_dir: Path) -> ExecutionResult:
        self.calls += 1
        return self._result


class _RecordingLLM:
    """把每次收到的提示词记下来——"模型看见了什么"只能用这种方式断言。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def chat(
        self, messages: list[dict[str, str]], stage: str, on_delta: Any = None
    ) -> dict[str, Any]:
        self.prompts.append(" ".join(m.get("content", "") for m in messages))
        return {
            "content": f"复核通过（{stage}）",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "estimated": True},
            "model": "recording",
            "stage": stage,
        }


def _ok(stdout: str = SHORT_STDOUT) -> ExecutionResult:
    return ExecutionResult(exit_code=0, stdout=stdout, stderr="", artifacts=[])


class _Harness:
    """一套完整装配：替身沙箱 + 事件出口 + 真留痕库 + 阶段级管线。"""

    def __init__(self, home: Path, result: ExecutionResult | None = None) -> None:
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.sandbox = _StubSandbox(result or _ok())
        self.sink = io.StringIO()
        self.events = EventEmitter(sink=self.sink)
        self.trail = TrailRecorder(TrailStore(str(home / "audit.db")))
        self.registry = build_default_registry(self.sandbox, events=self.events, trail=self.trail)
        self.llm = _RecordingLLM()

    def pipeline(self, *, registry: Any = _UNSET) -> StagePipeline:
        """默认用装配自带的完整 Registry；显式传 `None`＝"没注入"，传别的＝换一套 allowlist。"""
        return StagePipeline(
            llm=self.llm.chat, sandbox=self.sandbox, work_root=self.home,
            registry=self.registry if registry is _UNSET else registry,
            tool_mode="stage_level",
        )

    def emitted(self) -> list[dict]:
        return [json.loads(line) for line in self.sink.getvalue().splitlines() if line.strip()]

    def tool_call_rows(self, task_id: str = TASK_ID) -> list[dict]:
        store = TrailStore(str(self.home / "audit.db"))
        return [e.detail for e in store.events(task_id) if e.event_type == EventType.TOOL_CALL]


# ----------------------------------------------------------------------
# 留痕与事件面：降级通道跑过代码，审计里就得看得见
# ----------------------------------------------------------------------
def test_stage_level_solving_leaves_one_tool_call_trail(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    data = _run(h.pipeline().process(TASK_ID, "solving"))

    rows = h.tool_call_rows()
    assert len(rows) == 1, f"应恰留一条 tool_call（solving 的那次沙箱执行），实际 {len(rows)}"
    assert rows[0]["tool"] == "execute_code"
    assert rows[0]["ok"] is True
    assert rows[0]["error"] is None
    assert str(rows[0]["call_id"]).startswith(f"stage-{TASK_ID}-")
    assert data["stdout"] == SHORT_STDOUT
    assert h.sandbox.calls == 1


def test_dispatch_emits_tool_call_and_tool_result(tmp_path: Path) -> None:
    """事件面与模型发起的调用同形：客户端工具时间线在降级通道上不再是空的。"""
    h = _Harness(tmp_path)
    _run(h.pipeline().process(TASK_ID, "solving"))

    tool_events = [e for e in h.emitted() if e["event"].startswith("tool.")]
    assert [e["event"] for e in tool_events] == ["tool.call", "tool.result"]
    assert {e["tool"] for e in tool_events} == {"execute_code"}
    assert tool_events[0]["call_id"] == tool_events[1]["call_id"]
    assert tool_events[0]["stage"] == "solving"
    assert tool_events[1]["ok"] is True


# ----------------------------------------------------------------------
# 模型可见面：一个字都不改——这才是那条前置真正在担心的东西
# ----------------------------------------------------------------------
def test_review_prompt_keeps_full_stdout_beyond_the_model_limit(tmp_path: Path) -> None:
    long_stdout = "迭代收敛记录\n" + ("x=0.5 resid=1e-9\n" * 4000)  # ≫ SUMMARY_LIMIT
    assert len(long_stdout) > SUMMARY_LIMIT
    h = _Harness(tmp_path, _ok(long_stdout))

    data = _run(h.pipeline().process(TASK_ID, "solving"))

    assert long_stdout in h.llm.prompts[0], "复核提示词被截断＝判定通道已被顺手改动"
    assert data["stdout"] == long_stdout.strip()


def test_two_views_of_one_execution_stay_deliberately_different(tmp_path: Path) -> None:
    """正向对照：summary（模型视图）截到上限，data（引擎视图）不截。

    没有这条，上一条的"提示词里是全文"可能只是因为整个出口都不截断了——那才是真事故。
    """
    long_stdout = "y" * (SUMMARY_LIMIT + 500)
    h = _Harness(tmp_path, _ok(long_stdout))
    call = SyntheticToolCall(
        id="c1", name="execute_code",
        arguments_json=json.dumps({"code": "print(1)", "language": "python"}),
    )

    result = _run(h.registry.dispatch(call, ToolContext(
        task_id=TASK_ID, stage="solving", work_dir=tmp_path
    )))

    assert len(result.summary) == SUMMARY_LIMIT
    assert result.data["stdout"] == long_stdout


def test_stage_data_is_identical_with_and_without_registry(tmp_path: Path) -> None:
    """行为保持的可执行定义：换不换 Registry，阶段产出逐字段相等。"""
    plain = _Harness(tmp_path / "a")
    wired = _Harness(tmp_path / "b")

    left = _run(plain.pipeline(registry=None).process(TASK_ID, "solving"))
    right = _run(wired.pipeline().process(TASK_ID, "solving"))

    assert right == left
    assert wired.sandbox.calls == 1, "走了 Registry 却没真执行代码＝留痕在造假"


# ----------------------------------------------------------------------
# 边界：拿不到执行事实时必须失败，而不是拿空输出继续往下走
# ----------------------------------------------------------------------
def test_dispatch_without_execution_facts_fails_loud(tmp_path: Path) -> None:
    """空 allowlist（execute_code 未注册）→ 报错，绝不"stdout 为空但阶段成功"。"""
    h = _Harness(tmp_path)
    empty = ToolRegistry(events=EventEmitter(sink=io.StringIO()))

    with pytest.raises(RuntimeError, match="阶段级求解未经沙箱执行"):
        _run(h.pipeline(registry=empty).process(TASK_ID, "solving"))
    assert h.llm.prompts == [], "没跑代码却去'复核求解结果'＝让模型复核不存在的东西"
    assert h.sandbox.calls == 0


@pytest.mark.parametrize(
    ("exit_code", "result"),
    [
        (-1, ExecutionResult(exit_code=-1, stdout="部分输出", stderr="", timed_out=True)),
        (1, ExecutionResult(exit_code=1, stdout="半程结果", stderr="ZeroDivisionError")),
    ],
    ids=["timed_out", "nonzero_exit"],
)
def test_failure_attributions_survive_dispatch(
    tmp_path: Path, exit_code: int, result: ExecutionResult
) -> None:
    """归因不丢：超时与非零退出仍是阶段 data 里那两个字段（writing 与门禁都读它）。"""
    h = _Harness(tmp_path, result)
    task_id = f"t-{exit_code}"

    data = _run(h.pipeline().process(task_id, "solving"))

    assert data["stage"] == "solving"
    assert data["exit_code"] == exit_code
    assert data["timed_out"] is (exit_code == -1)
    assert h.tool_call_rows(task_id)[0]["ok"] is False, "失败也必须留痕，否则审计只记得成功的那几次"


def test_synthetic_call_and_model_call_share_one_port(tmp_path: Path) -> None:
    """`SyntheticToolCall` 与适配器 `ToolCall` 走同一条 dispatch——不是第二套调用面。

    端口契合由 mypy 保证（`dispatch` 的形参类型就是 `ToolCallLike`），所以这里验的是
    **行为同形**：同样的参数、同样的执行、同样的留痕形状，只是发起方一个是模型一个是引擎。
    """
    from engine.adapters.openai_compat import ToolCall

    h = _Harness(tmp_path)
    arguments = json.dumps({"code": "print(1)", "language": "python"})
    model_side = ToolCall(id="m1", name="execute_code", arguments_json=arguments)
    engine_side = SyntheticToolCall(id="s1", name="execute_code", arguments_json=arguments)

    results = [
        _run(h.registry.dispatch(call, ToolContext(
            task_id=TASK_ID, stage="solving", work_dir=tmp_path
        )))
        for call in (model_side, engine_side)
    ]
    assert [r.ok for r in results] == [True, True]
    assert [r.data["stdout"] for r in results] == [SHORT_STDOUT, SHORT_STDOUT]
    assert [r.duration_ms >= 0 for r in results] == [True, True]


def test_built_in_solve_script_fits_the_tool_argument_budget() -> None:
    """阶段级脚本必须仍在 `execute_code` 的参数上限内，否则降级通道天天走报错分支。

    上限属工具参数校验（`ExecuteCodeArgs`），不是判定通道——它一旦小于内置脚本，
    阶段级求解会整条变成 TOOL_ARGS_INVALID，是"改工具面顺手打死降级路径"的形状。
    用模型自身校验，不去翻字段元数据：约束在 pydantic 里挂在 metadata 上，把它的属性名
    写进守护等于依赖一个会随版本漂移的内部结构。
    """
    from pydantic import ValidationError

    try:
        args = ExecuteCodeArgs(code=_SOLVE_SCRIPT, language="python")
    except ValidationError as exc:
        pytest.fail(f"内置求解脚本超出 execute_code 参数上限，阶段级路径会整条报错：{exc}")
    assert args.language == "python"


# ----------------------------------------------------------------------
# 新出口的边界：data 只是引擎内部视图，合成调用只带内置脚本
# ----------------------------------------------------------------------
def test_untruncated_facts_do_not_leak_into_outbound_channels(tmp_path: Path) -> None:
    """`ToolResult.data` 一旦出现在事件或留痕里，就多了第三条**未脱敏未截断**的出口。

    注释里的承诺不算守护，所以这里用探针实测：全文必须只在模型提示词里出现。
    """
    marker = "PROBE-" + "q" * 300
    long_stdout = f"{marker}\n" + ("x=0.5 resid=1e-9\n" * 4000)
    h = _Harness(tmp_path, _ok(long_stdout))

    _run(h.pipeline().process(TASK_ID, "solving"))

    assert len(long_stdout) > SUMMARY_LIMIT
    assert marker in h.llm.prompts[0], "探针失效：全文没进复核提示词，下面的断言就是恒真"
    assert marker not in h.sink.getvalue(), "未截断 stdout 出现在 NDJSON 事件流"
    assert marker not in json.dumps(h.tool_call_rows(), ensure_ascii=False), (
        "未截断 stdout 出现在留痕明细"
    )


def test_problem_text_stays_out_of_the_tool_surface(tmp_path: Path) -> None:
    """合成调用的参数只含内置脚本：题面不得进事件与留痕（DEC-010 在工具面的落点）。

    "顺手把上游上下文也带进工具参数"是最容易长出来的形状，而 args_summary 会同时进
    事件流与留痕——题面一旦进去就出了本机进程的语义边界。
    """
    marker = "本题要求最小化总运输成本（题面外泄探针）"
    h = _Harness(tmp_path)
    pipeline = StagePipeline(
        llm=h.llm.chat, sandbox=h.sandbox, work_root=h.home, registry=h.registry,
        tool_mode="stage_level", task_inputs={TASK_ID: {"problem_text": marker, "title": "探针题"}},
    )

    _run(pipeline.process(TASK_ID, "solving"))

    assert marker in h.llm.prompts[0], "探针失效：题面没进提示词，本用例恒真"
    assert marker not in h.sink.getvalue(), "题面进了 NDJSON 事件流"
    assert marker not in json.dumps(h.tool_call_rows(), ensure_ascii=False), "题面进了留痕明细"
