"""NDJSON 事件发射器单元测试（SP1-1）。"""

import io
import json

import pytest

from engine.ipc.events import EventEmitter


def _emitter() -> tuple[EventEmitter, io.StringIO]:
    sink = io.StringIO()
    return EventEmitter(sink=sink, trace_id="trace-123"), sink


def test_emit_progress_event() -> None:
    em, sink = _emitter()
    line = em.emit("stage.progress", task_id="t1", stage="analysis", progress=0.5)
    obj = json.loads(line)
    assert obj["trace_id"] == "trace-123"
    assert obj["event"] == "stage.progress"
    assert obj["progress"] == 0.5
    assert sink.getvalue().strip() == line


def test_emit_missing_required_field_rejected() -> None:
    em, _ = _emitter()
    with pytest.raises(ValueError):
        em.emit("stage.progress", task_id="t1")  # 缺 stage/progress


def test_emit_invalid_event_rejected() -> None:
    em, _ = _emitter()
    with pytest.raises(ValueError):
        em.emit("bogus.event", task_id="t1")


def test_emit_artifact_ready_with_sha256() -> None:
    em, _ = _emitter()
    line = em.emit("artifact.ready", task_id="t1", artifact="figure", sha256="a" * 64)
    obj = json.loads(line)
    assert obj["event"] == "artifact.ready"
    assert obj["sha256"] == "a" * 64


def test_emit_gate_failed() -> None:
    em, _ = _emitter()
    line = em.emit("gate.failed", task_id="t1", gate="gate-1", reason="评审未通过")
    obj = json.loads(line)
    assert obj["event"] == "gate.failed"
    assert obj["gate"] == "gate-1"
