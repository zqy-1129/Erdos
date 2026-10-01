"""趋势下采样服务单元测试（M1 关键算法）。"""

from datetime import UTC, datetime

import pytest

from app.domain.metrics.ports import TrendPoint
from app.domain.metrics.service import TrendService

BASE = datetime(2026, 10, 1, tzinfo=UTC)  # epoch 整小时对齐，便于桶边界断言


def _pt(seconds: int, value: int) -> TrendPoint:
    return TrendPoint(ts=datetime.fromtimestamp(BASE.timestamp() + seconds, tz=UTC), value=value)


def test_m5_bucket_takes_max() -> None:
    service = TrendService()
    raw = [_pt(60, 2), _pt(120, 9), _pt(600, 5)]
    series = service.build(raw, "5m", BASE, _pt(3600, 0).ts)
    assert series.points == [_pt(0, 9), _pt(600, 5)]
    assert series.granularity == "5m"


def test_m1_passthrough() -> None:
    service = TrendService()
    raw = [_pt(60, 2), _pt(120, 3)]
    series = service.build(raw, "1m", BASE, _pt(3600, 0).ts)
    assert series.points == raw


def test_out_of_range_points_filtered() -> None:
    service = TrendService()
    raw = [_pt(-60, 7), _pt(60, 2), _pt(7200, 9)]
    series = service.build(raw, "1m", BASE, _pt(3600, 0).ts)
    assert [p.value for p in series.points] == [2]


def test_invalid_granularity_rejected() -> None:
    with pytest.raises(ValueError):
        TrendService().build([], "2m", BASE, BASE)


def test_empty_points_returns_empty_series() -> None:
    series = TrendService().build([], "1h", BASE, _pt(3600, 0).ts)
    assert series.points == []
    assert series.start == BASE