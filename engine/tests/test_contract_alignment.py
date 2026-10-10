"""
契约三边一致性守护（引擎侧）：contracts/engine-rpc.schema.json ↔ engine 实现。

覆盖（自动化集成测试，防漂移）：
- register_all 注册的 RPC 方法名与 schema methods 一致（当前 10 个，含 CT-V2 增量）；
- EventEmitter 的 VALID_EVENTS 与 schema events 一致；
- REQUIRED_FIELDS 与 schema 各事件 required 一致；
- transport 段：configure_stdio() 的三条管道重配参数与契约逐字一致（UTF-8 + LF 红线）。
契约文件为唯一权威；任何一边变更不同步即红。
"""

import ast
import json
import sys
from pathlib import Path

import pytest

from engine.ipc import events as events_module
from engine.ipc.methods import register_all
from engine.ipc.stdio import configure_stdio

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


class RecordingStream:
    """记录 reconfigure() 调用的假管道（configure_stdio 只依赖 .reconfigure）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def reconfigure(self, **kwargs) -> None:  # noqa: ANN003
        self.calls.append(kwargs)


def test_configure_stdio_matches_contract_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """三条管道的编码/错误策略/换行符逐字取自契约 transport 段。

    stderr 允许 replace（日志坏字节不影响协议），stdin/stdout 必须 strict：
    坏字节按 replace 会变成 U+FFFD 并静默改写题面/task_id，比直接失败更危险。
    """
    transport = _schema()["transport"]
    channels = transport["channels"]
    streams = {name: RecordingStream() for name in ("stdin", "stdout", "stderr")}
    for name, stream in streams.items():
        monkeypatch.setattr(sys, name, stream)
    configure_stdio()

    for name in ("stdin", "stdout", "stderr"):
        assert streams[name].calls == [
            {
                "encoding": transport["encoding"],
                "errors": channels[name]["errors"],
                "newline": channels[name].get("newline"),
            }
        ], name
    assert channels["stdout"]["newline"] == "\n", "NDJSON 行结束符必须是 LF，不得回退到平台默认"


def test_configure_stdio_runs_before_any_protocol_io() -> None:
    """进程入口第一句就重配 stdio：晚一步则首行 Key/首帧请求已按宿主码页读走。"""
    entry = Path(__file__).resolve().parents[1] / "__main__.py"
    tree = ast.parse(entry.read_text(encoding="utf-8"))
    main = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    first = main.body[0]
    assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
    callee = first.value.func
    assert isinstance(callee, ast.Name) and callee.id == "configure_stdio"
