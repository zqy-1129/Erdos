"""遥测摄入服务（SP4-1）：schema 校验 + props 白名单过滤 + 批写。

隐私红线（SP4-1 提示词）：
- 事件 schema 校验来自 contracts/telemetry.schema.json；
- 违禁字段（题面/Key/路径）过滤为丢弃 + 告警；
- 未经白名单的字段不写入存储。
"""

from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.domain.telemetry.ports import (
    TelemetryEvent,
    TelemetryRepository,
    filter_allowed_props,
    validate_event,
)

logger = get_logger("erdos.telemetry")


@dataclass(slots=True)
class IngestResult:
    """一批事件摄入结果。"""

    accepted: int = 0
    rejected: int = 0
    reasons: list[str] = field(default_factory=list)


class TelemetryService:
    """遥测摄入用例：校验 → 过滤 → 批写。"""

    def __init__(self, repository: TelemetryRepository, batch_size: int = 1000) -> None:
        self._repository = repository
        self._batch_size = batch_size

    async def ingest(self, events: list[TelemetryEvent]) -> IngestResult:
        """摄入一批事件：校验 + 白名单过滤 + 违禁拦截，批写落库。"""
        result = IngestResult()
        accepted_events: list[TelemetryEvent] = []

        for event in events:
            validation = validate_event(event)
            if not validation.valid:
                result.rejected += 1
                result.reasons.append(validation.reason)
                logger.warning("遥测事件被拦截：%s", validation.reason)
                continue
            # 白名单过滤（冗余防线）
            cleaned = TelemetryEvent(
                event_name=event.event_name,
                distinct_id=event.distinct_id,
                props=filter_allowed_props(event.props),
                app_version=event.app_version,
                os=event.os,
                channel=event.channel,
            )
            accepted_events.append(cleaned)

        # 批写（1000 行/批）
        for i in range(0, len(accepted_events), self._batch_size):
            batch = accepted_events[i : i + self._batch_size]
            result.accepted += await self._repository.batch_insert(batch)

        return result
