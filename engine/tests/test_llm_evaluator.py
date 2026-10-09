"""rubric LLM 评委生产实现单测（SP1-3/SP1-5）：prompt 单源、有界投喂、失败上抛、用量如实。"""

import json
from pathlib import Path

from engine.adapters.errors import AdapterError, ErrorKind
from engine.gates.evaluator import GateRunner, _parse_score
from engine.gates.llm_evaluator import (
    MAX_ARTIFACT_CHARS,
    LlmRubricEvaluator,
    extract_json_object,
)
from engine.gates.schema import Rubric, load_rubric_for_stage

RUBRICS_DIR = Path(__file__).resolve().parent.parent / "gates" / "rubrics"


class RecordingLLM:
    """评委模型端口替身：记录每次入参，可注入固定应答或异常。"""

    def __init__(self, content: str | None = None, *, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._content = content or json.dumps({"scores": []}, ensure_ascii=False)
        self._error = error

    async def __call__(self, messages: list[dict[str, str]], stage: str) -> dict:
        self.calls.append({"messages": messages, "stage": stage})
        if self._error is not None:
            raise self._error
        return {
            "content": self._content,
            "usage": {"prompt_tokens": 1200, "completion_tokens": 80},
            "model": "judge-model",
        }


def _rubric(stage: str = "analysis") -> Rubric:
    return load_rubric_for_stage(stage, RUBRICS_DIR)


def _artifact() -> dict:
    return {
        "stage": "analysis",
        "insights": ["决策变量为 8 台机床的加工顺序", "目标是最小化总加工时间"],
        "problem_text": "某生产线由 8 台 CNC 机床与 1 台 RGV 组成，求最优调度策略。",
        "title": "智能RGV的动态调度策略",
        "usage": {"prompt_tokens": 900, "completion_tokens": 60},
        "model": "task-model",
        "duration_ms": 4200.0,
        "paper_path": "/tmp/tasks/t1/paper.md",
    }


def _scores(names: list[str], value: float) -> str:
    return json.dumps(
        {"scores": [{"dimension": n, "score": value, "reason": "说明"} for n in names]},
        ensure_ascii=False,
    )


def _passing_output(rubric: Rubric) -> str:
    return _scores([d.name for d in rubric.dimensions], 0.9)


async def _prompt_for(
    llm: RecordingLLM, stage: str = "analysis", artifact: dict | None = None,
    rubric: Rubric | None = None, **kwargs: object,
) -> str:
    """跑一次评委调用并返回投喂给模型的 user prompt（断言 prompt 内容的统一入口）。"""
    await LlmRubricEvaluator(llm, **kwargs).evaluate(  # type: ignore[arg-type]
        stage, artifact if artifact is not None else _artifact(), rubric or _rubric(stage)
    )
    return next(m["content"] for m in llm.calls[-1]["messages"] if m["role"] == "user")


# ----------------------------------------------------------------------
# prompt 单源来自 rubric（改 rubric 即改评委口径，评分规则不在代码里硬编码）
# ----------------------------------------------------------------------
async def test_prompt_carries_every_rubric_dimension_weight_and_threshold() -> None:
    rubric = _rubric()
    prompt = await _prompt_for(RecordingLLM(), rubric=rubric)
    for dimension in rubric.dimensions:
        assert dimension.name in prompt
        assert dimension.description in prompt
        assert f"{dimension.weight:.2f}" in prompt
    assert f"{rubric.threshold:.2f}" in prompt
    assert f"v{rubric.version}" in prompt


async def test_prompt_requires_unit_scale_and_exact_dimension_names() -> None:
    prompt = await _prompt_for(RecordingLLM())
    assert "0~1" in prompt
    assert "维度名必须逐字一致" in prompt


async def test_prompt_includes_problem_statement_and_stage_artifact() -> None:
    prompt = await _prompt_for(RecordingLLM())
    assert "智能RGV的动态调度策略" in prompt
    assert "决策变量为 8 台机床的加工顺序" in prompt


async def test_prompt_excludes_non_review_noise_fields() -> None:
    """usage/model/耗时/产物路径不是评审对象，进 prompt 只会挤占上下文。"""
    prompt = await _prompt_for(RecordingLLM())
    for noise in ("duration_ms", "paper_path", "prompt_tokens", "task-model"):
        assert noise not in prompt


async def test_prompt_truncates_long_artifact_and_declares_it() -> None:
    artifact = {
        "stage": "writing",
        "paper_md": "摘要内容" * 5000,
        "problem_text": "题面" * 100,
    }
    prompt = await _prompt_for(
        RecordingLLM(), stage="writing", artifact=artifact, rubric=_rubric("writing"),
        max_artifact_chars=600,
    )
    assert "已截断" in prompt
    assert len(prompt) < 4000


async def test_default_artifact_budget_is_bounded() -> None:
    artifact = {"stage": "writing", "paper_md": "长" * (MAX_ARTIFACT_CHARS * 3)}
    prompt = await _prompt_for(
        RecordingLLM(), stage="writing", artifact=artifact, rubric=_rubric("writing")
    )
    assert len(prompt) < MAX_ARTIFACT_CHARS * 2


async def test_stage_without_field_whitelist_falls_back_to_non_noise_fields() -> None:
    """阶段若无字段白名单，退化为剔除噪声字段后全量投喂，不能只给空产物让评委盲判。"""
    artifact = {"stage": "analysis", "custom_field": "自定义产出内容"}
    prompt = await _prompt_for(RecordingLLM(), artifact=artifact)
    assert "自定义产出内容" in prompt


# ----------------------------------------------------------------------
# 应答处理：评分文本原样返回、剥壳不伪装、调用失败上抛不降级
# ----------------------------------------------------------------------
async def test_evaluate_returns_score_json_verbatim() -> None:
    rubric = _rubric()
    raw = _passing_output(rubric)
    out = await LlmRubricEvaluator(RecordingLLM(content=raw)).evaluate("analysis", _artifact(), rubric)
    assert out == raw


async def test_evaluate_strips_code_fence_before_returning() -> None:
    fenced = f"```json\n{_passing_output(_rubric())}\n```"
    out = await LlmRubricEvaluator(RecordingLLM(content=fenced)).evaluate(
        "analysis", _artifact(), _rubric()
    )
    assert json.loads(out)["scores"]


async def test_prose_reply_flows_to_gate_and_fails_closed() -> None:
    """红线：解析失败即不通过——评委层不替模型编造 JSON。"""
    prose = "这份产物整体不错，我没有按格式打分。"
    out = await LlmRubricEvaluator(RecordingLLM(content=prose)).evaluate(
        "analysis", _artifact(), _rubric()
    )
    assert out == prose
    assert _parse_score("analysis", out, _rubric()).passed is False


async def test_evaluate_reraises_adapter_error_instead_of_faking_low_score() -> None:
    """适配器故障必须上抛：伪装成评分不通过会把 infra 故障归因成质量问题（SP1-7 归因失真）。"""
    evaluator = LlmRubricEvaluator(RecordingLLM(error=AdapterError(ErrorKind.NETWORK, "连接超时")))
    try:
        await evaluator.evaluate("analysis", _artifact(), _rubric())
    except AdapterError:
        return
    raise AssertionError("模型调用失败不得被吞成评分结果")


async def test_model_reply_without_content_raises() -> None:
    class NoContentLLM:
        async def __call__(self, messages: list[dict[str, str]], stage: str) -> dict:
            return {"usage": {}, "model": "m"}

    try:
        await LlmRubricEvaluator(NoContentLLM()).evaluate("analysis", _artifact(), _rubric())
    except ValueError:
        return
    raise AssertionError("评委端口应答缺 content 应显式报错")


async def test_rubric_stage_mismatch_is_rejected() -> None:
    """阶段与 rubric 错配（拿 writing rubric 评 analysis）不可静默执行。"""
    try:
        await LlmRubricEvaluator(RecordingLLM()).evaluate("analysis", _artifact(), _rubric("writing"))
    except ValueError:
        return
    raise AssertionError("rubric.stage 与待评阶段不一致时必须拒绝")


async def test_extract_json_object_handles_plain_and_wrapped() -> None:
    assert extract_json_object('{"a": 1}') == '{"a": 1}'
    assert extract_json_object('前缀 {"a": 1} 后缀') == '{"a": 1}'
    assert extract_json_object("没有对象") == "没有对象"


# ----------------------------------------------------------------------
# 评委自身成本如实上报（自动评审消耗不隐藏）
# ----------------------------------------------------------------------
async def test_usage_sink_records_each_judge_call() -> None:
    records: list[dict] = []
    rubric = _rubric()
    evaluator = LlmRubricEvaluator(RecordingLLM(), usage_sink=records.append)
    await evaluator.evaluate("analysis", _artifact(), rubric)
    await evaluator.evaluate("analysis", _artifact(), rubric)
    assert len(records) == 2
    assert records[0]["stage"] == "analysis"
    assert records[0]["model"] == "judge-model"
    assert records[0]["usage"]["prompt_tokens"] == 1200


# ----------------------------------------------------------------------
# 与 GateRunner 的端到端语义（重试 ≤3 转人工）
# ----------------------------------------------------------------------
async def test_gate_runner_passes_with_llm_judge_on_first_attempt() -> None:
    rubric = _rubric()
    llm = RecordingLLM(content=_passing_output(rubric))
    result = await GateRunner(LlmRubricEvaluator(llm)).run("analysis", _artifact(), rubric)
    assert result.passed is True
    assert len(llm.calls) == 1


async def test_gate_runner_retries_judge_at_most_three_times() -> None:
    rubric = _rubric()
    llm = RecordingLLM(content=_scores([d.name for d in rubric.dimensions], 0.2))
    runner = GateRunner(LlmRubricEvaluator(llm))
    result = await runner.run("analysis", _artifact(), rubric)
    assert result.passed is False
    assert runner.retry_count == 3
    assert len(llm.calls) == 3
