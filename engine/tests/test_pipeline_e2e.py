"""端到端系统测试（SP1-7 支撑）：LangGraph 编排 + 四阶段流水线 + 沙箱 + 留痕全链路。

使用确定性 FakeLLM（离线），验证「题目 → 四阶段 → 门禁 → 产物+哈希」引擎内闭环；
真实厂商模型端的回归留待 SP1-5 真 Key 注入（本测试验证局部接线正确性）。
"""

import hashlib
from pathlib import Path

import pytest

from engine.orchestrator.graph import STAGES, StageOrchestrator
from engine.orchestrator.pipeline import StagePipeline
from engine.sandbox.base import validate_artifact_path
from engine.sandbox.subprocess_sandbox import SubprocessSandbox
from engine.trail.recorder import TrailRecorder
from engine.trail.store import TrailStore


class SinkRecorder:
    """记录 sink 回调序列（断言四阶段全覆盖）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def __call__(self, task_id: str, stage: str, data: dict) -> None:
        self.calls.append((task_id, stage, data))


async def test_stage_data_carries_model_duration(tmp_path: Path) -> None:
    """阶段数据透传模型耗时，sink 才能记下真实 duration_ms（F-007 精度）。"""

    async def timed_llm(messages: list[dict[str, str]], stage: str) -> dict:
        return {
            "content": "决策变量；约束条件",
            "usage": {"prompt_tokens": 5, "completion_tokens": 5},
            "model": "timed-model",
            "stage": stage,
            "duration_ms": 777.5,
        }

    sink = SinkRecorder()
    pipeline = StagePipeline(llm=timed_llm, sink=sink, work_root=tmp_path)
    data = await pipeline.process("t-dur", "analysis")
    assert data["duration_ms"] == 777.5
    assert sink.calls[0][2]["duration_ms"] == 777.5


async def _run_full(task_id: str, pipeline: StagePipeline) -> dict:
    orch = StageOrchestrator(task_id, runner=pipeline.process)
    final: dict = {}
    for stage in STAGES:
        data = await orch.run_current_stage()
        action = await orch.answer_gate("pass")
        if stage == STAGES[-1]:
            assert action["action"] == "complete"
        else:
            assert action["action"] == "next_stage"
        final = data
    return final


async def test_full_pipeline_solves_and_writes_paper(tmp_path) -> None:
    sandbox = SubprocessSandbox(timeout=30)
    sink = SinkRecorder()
    pipeline = StagePipeline(sandbox=sandbox, sink=sink, work_root=tmp_path / "work")
    final = await _run_full("t-e2e", pipeline)

    # sink 覆盖四阶段
    assert [stage for _, stage, _ in sink.calls] == list(STAGES)
    # solving 阶段经沙箱真实执行：最小二乘解存在
    solving = next(data for _, stage, data in sink.calls if stage == "solving")
    assert "slope=" in solving.get("stdout", "")
    assert "intercept=" in solving.get("stdout", "")

    # writing 产物：结构化论文 + sha256（EN-PAPER 论文组装格式）
    assert final["paper_md"].startswith("# t-e2e")
    for section in ("摘要", "问题重述", "求解与结果", "结论"):
        assert section in final["paper_md"]
    digest = hashlib.sha256(final["paper_md"].encode("utf-8")).hexdigest()
    assert final["paper_sha256"] == digest


async def test_pipeline_trail_records_four_stages_and_artifact(tmp_path) -> None:
    sandbox = SubprocessSandbox(timeout=30)
    trail_db = str(tmp_path / "trail.db")
    store = TrailStore(trail_db)
    recorder = TrailRecorder(store)

    async def trail_sink(task_id: str, stage: str, data: dict) -> None:
        recorder.record_model_call(task_id, stage, "fake-llm", data.get("usage", {}), 0.0)
        if stage == "writing":
            recorder.record_artifact(task_id, stage, "paper", data["paper_path"])

    pipeline = StagePipeline(sandbox=sandbox, sink=trail_sink, work_root=tmp_path / "work")
    orch = StageOrchestrator("t-trail", runner=pipeline.process)
    for _ in STAGES:
        await orch.run_current_stage()
        await orch.answer_gate("pass")

    events = store.events("t-trail")
    assert len([e for e in events if e.event_type == "model_call"]) == 4
    artifacts = store.artifacts("t-trail")
    assert len(artifacts) == 1
    assert artifacts[0]["kind"] == "paper"
    assert len(artifacts[0]["sha256"]) == 64
    store.close()


async def test_pipeline_llm_failure_fails_task_not_silent() -> None:
    async def broken_llm(messages: list[dict[str, str]], stage: str) -> dict:
        raise ConnectionError("厂商不可达（模拟）")

    pipeline = StagePipeline(llm=broken_llm, sandbox=SubprocessSandbox(timeout=30), work_root=Path("ignored"))
    orch = StageOrchestrator("t-fail", runner=pipeline.process)
    with pytest.raises(ConnectionError):  # 失败上抛：任务失败不静默（SP1-6 语义）
        await orch.run_current_stage()


def test_sandbox_artifact_path_guard_rejects_escape(tmp_path) -> None:
    work = tmp_path / "w"
    assert validate_artifact_path(work, "out.csv") is True
    assert validate_artifact_path(work, "../escape.txt") is False
    assert validate_artifact_path(work, "a/../../escape.txt") is False