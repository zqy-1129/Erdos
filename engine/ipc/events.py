"""NDJSON 事件发射器（SP1-1）：含 traceID + 时序 + schema 校验。

事件走 stdout 的 NDJSON 流（每行一个 JSON），主进程按 event 字段分发。
禁止在事件负载中出现 API Key / 敏感字段（合规红线）。
"""

import json
import sys
import uuid
from datetime import datetime
from typing import Any, TextIO

# 允许的事件类型（与 contracts/engine-rpc.schema.json 对齐；
# tool.call/tool.result 为 CT-V2 v2 增量：args_summary/error 须经脱敏词表）
VALID_EVENTS = ("stage.progress", "artifact.ready", "gate.failed", "tool.call", "tool.result")

# 各事件必填字段（schema 校验用）
REQUIRED_FIELDS = {
    "stage.progress": {"trace_id", "event", "task_id", "stage", "progress", "timestamp"},
    "artifact.ready": {"trace_id", "event", "task_id", "artifact", "sha256", "timestamp"},
    "gate.failed": {"trace_id", "event", "task_id", "gate", "reason", "timestamp"},
    "tool.call": {"trace_id", "event", "task_id", "stage", "call_id", "tool", "args_summary", "timestamp"},
    "tool.result": {"trace_id", "event", "task_id", "call_id", "tool", "ok", "duration_ms", "timestamp"},
}


class EventEmitter:
    """NDJSON 事件发射器：写 stdout，逐条 schema 校验。"""

    def __init__(self, sink: TextIO | None = None, trace_id: str | None = None) -> None:
        self._sink: TextIO = sink or sys.stdout
        self._trace_id = trace_id or uuid.uuid4().hex

    def emit(self, event: str, **fields: Any) -> str:
        """发射一条事件；event 非法或缺必填字段抛 ValueError。"""
        if event not in VALID_EVENTS:
            raise ValueError(f"非法事件类型：{event}")
        payload = {"trace_id": self._trace_id, "event": event, "timestamp": datetime.now().astimezone().isoformat(), **fields}
        missing = REQUIRED_FIELDS[event] - set(payload)
        if missing:
            raise ValueError(f"事件 {event} 缺必填字段：{sorted(missing)}")
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self._sink.write(line + "\n")
        self._sink.flush()
        return line
