"""运行监测接口集成测试（overview/trend/alerts + RBAC）。"""

from datetime import UTC, datetime

from app.domain.monitoring.ports import MonitoringSample
from app.repository.models import DashboardEvent, MonitoringMinuteSnapshot

ADMIN = {"Authorization": "Bearer admin-token"}
BASE = "/v1/admin/monitoring"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _sample(**overrides) -> MonitoringSample:
    base = dict(
        sampled_at=NOW,
        qps=12.0,
        error_rate=0.02,
        p50_ms=20.0,
        p95_ms=80.0,
        cpu_percent=10.0,
        memory_percent=30.0,
        rss_mb=64.0,
        db_query_p95_ms=30.0,
        db_pool_usage=0.1,
    )
    base.update(overrides)
    return MonitoringSample(**base)


async def test_overview_without_sample_returns_null_values(admin_client) -> None:
    resp = await admin_client.get(BASE + "/overview", headers=ADMIN)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["sampled_at"] is None
    assert data["values"] is None
    assert data["active_alerts"] == []
    assert data["hot_paths"] == []
    assert data["thresholds"]["p95_ms"] == 500.0
    assert data["thresholds"]["error_rate"] == 5.0
    assert data["uptime_seconds"] >= 0


async def test_overview_with_cached_sample_and_alerts(admin_app, admin_client) -> None:
    admin_app.state.monitoring_last = _sample()
    admin_app.state.monitoring_alerts = {"p95_ms": "超过阈值 500.0"}
    resp = await admin_client.get(BASE + "/overview", headers=ADMIN)
    data = resp.json()["data"]
    assert data["values"]["qps"] == 12.0
    assert data["values"]["error_rate"] == 2.0  # 比率 -> 百分比
    assert data["values"]["db_pool_usage"] == 10.0
    assert [a["metric"] for a in data["active_alerts"]] == ["p95_ms"]
    assert data["active_alerts"][0]["label"] == "P95 延迟"
    assert data["hot_paths"] == []


async def test_overview_requires_admin_role(client) -> None:
    # 裸请求：未认证 -> 40101；带令牌但无角色 -> 40301
    resp = await client.get(BASE + "/overview")
    assert resp.json()["code"] == 40101
    resp = await client.get(BASE + "/overview", headers=ADMIN)
    assert resp.json()["code"] == 40301


async def test_trend_downsample_with_factor(admin_app, admin_client) -> None:
    factory = admin_app.state.session_factory
    async with factory() as session:
        for minute, qps, err in ((0, 2.0, 0.04), (2, 4.0, 0.08)):
            session.add(
                MonitoringMinuteSnapshot(
                    minute_ts=NOW.replace(minute=minute),
                    qps=qps,
                    error_rate=err,
                    p50_ms=1.0,
                    p95_ms=1.0,
                    cpu_percent=1.0,
                    memory_percent=1.0,
                    db_query_p95_ms=1.0,
                    db_pool_usage=0.01,
                )
            )
        await session.commit()

    from_t = NOW.isoformat()
    to_t = NOW.replace(minute=9).isoformat()
    resp = await admin_client.get(
        f"{BASE}/trend",
        params={"metric": "qps", "granularity": "5m", "from": from_t, "to": to_t},
        headers=ADMIN,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["unit"] == "req/s"
    assert data["points"][0]["value"] == 3.0  # avg(2,4)

    resp = await admin_client.get(
        f"{BASE}/trend",
        params={"metric": "error_rate", "granularity": "5m", "from": from_t, "to": to_t},
        headers=ADMIN,
    )
    data = resp.json()["data"]
    assert data["unit"] == "%"
    assert data["points"][0]["value"] == 6.0  # avg(0.04,0.08)*100


async def test_trend_rejects_unknown_metric(admin_client) -> None:
    resp = await admin_client.get(BASE + "/trend?metric=wat", headers=ADMIN)
    assert resp.json()["code"] == 40001


async def test_alerts_lists_monitor_events(admin_app, admin_client) -> None:
    factory = admin_app.state.session_factory
    async with factory() as session:
        session.add(
            DashboardEvent(
                occurred_at=NOW,
                type="monitor.alert",
                severity="warning",
                actor_id="system",
                payload={"metric": "cpu_percent", "state": "triggered"},
            )
        )
        session.add(
            DashboardEvent(
                occurred_at=NOW,
                type="user.online",
                severity="info",
                actor_id="u1",
                payload={},
            )
        )
        await session.commit()

    resp = await admin_client.get(BASE + "/alerts", headers=ADMIN)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["severity"] == "warning"
    assert data["items"][0]["payload"]["metric"] == "cpu_percent"


async def test_metrics_spec_lists_all_defs(admin_client) -> None:
    resp = await admin_client.get(BASE + "/metrics-spec", headers=ADMIN)
    data = resp.json()["data"]
    assert set(data) >= {"qps", "p95_ms", "error_rate", "cpu_percent", "memory_percent"}