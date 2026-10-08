"""留痕记录器单元测试（SP1-6）：四类事件齐全 / sha256 一致 / 写入失败阻断 / 无删除 API。"""

import hashlib
import sqlite3

import pytest

from engine.trail.recorder import TrailRecorder, sha256_of
from engine.trail.store import EventType, TrailStore


def _recorder(tmp_path) -> tuple[TrailRecorder, TrailStore]:
    store = TrailStore(str(tmp_path / "trail.db"))
    return TrailRecorder(store), store


async def test_sink_records_measured_model_duration(tmp_path) -> None:
    """model_call 的耗时来自真实测量值，不是占位 0（F-007 留痕精度）。"""
    from engine.__main__ import _build_sink

    recorder, store = _recorder(tmp_path)
    await _build_sink(recorder)("t1", "analysis", {
        "model": "timed-model",
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        "duration_ms": 1234.5,
    })
    event = next(e for e in store.events("t1") if e.event_type == EventType.MODEL_CALL)
    assert event.detail["duration_ms"] == 1234.5


def test_four_event_types_all_recorded(tmp_path) -> None:
    """跑完整一题：四类事件齐全。"""
    recorder, store = _recorder(tmp_path)

    # 产物文件
    artifact = tmp_path / "out.txt"
    artifact.write_text("result content", encoding="utf-8")

    # 四类事件
    recorder.record_model_call("t1", "analysis", "deepseek-chat", {"total_tokens": 30}, 1200.0)
    recorder.record_tool_call("t1", "modeling", "solve", {"method": "linear"})
    recorder.record_artifact("t1", "solving", "data", artifact)
    recorder.record_manual_edit("t1", "writing", "人工修正结论")

    events = store.events("t1")
    types = {e.event_type for e in events}
    assert types == {
        EventType.MODEL_CALL,
        EventType.TOOL_CALL,
        EventType.ARTIFACT,
        EventType.MANUAL_EDIT,
    }


def test_append_event_redacts_sensitive_detail(tmp_path) -> None:
    """密钥域红线：留痕落库前剔除敏感键（含 authorization）。"""
    _, store = _recorder(tmp_path)
    store.append_event(
        "t1",
        "analysis",
        "model_call",
        {
            "model": "deepseek-chat",
            "total_tokens": 30,
            "api_key": "sk-plain-secret",
            "authorization": "Bearer eyJ....sig",
            "nested": {"refresh_token": "rt-x"},
        },
    )
    (event,) = store.events("t1")
    assert event.detail == {"model": "deepseek-chat", "total_tokens": 30, "nested": {}}  # 敏感键被剔除


def test_artifact_sha256_matches_file(tmp_path) -> None:
    """产物 sha256 与产物文件一致率 100%。"""
    recorder, store = _recorder(tmp_path)
    artifact = tmp_path / "data.csv"
    content = b"a,b,c\n1,2,3\n"
    artifact.write_bytes(content)

    recorder.record_artifact("t1", "solving", "data", artifact)

    # 与直接计算的 sha256 一致
    expected = hashlib.sha256(content).hexdigest()
    artifacts = store.artifacts("t1")
    assert len(artifacts) == 1
    assert artifacts[0]["sha256"] == expected
    assert artifacts[0]["size_bytes"] == len(content)


def test_sha256_of_helper(tmp_path) -> None:
    """sha256_of 辅助函数与 hashlib 一致。"""
    f = tmp_path / "f.txt"
    f.write_text("hello", encoding="utf-8")
    assert sha256_of(f) == hashlib.sha256(b"hello").hexdigest()


def test_artifact_missing_file_raises(tmp_path) -> None:
    """产物不存在：记录抛异常（不静默）。"""
    recorder, _ = _recorder(tmp_path)
    with pytest.raises(FileNotFoundError):
        recorder.record_artifact("t1", "solving", "data", tmp_path / "nope.txt")


def test_write_failure_blocks_task(tmp_path) -> None:
    """留痕写入失败：抛异常让任务失败（非静默丢弃）。"""
    store = TrailStore(str(tmp_path / "trail.db"))
    store.close()  # 关闭连接后写入应失败
    recorder = TrailRecorder(store)
    with pytest.raises(sqlite3.ProgrammingError):
        recorder.record_model_call("t1", "analysis", "m", {}, 1.0)


def test_no_delete_api() -> None:
    """无删除 API：TrailStore 不暴露 delete/clear 方法。"""
    assert not hasattr(TrailStore, "delete")
    assert not hasattr(TrailStore, "clear")
    assert not hasattr(TrailStore, "remove")
    assert not hasattr(TrailRecorder, "delete")
    assert not hasattr(TrailRecorder, "clear")


def test_events_append_only_order(tmp_path) -> None:
    """只追加：事件按 id 顺序，无覆盖。"""
    recorder, store = _recorder(tmp_path)
    recorder.record_model_call("t1", "analysis", "m1", {}, 1.0)
    recorder.record_model_call("t1", "modeling", "m2", {}, 2.0)
    events = store.events("t1")
    assert len(events) == 2
    assert events[0].id < events[1].id  # 追加序
    assert events[0].detail["model"] == "m1"
    assert events[1].detail["model"] == "m2"
