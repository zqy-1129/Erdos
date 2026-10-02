"""遥测摄入测试（SP4-1）：违禁字段拦截 + 白名单过滤 + 吞吐保真 + 批写。"""

from app.domain.telemetry.ports import (
    TelemetryEvent,
    filter_allowed_props,
    validate_event,
)
from app.domain.telemetry.service import TelemetryService
from app.repository.telemetry import SQLAlchemyTelemetryRepository
from app.repository.uow import UnitOfWork


def _event(name: str = "page_view", props: dict | None = None) -> TelemetryEvent:
    return TelemetryEvent(
        event_name=name, distinct_id="d1", props=props or {},
        app_version="1.0", os="windows", channel="stable",
    )


# ----------------------------------------------------------------------
# schema 校验 + 隐私红线
# ----------------------------------------------------------------------
def test_validate_valid_event() -> None:
    r = validate_event(_event(props={"page": "home"}))
    assert r.valid is True


def test_validate_invalid_event_name() -> None:
    r = validate_event(_event(name="bogus_event"))
    assert r.valid is False
    assert "非法事件名" in r.reason


def test_validate_forbidden_prop_rejected() -> None:
    """违禁字段（题面/Key/路径）拦截。"""
    for key in ("prompt_zh", "api_key", "file_path", "paper_content"):
        r = validate_event(_event(props={key: "secret"}))
        assert r.valid is False
        assert key in r.reason


def test_filter_allowed_props_strips_forbidden() -> None:
    """白名单过滤：违禁字段被剔除。"""
    props = {"page": "home", "api_key": "sk-secret", "file_path": "/etc/passwd"}
    cleaned = filter_allowed_props(props)
    assert "page" in cleaned
    assert "api_key" not in cleaned
    assert "file_path" not in cleaned


# ----------------------------------------------------------------------
# 摄入 + 批写 + 吞吐保真
# ----------------------------------------------------------------------
async def test_ingest_accepted_and_rejected(session_factory) -> None:
    """摄入：合法接受，违禁拦截。"""
    events = [
        _event(props={"page": "home"}),
        _event(props={"api_key": "sk-secret"}),  # 违禁
        _event(name="stage_start", props={"stage": "analysis", "task_id": "t1"}),
    ]
    async with UnitOfWork(session_factory) as uow:
        service = TelemetryService(SQLAlchemyTelemetryRepository(uow.session))
        result = await service.ingest(events)
        assert result.accepted == 2
        assert result.rejected == 1
        assert any("api_key" in r for r in result.reasons)


async def test_ingest_throughput_fidelity(session_factory) -> None:
    """吞吐保真：1000 事件摄入，落库数 = 发送数（丢事件率 0）。"""
    events = [_event(props={"page": f"p{i}"}) for i in range(1000)]
    async with UnitOfWork(session_factory) as uow:
        service = TelemetryService(SQLAlchemyTelemetryRepository(uow.session))
        result = await service.ingest(events)
        assert result.accepted == 1000  # 全量落库，零丢失


async def test_purge_expired(session_factory) -> None:
    """TTL 清理：过期事件被清理。"""
    from datetime import UTC, datetime, timedelta

    repo = SQLAlchemyTelemetryRepository
    async with UnitOfWork(session_factory) as uow:
        store = repo(uow.session)
        await store.batch_insert([_event(props={"page": "old"})])
        # 清理（now 传入未来 91 天，使所有事件过期）
        purged = await store.purge_expired(datetime.now(UTC) + timedelta(days=91))
        assert purged >= 1
