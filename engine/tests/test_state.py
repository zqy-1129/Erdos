"""引擎状态机单元测试（SP1-1）。"""

import pytest

from engine.ipc.state import EngineState, EngineStatus, StageStatus


def test_initial_state_idle() -> None:
    state = EngineState()
    snap = state.snapshot()
    assert snap["engine"] == EngineStatus.IDLE.value
    assert snap["task"] is None


def test_start_stage_transitions_to_running() -> None:
    state = EngineState()
    task = state.start_stage("t1", "analysis")
    assert task.task_id == "t1"
    assert task.stage == "analysis"
    assert task.status == StageStatus.RUNNING.value
    assert state.snapshot()["engine"] == EngineStatus.RUNNING.value


def test_start_stage_invalid_stage_rejected() -> None:
    state = EngineState()
    with pytest.raises(ValueError):
        state.start_stage("t1", "bogus")


def test_pause_resume_cancel_cycle() -> None:
    state = EngineState()
    state.start_stage("t1", "modeling")
    state.pause("t1")
    assert state.snapshot()["engine"] == EngineStatus.PAUSED.value
    state.resume("t1")
    assert state.snapshot()["engine"] == EngineStatus.RUNNING.value
    state.cancel("t1")
    assert state.snapshot()["engine"] == EngineStatus.IDLE.value
    assert state.task is not None
    assert state.task.status == StageStatus.CANCELLED.value


def test_operation_on_wrong_task_rejected() -> None:
    state = EngineState()
    state.start_stage("t1", "solving")
    with pytest.raises(ValueError):
        state.pause("t2")  # task_id 不匹配


def test_answer_gate_records_decision() -> None:
    state = EngineState()
    state.start_stage("t1", "analysis")
    state.answer_gate("t1", "gate-1", "pass")
    assert state.gate_answers["gate-1"] == "pass"
