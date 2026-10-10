"""月赠发放失败的可观测性（SP2-7）：单人失败不得静默，且不得拖垮整批。

原实现是 `except Exception: continue`——没有计数、没有日志、没有告警，于是"某个人订了
订阅却没拿到当月积分"这件事在全链路里不留痕迹；更糟的是失败发生在同一个会话里，
一次 flush 失败会让会话进入待回滚状态，后面所有人都跟着失败。

现在：每人一个保存点隔离失败、失败计数进批次结果、任务层转 P1 告警、明细 exec_id 落日志。
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.domain.alerts.ports import BusinessAlert
from app.domain.alerts.severity import MONTHLY_GRANT_METRIC, Severity
from app.domain.points.service import PointsService as RealPointsService
from app.infra import scheduler_runners
from app.infra.scheduler_tasks import run_monthly_grant_task
from app.repository.models import PointAccount, Subscription
from app.repository.uow import UnitOfWork

NOW = datetime(2026, 10, 1, 0, 5, tzinfo=UTC)
BROKEN_USER = "sub-broken"


class RecordingOutlet:
    def __init__(self) -> None:
        self.emitted: list[BusinessAlert] = []

    async def emit(self, alert: BusinessAlert, now: datetime, dedupe: bool = True) -> bool:
        self.emitted.append(alert)
        return True


class BoomForOne(RealPointsService):
    """只让一个用户失败：被测的是失败处置（计数/日志/告警/隔离），不是积分域的失败原因。

    积分域自己的失败分支由 test_points*.py 覆盖；这里要的是"发放途中炸一个人"这个时序
    在调度批次里的后果。
    """

    async def grant_points(self, user_id: str, *args, **kwargs) -> None:
        if user_id == BROKEN_USER:
            raise RuntimeError("积分账户写入冲突（模拟）")
        await super().grant_points(user_id, *args, **kwargs)


@pytest.fixture
def broken_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler_runners, "PointsService", BoomForOne)


async def _seed(session_factory, *user_ids: str) -> None:
    async with UnitOfWork(session_factory) as uow:
        for user_id in user_ids:
            uow.session.add(
                Subscription(
                    user_id=user_id,
                    plan="monthly",
                    status="active",
                    start_at=NOW - timedelta(days=1),
                    end_at=NOW + timedelta(days=29),
                )
            )
        await uow.session.flush()


async def _balances(session_factory) -> dict[str, int]:
    async with UnitOfWork(session_factory) as uow:
        return {
            row.user_id: row.monthly_balance
            for row in (await uow.session.execute(select(PointAccount))).scalars()
        }


async def test_one_failure_does_not_poison_the_batch(
    session_factory, settings, broken_grant
) -> None:
    """3 个活跃订阅里 1 个发放炸了：另外 2 人照常拿到月赠，失败被计数。"""
    await _seed(session_factory, "sub-ok-1", BROKEN_USER, "sub-ok-2")
    outlet = RecordingOutlet()

    result = await run_monthly_grant_task(session_factory, settings, outlet, NOW)

    assert result == "granted:2 failed:1", result
    assert await _balances(session_factory) == {"sub-ok-1": 400, "sub-ok-2": 400}, (
        "失败者之后的账户也被拖垮了——每人一个保存点没生效"
    )


async def test_failure_alert_is_p1_without_user_details(
    session_factory, settings, broken_grant
) -> None:
    """告警口径：P1、指标键已登记、文案只带计数并指路 exec_id，不落用户明细。"""
    await _seed(session_factory, "sub-ok", BROKEN_USER)
    outlet = RecordingOutlet()

    await run_monthly_grant_task(session_factory, settings, outlet, NOW)

    assert len(outlet.emitted) == 1
    alert = outlet.emitted[0]
    assert alert.key == MONTHLY_GRANT_METRIC
    assert alert.severity is Severity.P1
    assert alert.value == 1.0
    assert "1/2" in alert.message
    assert BROKEN_USER not in alert.message
    assert "exec_id" in alert.message, "文案要指路：值班靠 exec_id 回查流水"


async def test_failure_detail_is_logged_with_exec_id(
    session_factory, settings, broken_grant, caplog
) -> None:
    """明细落日志（含 exec_id），告警载荷不落明细——两边合起来才可追查。"""
    await _seed(session_factory, BROKEN_USER)

    with caplog.at_level("ERROR", logger="erdos.scheduler"):
        await run_monthly_grant_task(session_factory, settings, RecordingOutlet(), NOW)

    assert "月赠发放失败" in caplog.text
    assert f"monthly:{BROKEN_USER}:monthly:202610" in caplog.text


async def test_all_success_emits_no_alert(session_factory, settings) -> None:
    """零失败不告警：否则每月 1 日全员订阅都会触发一条噪音。"""
    await _seed(session_factory, "sub-1", "sub-2")
    outlet = RecordingOutlet()

    result = await run_monthly_grant_task(session_factory, settings, outlet, NOW)

    assert result == "granted:2 failed:0"
    assert outlet.emitted == []
    assert await _balances(session_factory) == {"sub-1": 400, "sub-2": 400}


async def test_second_run_same_month_is_idempotent(session_factory, settings) -> None:
    """批次键幂等：同月第二次触发不再发放，也不会因为"没跑"而误报失败。"""
    await _seed(session_factory, "sub-1")
    outlet = RecordingOutlet()

    first = await run_monthly_grant_task(session_factory, settings, outlet, NOW)
    second = await run_monthly_grant_task(session_factory, settings, outlet, NOW)

    assert first == "granted:1 failed:0"
    assert second == "already_ran"
    assert outlet.emitted == []


def test_grant_amount_comes_from_settings(settings: Settings) -> None:
    """发放额度取自配置（默认 400 分/月），不写死在执行器里。"""
    assert settings.subscription_monthly_grant_points == 400
