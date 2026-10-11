"""求解路径归因进验收证据（G41 的第三个面：证据面——这一面不等跨端裁决）。

G41 原来只记了事件面（`stage.progress` 无 `tool_mode`，客户端看不见降级），那一半要追认；
但**验收证据面同样看不出走的是哪条路径**，而它不需要任何跨端口径：`--driver rpc` 的
`initialize` 响应此前被整块丢掉，逐题证据只有阶段/耗时/token。

后果是具体的：真实 Key 窗口（11-05~11-14）若厂商不支持 tools，整套回归会静默全跑
`stage_level`——成功率照样是 100%，而工具链、沙箱派发、内存强制一次都没被验过。
"通过率"必须能读出它是怎么来的，所以三件事进了证据：

- `solve_path`：这条求解是 `tool_loop` 还是 `stage_level`（引擎两条路径都自述，缺则记 None，
  **不反推**——把"没有字段"读成"走了降级"就是编造归因）；
- `dispatch_count`：工具循环实际派发次数（stage_level 无此概念，记 None 而不是 0）；
- `isolation_mode`：沙箱如实上报的模式（DEC-006 在证据层的落点）。

三件都会自动进 `asdict()` 的证据 JSON 与双轨比对字段（比对视图不挑字段，只剥耗时）。
"""

import json
from typing import Any

import pytest

from engine.orchestrator.pipeline import StagePipeline
from engine.regression.evidence import EvidenceWriter, render_markdown
from engine.regression.flow import FakeLLMFlow
from engine.regression.problems import REGRESSION_SET
from engine.regression.runner import AcceptanceReport, AcceptanceRunner, ProblemResult
from engine.sandbox.base import ExecutionResult

PROBLEM = REGRESSION_SET[0]
STAGES = ("analysis", "modeling", "solving", "writing")


class _StubSandbox:
    isolation_mode = "stub"

    async def execute(self, code: str, files: dict[str, str], work_dir: Any) -> ExecutionResult:
        return ExecutionResult(exit_code=0, stdout="slope=1.0\n", stderr="", artifacts=[])


class _FlowStub:
    """满足 TaskFlow 端口的替身：solving 阶段的产出货由测试给。"""

    def __init__(self, solving_data: dict[str, Any]) -> None:
        self._solving = solving_data
        self._done: list[str] = []

    def current_stage(self) -> str:
        return STAGES[len(self._done)] if len(self._done) < len(STAGES) else "writing"

    async def run_stage(self, stage: str) -> dict[str, Any]:
        self._done.append(stage)
        base = {"stage": stage, "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        if stage == "solving":
            base.update(self._solving)
        if stage == "writing":
            base["paper_sha256"] = "ab" * 32
        return base

    async def answer_gate(self, decision: str) -> dict[str, Any]:
        return {"decision": decision}

    def executed_stages(self) -> tuple[str, ...]:
        return tuple(self._done)

    def hard_reject_reason(self) -> str | None:
        return None

    def close(self, force: bool = False) -> None:
        return None


async def _run_stub(solving_data: dict[str, Any]) -> ProblemResult:
    runner = AcceptanceRunner(lambda problem: _FlowStub(solving_data), channel="fakellm")
    return await runner.run_one(PROBLEM)


def _result_with_path(**overrides: Any) -> ProblemResult:
    defaults: dict[str, Any] = {
        "business_id": PROBLEM.business_id,
        "category": PROBLEM.category,
        "channel": "fakellm",
        "passed": True,
        "failure_module": None,
        "failure_detail": "",
        "stages_passed": 4,
        "stage_records": (),
        "prompt_tokens": 40,
        "completion_tokens": 20,
        "paper_sha256": None,
        "resumed": False,
        "kill_after_stage": None,
        "executed_stages": STAGES,
    }
    defaults.update(overrides)
    return ProblemResult(**defaults)


# ----------------------------------------------------------------------
# 引擎侧：两条路径都必须自述，读取方不靠"缺字段"反推
# ----------------------------------------------------------------------
async def test_stage_level_solving_self_describes_path_and_isolation(tmp_path) -> None:
    pipeline = StagePipeline(
        sandbox=_StubSandbox(), work_root=tmp_path, tool_mode="stage_level", registry=None
    )
    data = await pipeline.process("t-attr", "solving")

    assert data["mode"] == "stage_level"
    assert data["isolation_mode"] == "stub"


# ----------------------------------------------------------------------
# 运行器：归因三件进结果
# ----------------------------------------------------------------------
async def test_runner_carries_tool_loop_path_and_dispatch_count() -> None:
    result = await _run_stub(
        {"mode": "tool_loop", "isolation_mode": "docker", "dispatch_count": 3}
    )
    assert result.solve_path == "tool_loop"
    assert result.isolation_mode == "docker"
    assert result.dispatch_count == 3


async def test_runner_records_none_rather_than_guessing_the_path() -> None:
    """正向对照/反证：老检查点或第三方实现没有 mode 时，记 None 而不是"当成降级"。

    把缺字段读成 stage_level 会让"工具链从未被跑过"这件事在报告里看起来像"跑过且都通过"。
    """
    result = await _run_stub({"usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    assert result.solve_path is None
    assert result.isolation_mode is None
    assert result.dispatch_count is None


async def test_stage_level_path_has_no_dispatch_count_but_reports_zero_for_tool_loop() -> None:
    loop_zero = await _run_stub({"mode": "tool_loop", "isolation_mode": "subprocess",
                                 "dispatch_count": 0})
    degraded = await _run_stub({"mode": "stage_level", "isolation_mode": "subprocess"})
    assert loop_zero.dispatch_count == 0, "派发 0 次是事实，不能被 None 吞掉"
    assert degraded.dispatch_count is None, "降级路径没有派发概念"
    assert (loop_zero.solve_path, degraded.solve_path) == ("tool_loop", "stage_level")


async def test_real_inproc_flow_reports_the_degraded_path_honestly(tmp_path) -> None:
    """端到端：进程内 FakeLLM 流（未注入工具循环）必须落 stage_level + 替身沙箱模式。"""
    sandbox = _StubSandbox()

    def factory(problem) -> FakeLLMFlow:  # noqa: ANN001 - FlowFactory 形参
        return FakeLLMFlow(
            task_id=f"reg-{problem.business_id}",
            title=problem.title,
            problem_text=problem.statement,
            sandbox=sandbox,
            work_root=tmp_path / "work",
            checkpoint_path=tmp_path / "ckpt.db",
        )

    result = await AcceptanceRunner(factory, channel="fakellm").run_one(PROBLEM)

    assert result.passed, result.failure_detail
    assert result.solve_path == "stage_level"
    assert result.isolation_mode == "stub"
    assert result.dispatch_count is None


# ----------------------------------------------------------------------
# 证据面：JSON 与 report.md 都要看得见
# ----------------------------------------------------------------------
async def test_evidence_json_and_report_carry_the_attribution(tmp_path) -> None:
    result = await _run_stub(
        {"mode": "tool_loop", "isolation_mode": "subprocess", "dispatch_count": 2}
    )

    writer = EvidenceWriter(tmp_path / "evidence")
    path = writer.write_result(PROBLEM, result)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["result"]["solve_path"] == "tool_loop"
    assert payload["result"]["isolation_mode"] == "subprocess"
    assert payload["result"]["dispatch_count"] == 2

    md = render_markdown(AcceptanceReport(results=[result]), title="测试报告")
    assert "求解路径 tool_loop" in md, md
    assert "tool_loop·派发2·隔离:subprocess" in md, md


async def test_report_prints_unknown_instead_of_dropping_the_row(tmp_path) -> None:
    """路径缺失时报告必须写"未知"，不能整格消失——空白会被读成"没这项要求"。"""
    result = await _run_stub({"usage": {}})

    md = render_markdown(AcceptanceReport(results=[result]), title="测试报告")
    assert "求解路径 未知 1" in md, md
    assert "隔离:未知" in md, md


@pytest.mark.parametrize(
    ("solving_data", "expected"),
    [
        ({"mode": "tool_loop", "dispatch_count": 1}, "tool_loop"),
        ({"mode": "stage_level"}, "stage_level"),
        ({}, None),
    ],
    ids=["tool_loop", "stage_level", "absent"],
)
async def test_path_column_never_invents_a_value(solving_data: dict, expected) -> None:
    result = await _run_stub({"usage": {}, **solving_data})
    assert result.solve_path == expected
