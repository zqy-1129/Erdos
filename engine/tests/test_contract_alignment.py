"""
契约三边一致性守护（引擎侧）：contracts/engine-rpc.schema.json ↔ engine 实现。

覆盖（自动化集成测试，防漂移）：
- register_all 注册的 RPC 方法名与 schema methods 一致（当前 10 个，含 CT-V2 增量）；
- EventEmitter 的 VALID_EVENTS 与 schema events 一致；
- REQUIRED_FIELDS 与 schema 各事件 required 一致。
契约文件为唯一权威；任何一边变更不同步即红。
"""

import json
from pathlib import Path

from engine.ipc import events as events_module
from engine.ipc.methods import register_all

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "engine-rpc.schema.json"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


class RecordingServer:
    """收集注册方法名的假服务器。"""

    def __init__(self) -> None:
        self.methods: list[str] = []

    def register(self, name: str, handler) -> None:  # noqa: ANN001
        if name in self.methods:
            raise ValueError(f"方法重复注册：{name}")
        self.methods.append(name)


def test_schema_methods_match_registered() -> None:
    schema = _schema()
    server = RecordingServer()
    register_all(server, None)  # type: ignore[arg-type]  # 仅收集注册名，不执行方法
    assert sorted(server.methods) == sorted(schema["methods"].keys())


def test_schema_events_match_valid_events() -> None:
    schema = _schema()
    assert sorted(events_module.VALID_EVENTS) == sorted(schema["events"].keys())


def test_required_fields_match_schema_per_event() -> None:
    schema = _schema()
    assert sorted(events_module.REQUIRED_FIELDS.keys()) == sorted(schema["events"].keys())
    for event_name, required in events_module.REQUIRED_FIELDS.items():
        assert required == set(schema["events"][event_name]["required"]), event_name


def test_schema_methods_count_unchanged() -> None:
    """方法/事件数量为契约红线（增删必须同步三边后走评审）。

    CT-V2（2026-10）：events 由 v1 的 3 个增至 6 个（tool.call/tool.result/model.delta）；
    methods 由 v1 的 6 个增至 10 个（initialize/provider_test/events_replay/task_create）。
    """
    assert len(_schema()["methods"]) == 10
    assert len(_schema()["events"]) == 6