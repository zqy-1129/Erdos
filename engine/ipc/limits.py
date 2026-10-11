"""NDJSON 单行上限的执行点（G25/W36）：`contracts/engine-rpc.schema.json` 的
`transport.limits.max_line_bytes` 在引擎侧落地。

两个方向的暴露面不同，处置也必须不同：

- **写向（引擎→宿主）**：宿主读到超限行会按协议错误**整行丢弃**（`client/main/engine-host/rpc.ts:99`），
  表现为"引擎发了、宿主永远看不见"——比报错更糟。所以写向的保证是**绝不发出超限行**：
  事件负载就地截断并标 `truncated`，响应退化为 JSON-RPC 错误信封。
- **读向（宿主→引擎）**：超限请求行当场按 `INVALID_REQUEST` 拒绝并计数，**不断链**（EC-S6：
  事件洪水/坏行不得终止会话）。这里限的是"引擎愿意解析并分派的单行规模"，不是读缓冲的字节上限：
  stdin 是 `configure_stdio()` 重配过的 UTF-8 strict 文本层，首行握手也从同一层读，
  改走 `sys.stdin.buffer` 有双层缓冲丢字节的风险，收益不抵代价（该边界同步登记在契约）。
"""

import json
from dataclasses import asdict, dataclass, field
from typing import Any

MAX_LINE_BYTES = 1048576  # 与契约 transport.limits.max_line_bytes 同源（守护比对字面值，G32 同族）

# 截断时不许动的字段：标识与枚举类字段截断会让宿主无法路由（task_id 截断=事件投给错的任务）
_UNTOUCHABLE = frozenset(
    {"trace_id", "event", "timestamp", "seq", "task_id", "stage", "tool", "call_id", "artifact", "gate"}
)
_MIN_FIELD_BUDGET = 32
_TRUNCATE_MARK = "…(truncated)"


def line_bytes(text: str) -> int:
    """一行按 UTF-8 编码后的字节数（协议字节序红线：与宿主侧 Buffer.byteLength 同口径）。"""
    return len(text.encode("utf-8"))


def dumps(value: Any) -> str:
    """协议序列化：紧凑分隔符 + 不转义非 ASCII，与 EventEmitter/encode_error 一致。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass
class ProtocolStats:
    """协议层异常计数（get_status 的 `protocol` 字段）；只增不减，供宿主观测而非静默吞掉。"""

    oversized_in: int = 0  # 读向：被拒绝的超限请求行
    oversized_out: int = 0  # 写向：被降级为错误信封的超限响应
    truncated_events: int = 0  # 写向：被就地截断的事件数

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def bound_event_payload(payload: dict[str, Any], required: set[str]) -> tuple[dict[str, Any], bool]:
    """把事件负载压进单行上限；**只砍内容，不砍路由**。

    两级：① 反复砍最长的"可截内容字段"的一半，直到装得下；② 仍装不下就丢掉所有非必填字段。
    可截与否看 `_UNTOUCHABLE` 而不是看必填——`gate.failed.reason` 必填但它才是内容，必须允许砍；
    `task_id`/`stage`/`seq` 截断会让事件投给错的任务或无法去重，绝不砍。

    返回 `(负载, 是否降级)`。降级后仍超限（必填路由字段本身撑爆）时由调用方**放弃发送并计数**，
    因为发出去宿主会整行丢弃——既丢事件又坏链路，不如留在引擎侧显式暴露。
    """
    out = dict(payload)
    if fits(out):
        return out, False

    degraded = False
    while True:
        candidates = [
            key
            for key, value in out.items()
            if isinstance(value, str) and key not in _UNTOUCHABLE and len(value) > _MIN_FIELD_BUDGET
        ]
        if not candidates:
            break
        worst = max(candidates, key=lambda key: len(out[key]))
        shrunk = out[worst][: max(_MIN_FIELD_BUDGET, len(out[worst]) // 2)] + _TRUNCATE_MARK
        # 带不进标记（字段已接近标记长度）时清空它：否则这一轮没有任何收缩，循环原地打转
        out[worst] = shrunk if len(shrunk) < len(out[worst]) else ""
        degraded = True
        if fits(out):
            return out, degraded

    droppable = [key for key in out if key not in required]
    for key in droppable:
        del out[key]
    return out, degraded or bool(droppable)


def fits(value: Any) -> bool:
    """序列化后是否装得进单行上限。"""
    return line_bytes(dumps(value)) <= MAX_LINE_BYTES


@dataclass(slots=True)
class LineGuard:
    """一行文本的上限判定与统计（读写两侧共用同一份计数）。"""

    stats: ProtocolStats = field(default_factory=ProtocolStats)

    def exceeds(self, line: str) -> bool:
        return line_bytes(line) > MAX_LINE_BYTES

    def note_oversized_in(self) -> None:
        self.stats.oversized_in += 1

    def note_oversized_out(self) -> None:
        self.stats.oversized_out += 1

    def note_truncated(self) -> None:
        self.stats.truncated_events += 1
