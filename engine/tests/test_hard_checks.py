"""门禁硬检查（SP1-3）单测：阶段产出的代码级判定，逐阶段正/反用例。

覆盖：
- 四阶段各自合规产出 → 无违规（含 StagePipeline 真实产出形状，离线确定性 FakeLLM）；
- 关键回归：求解阶段级执行 exit_code≠0 + stdout 空 + artifacts 空（历史工作目录缺陷形态）必须拦住；
- 超时、工具循环未成功、结果含 NaN、报告缺章节/哈希非法/产物缺失、未知阶段。
"""

from pathlib import Path

from engine.gates.hard_checks import check_stage
from engine.orchestrator.pipeline import FakeLLM, StagePipeline
from engine.sandbox.subprocess_sandbox import SubprocessSandbox

MAX_REASON_LEN = 120


def _analysis(**over) -> dict:
    data = {
        "stage": "analysis",
        "insights": ["决策变量与目标", "约束条件（资源/边界）"],
        "question_focused": True,
        "model": "fake-llm/deterministic",
        "usage": {"prompt_tokens": 30, "completion_tokens": 12},
        "duration_ms": 8.5,
    }
    data.update(over)
    return data


def _modeling(**over) -> dict:
    data = {
        "stage": "modeling",
        "assumptions": "模型假设：变量独立、线性关系近似成立",
        "objective": "目标函数：min f(x)=Σwᵢ·xᵢ",
        "modeling_detail": "目标函数与约束说明，xᵢ≥0 且资源总量受限。",
        "variables": ["slope", "intercept"],
        "model": "fake-llm/deterministic",
        "usage": {"prompt_tokens": 40, "completion_tokens": 20},
        "duration_ms": 9.0,
    }
    data.update(over)
    return data


def _solving_stage_level(**over) -> dict:
    data = {
        "stage": "solving",
        "exit_code": 0,
        "stdout": "slope=1.9850\nintercept=2.1150",
        "timed_out": False,
        "artifacts": [],
        "model": "fake-llm/deterministic",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "duration_ms": 700.0,
    }
    data.update(over)
    return data


def _solving_tool_loop(**over) -> dict:
    data = {
        "stage": "solving",
        "mode": "tool_loop",
        "status": "succeeded",
        "results": [{"name": "slope", "value": 1.985, "unit": "元/件"}],
        "repair_count": 1,
        "dispatch_count": 2,
        "limitations": [],
        "model": "unknown",
        "usage": {"prompt_tokens": 120, "completion_tokens": 60},
    }
    data.update(over)
    return data


def _writing(paper_path: Path, **over) -> dict:
    paper = (
        "# 生产计划优化\n\n## 摘要\n\n方法与结果。\n\n## 一、问题重述\n\n题面。\n\n"
        "## 二、模型假设与建模\n\n假设。\n\n## 三、求解与结果\n\n```\nslope=1.9850\n```\n\n"
        "## 四、结论\n\n结论。\n\n## 五、结果局限\n\n- 无\n"
    )
    paper_path.write_text(paper, encoding="utf-8")
    data = {
        "stage": "writing",
        "paper_md": paper,
        "paper_sha256": "0" * 64,
        "paper_path": str(paper_path),
        "model": "fake-llm/deterministic",
        "usage": {"prompt_tokens": 50, "completion_tokens": 25},
        "duration_ms": 11.0,
    }
    data.update(over)
    return data


def _joined(violations: list[str]) -> str:
    return "；".join(violations)


# ----------------------------------------------------------------------
# analysis
# ----------------------------------------------------------------------
def test_analysis_positive() -> None:
    assert check_stage("analysis", _analysis()) == []


def test_analysis_rejects_empty_insights() -> None:
    assert check_stage("analysis", _analysis(insights=[]))
    assert check_stage("analysis", _analysis(insights="决策变量与目标"))
    assert check_stage("analysis", {"stage": "analysis"})


def test_analysis_rejects_blank_insights() -> None:
    violations = check_stage("analysis", _analysis(insights=["   ", "", "\n"]))
    assert violations and "insights" in _joined(violations)


# ----------------------------------------------------------------------
# modeling
# ----------------------------------------------------------------------
def test_modeling_positive() -> None:
    assert check_stage("modeling", _modeling()) == []


def test_modeling_rejects_blank_assumptions_and_detail() -> None:
    violations = check_stage("modeling", _modeling(assumptions="  ", modeling_detail=""))
    assert "assumptions" in _joined(violations)
    assert "modeling_detail" in _joined(violations)


def test_modeling_rejects_empty_variables() -> None:
    violations = check_stage("modeling", _modeling(variables=[]))
    assert violations and "variables" in _joined(violations)


# ----------------------------------------------------------------------
# solving：阶段级执行路径
# ----------------------------------------------------------------------
def test_solving_stage_level_positive() -> None:
    assert check_stage("solving", _solving_stage_level()) == []


def test_solving_stage_level_positive_with_artifacts_only() -> None:
    """stdout 空但有产物文件同样合规（规则只禁"两者同时为空"）。"""
    assert check_stage("solving", _solving_stage_level(stdout="   ", artifacts=["out.csv"])) == []


def test_solving_stage_level_rejects_silent_failure() -> None:
    """关键回归：子进程 can't open file（exit_code=2、stdout 空、无产物）不得被当成成功。"""
    violations = check_stage("solving", _solving_stage_level(exit_code=2, stdout="", artifacts=[]))
    reason = _joined(violations)
    assert violations
    assert "exit_code=2" in reason
    assert "退出码" in reason


def test_solving_stage_level_rejects_timeout() -> None:
    violations = check_stage("solving", _solving_stage_level(timed_out=True, stdout=""))
    assert violations and "超时" in _joined(violations)


def test_solving_stage_level_rejects_missing_output_and_artifacts() -> None:
    violations = check_stage("solving", _solving_stage_level(stdout="", artifacts=[]))
    assert violations and "artifacts" in _joined(violations)


# ----------------------------------------------------------------------
# solving：工具循环路径
# ----------------------------------------------------------------------
def test_solving_tool_loop_positive() -> None:
    assert check_stage("solving", _solving_tool_loop()) == []


def test_solving_tool_loop_rejects_awaiting_input() -> None:
    violations = check_stage("solving", _solving_tool_loop(status="awaiting_input"))
    assert violations and "awaiting_input" in _joined(violations)


def test_solving_tool_loop_rejects_failed() -> None:
    assert check_stage("solving", _solving_tool_loop(status="failed"))


def test_solving_tool_loop_rejects_empty_results() -> None:
    violations = check_stage("solving", _solving_tool_loop(results=[]))
    assert violations and "results" in _joined(violations)


def test_solving_tool_loop_rejects_non_finite_values() -> None:
    assert "NaN" in _joined(
        check_stage("solving", _solving_tool_loop(results=[{"name": "slope", "value": float("nan")}]))
    )
    assert check_stage(
        "solving", _solving_tool_loop(results=[{"name": "slope", "value": float("inf")}])
    )
    assert check_stage(
        "solving", _solving_tool_loop(results=[{"name": "slope", "value": float("-inf")}])
    )


def test_solving_tool_loop_accepts_non_numeric_values() -> None:
    """value 非数值类型（字符串/布尔）放行：硬检查只拒 NaN/±Infinity。"""
    assert check_stage(
        "solving",
        _solving_tool_loop(results=[{"name": "最优方案", "value": "A=10, B=20"}, {"name": "可行", "value": True}]),
    ) == []


def test_solving_tool_loop_ignores_counters() -> None:
    """repair_count/dispatch_count 只要求为整数，不判大小。"""
    assert check_stage("solving", _solving_tool_loop(repair_count=99, dispatch_count=500)) == []
    assert check_stage("solving", _solving_tool_loop(repair_count=None))


# ----------------------------------------------------------------------
# writing
# ----------------------------------------------------------------------
def test_writing_positive(tmp_path: Path) -> None:
    from hashlib import sha256

    paper_path = tmp_path / "paper.md"
    data = _writing(paper_path)
    data["paper_sha256"] = sha256(data["paper_md"].encode("utf-8")).hexdigest()
    assert check_stage("writing", data) == []


def test_writing_rejects_missing_sections(tmp_path: Path) -> None:
    data = _writing(tmp_path / "paper.md")
    data["paper_md"] = data["paper_md"].replace("## 三、求解与结果", "## 三、结果")
    violations = check_stage("writing", data)
    assert violations and "章节" in _joined(violations)
    assert "## 三、求解与结果" in _joined(violations)


def test_writing_rejects_empty_paper(tmp_path: Path) -> None:
    assert check_stage("writing", _writing(tmp_path / "paper.md", paper_md="   "))


def test_writing_rejects_bad_sha256(tmp_path: Path) -> None:
    assert "paper_sha256" in _joined(check_stage("writing", _writing(tmp_path / "a.md", paper_sha256="f" * 63)))
    assert "paper_sha256" in _joined(check_stage("writing", _writing(tmp_path / "b.md", paper_sha256="A" * 64)))
    assert check_stage("writing", _writing(tmp_path / "c.md", paper_sha256=""))


def test_writing_rejects_missing_artifact_file(tmp_path: Path) -> None:
    data = _writing(tmp_path / "paper.md")
    data["paper_path"] = str(tmp_path / "gone.md")
    violations = check_stage("writing", data)
    assert violations and "paper_path" in _joined(violations)


def test_writing_rejects_absent_paper_path(tmp_path: Path) -> None:
    data = _writing(tmp_path / "paper.md")
    data.pop("paper_path")
    violations = check_stage("writing", data)
    assert "paper_path" in _joined(violations)


# ----------------------------------------------------------------------
# 未知阶段 / 说明文字约束
# ----------------------------------------------------------------------
def test_unknown_stage_is_not_silently_passed() -> None:
    violations = check_stage("review", _analysis())
    assert violations and "review" in _joined(violations)


def test_violation_messages_are_short_and_stack_free(tmp_path: Path) -> None:
    """说明文字直接进 gate.failed.reason：≤120 字、状态+原因式、无堆栈。"""
    lost_paper = _writing(tmp_path / "paper.md")
    lost_paper["paper_path"] = str(tmp_path / "gone.md")
    cases = [
        check_stage("analysis", _analysis(insights=[])),
        check_stage("modeling", _modeling(variables=[])),
        check_stage("solving", _solving_stage_level(exit_code=2, stdout="", artifacts=[])),
        check_stage("solving", _solving_tool_loop(status="awaiting_input")),
        check_stage("writing", lost_paper),
        check_stage("bogus", {}),
    ]
    for violations in cases:
        assert violations
        for message in violations:
            assert len(message) <= MAX_REASON_LEN, message
            assert "Traceback" not in message


# ----------------------------------------------------------------------
# 真实产出形状：StagePipeline（FakeLLM + 沙箱）四阶段全部合规
# ----------------------------------------------------------------------
async def test_pipeline_real_stage_outputs_pass_hard_checks(tmp_path: Path) -> None:
    pipeline = StagePipeline(
        llm=FakeLLM().chat,
        sandbox=SubprocessSandbox(timeout=30),
        work_root=tmp_path / "work",
        task_inputs={"t-hard": {"title": "硬检查", "problem_text": "最小二乘拟合演示题面"}},
    )
    for stage in ("analysis", "modeling", "solving", "writing"):
        data = await pipeline.process("t-hard", stage)
        assert check_stage(stage, data) == [], (stage, check_stage(stage, data))
