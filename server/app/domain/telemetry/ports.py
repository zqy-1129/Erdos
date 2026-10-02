"""遥测域端口与数据载体（SP4-1，与 contracts/telemetry.schema.json 对齐）。

隐私红线（硬性）：props 禁止出现题面文本、API Key、文件路径、论文内容、模型输出、留痕明细；
留痕合规数据仅存本地，永不上报。
"""

from dataclasses import dataclass
from typing import Any, Protocol

# 允许的事件名（与 contracts/telemetry.schema.json events 对齐）
VALID_EVENT_NAMES = frozenset({
    "page_view", "feature_click", "stage_start", "stage_success", "stage_fail",
    "gate_retry", "order_created", "pay_success", "app_error",
})

# 违禁字段（props 中出现即拦截丢弃 + 告警）
FORBIDDEN_PROPS = frozenset({
    "prompt_zh", "prompt_en", "paper_content", "api_key", "file_path",
    "model_output", "trail_detail",
})


@dataclass(frozen=True, slots=True)
class TelemetryEvent:
    """一条遥测事件。"""

    event_name: str
    distinct_id: str
    props: dict[str, Any]
    app_version: str = ""
    os: str = ""
    channel: str = "stable"


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """事件校验结果。"""

    valid: bool
    event: TelemetryEvent | None = None
    reason: str = ""  # 拦截原因（违禁字段/非法事件名）


class TelemetryRepository(Protocol):
    """遥测存储端口（SQLite 降级 / ClickHouse 批写）。"""

    async def batch_insert(self, events: list[TelemetryEvent]) -> int:
        """批量写入（1000 行/批），返回落库行数。"""
        ...


def validate_event(event: TelemetryEvent) -> ValidationResult:
    """校验事件：事件名合法 + props 无违禁字段。

    违禁字段拦截：props 含 FORBIDDEN_PROPS 中任一键 → 丢弃并告警。
    """
    if event.event_name not in VALID_EVENT_NAMES:
        return ValidationResult(valid=False, reason=f"非法事件名：{event.event_name}")
    for key in FORBIDDEN_PROPS:
        if key in event.props:
            return ValidationResult(valid=False, reason=f"违禁字段：{key}")
    return ValidationResult(valid=True, event=event)


def filter_allowed_props(props: dict[str, Any]) -> dict[str, Any]:
    """props 白名单过滤：仅保留非违禁字段（冗余防线，配合 validate_event 使用）。"""
    return {k: v for k, v in props.items() if k not in FORBIDDEN_PROPS}
