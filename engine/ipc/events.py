"""NDJSON 事件发射器（SP1-1）：含 traceID + 时序 + schema 校验。

事件走 stdout 的 NDJSON 流（每行一个 JSON），主进程按 event 字段分发。
禁止在事件负载中出现 API Key / 敏感字段（合规红线）。
"""

import sys
import uuid
from datetime import datetime
from typing import Any, TextIO

from engine.ipc.limits import LineGuard, bound_event_payload, dumps

# 允许的事件类型（与 contracts/engine-rpc.schema.json 对齐；
# tool.call/tool.result/model.delta 为 CT-V2 v2 增量：负载须经脱敏词表）
VALID_EVENTS = ("stage.progress", "artifact.ready", "gate.failed", "tool.call", "tool.result", "model.delta")

# 不进入回放缓冲的事件（W15：token 流允许丢帧、不补发，终态以 stage.progress/artifact 为准）
NO_REPLAY_EVENTS = ("model.delta",)

# 各事件必填字段（schema 校验用）
REQUIRED_FIELDS = {
    "stage.progress": {"trace_id", "event", "task_id", "stage", "progress", "timestamp"},
    "artifact.ready": {"trace_id", "event", "task_id", "artifact", "sha256", "timestamp"},
    "gate.failed": {"trace_id", "event", "task_id", "gate", "reason", "timestamp"},
    "tool.call": {"trace_id", "event", "task_id", "stage", "call_id", "tool", "args_summary", "timestamp"},
    "tool.result": {"trace_id", "event", "task_id", "call_id", "tool", "ok", "duration_ms", "timestamp"},
    "model.delta": {"trace_id", "event", "task_id", "delta", "timestamp"},
}


class EventEmitter:
    """NDJSON 事件发射器：写 stdout，逐条 schema 校验；带 seq 与回放缓冲（W14）。"""

    def __init__(
        self, sink: TextIO | None = None, trace_id: str | None = None, buffer_size: int = 1000,
        guard: LineGuard | None = None,
    ) -> None:
        self._sink: TextIO = sink or sys.stdout
        self._trace_id = trace_id or uuid.uuid4().hex
        self._seq = 0
        self._buffer: list[dict] = []  # 回放缓冲（环形，events_replay 消费）
        self._buffer_size = buffer_size
        self._guard = guard or LineGuard()

    @property
    def guard(self) -> LineGuard:
        """协议层计数（与 server 共用同一实例时，宿主页能看到读写两侧合计）。"""
        return self._guard

    @property
    def last_seq(self) -> int:
        return self._seq

    def emit(self, event: str, **fields: Any) -> str:
        """发射一条事件；event 非法或缺必填字段抛 ValueError。"""
        if event not in VALID_EVENTS:
            raise ValueError(f"非法事件类型：{event}")
        self._seq += 1
        payload = {
            "trace_id": self._trace_id, "event": event,
            "timestamp": datetime.now().astimezone().isoformat(),
            "seq": self._seq, **fields,
        }
        missing = REQUIRED_FIELDS[event] - set(payload)
        if missing:
            raise ValueError(f"事件 {event} 缺必填字段：{sorted(missing)}")
        payload, degraded = bound_event_payload(payload, REQUIRED_FIELDS[event])
        if degraded:
            # 标记本身也占字节：加上后再收一次，保证"发出去的这一行"满足上限而不是近似满足
            payload["truncated"] = True
            payload, _ = bound_event_payload(payload, REQUIRED_FIELDS[event])
        line = dumps(payload)
        if self._guard.exceeds(line):
            # 必填路由字段本身撑爆上限（极端）：不发这一条。发出去宿主会整行丢弃并按协议错误
            # 处理，结果同样是丢事件，还额外把链路搞坏——所以留在引擎侧计数并放弃。
            self._guard.note_oversized_out()
            return ""
        if degraded:
            self._guard.note_truncated()  # 计数描述"实际发出去的东西"，不描述中途尝试
        self._sink.write(line + "\n")
        self._sink.flush()
        if event not in NO_REPLAY_EVENTS:  # token 流不进缓冲（允许丢帧、不补发）
            self._buffer.append(payload)
            if len(self._buffer) > self._buffer_size:
                del self._buffer[: len(self._buffer) - self._buffer_size]
        return line

    def replay(self, after_seq: int = 0, limit: int = 200, task_id: str | None = None) -> list[dict]:
        """seq > after_seq 的缓冲事件按序补发（可选按 task 过滤）；断线重连恢复终态事件。"""
        found = [
            e for e in self._buffer
            if e["seq"] > after_seq and (task_id is None or e.get("task_id") == task_id)
        ]
        return found[:limit]
