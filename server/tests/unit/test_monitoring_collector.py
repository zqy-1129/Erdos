"""运行监测采集器测试：窗口聚合、分位数、错误率与路径频率。"""

from app.infra.monitoring import MonitoringCollector, percentile


def _feed(collector: MonitoringCollector, durations_ms, monkeypatch, status=200, path="/v1/health"):
    """以受控时钟注入 (ts, duration, is_error) 样本。"""
    fake = iter(float(i) for i in range(10_000))
    monkeypatch.setattr("app.infra.monitoring.time.perf_counter", lambda: next(fake))
    for ms in durations_ms:
        collector.record_http(path, ms / 1000, status)


def test_percentile_linear_interpolation() -> None:
    values = [float(i) for i in range(100)]
    assert percentile(values, 50.0) == 49.5
    assert percentile(values, 95.0) == 94.05
    assert percentile([], 95.0) == 0.0
    assert percentile([42.0], 95.0) == 42.0


def test_snapshot_aggregates_qps_and_error_rate(monkeypatch) -> None:
    collector = MonitoringCollector()
    _feed(collector, [10.0, 20.0, 30.0, 40.0], monkeypatch, path="/v1/presence/heartbeat")
    snapshot = collector.snapshot(60.0)
    assert snapshot.qps == 4 / 60
    assert snapshot.error_rate == 0.0
    assert snapshot.p95_ms == 38.5  # rank=2.85 -> 30*0.15 + 40*0.85
    assert snapshot.paths[0].path == "/v1/presence/heartbeat"
    assert snapshot.paths[0].requests == 4


def test_snapshot_counts_5xx_as_errors(monkeypatch) -> None:
    collector = MonitoringCollector()
    fake = iter(float(i) for i in range(10_000))
    monkeypatch.setattr("app.infra.monitoring.time.perf_counter", lambda: next(fake))
    collector.record_http("/v1/health", 0.01, 200)
    collector.record_http("/v1/health", 0.02, 404)   # 4xx 不计错误
    collector.record_http("/v1/health", 0.03, 500)
    snapshot = collector.snapshot(60.0)
    assert snapshot.error_rate == 1 / 3


def test_snapshot_path_delta_between_snapshots(monkeypatch) -> None:
    collector = MonitoringCollector()
    _feed(collector, [1.0, 1.0, 1.0], monkeypatch, path="/v1/a")
    _feed(collector, [1.0], monkeypatch, path="/v1/b")
    first = collector.snapshot(60.0)
    assert [(p.path, p.requests) for p in first.paths] == [("/v1/a", 3), ("/v1/b", 1)]
    # 无新请求：下一次快照路径增量为空
    second = collector.snapshot(60.0)
    assert second.paths == ()


def test_snapshot_window_prunes_stale_samples(monkeypatch) -> None:
    collector = MonitoringCollector()
    timeline = {"t": 1000.0}
    monkeypatch.setattr(
        "app.infra.monitoring.time.perf_counter", lambda: timeline["t"]
    )
    collector.record_http("/v1/health", 0.01, 200)   # t=1000
    timeline["t"] = 1061.0
    collector.record_http("/v1/health", 0.09, 200)   # t=1061
    snapshot = collector.snapshot(60.0)  # 窗口起点 1001 -> 首个样本过期
    assert snapshot.qps == 1 / 60
    assert snapshot.p95_ms == 90.0


def test_system_metrics_shape() -> None:
    collector = MonitoringCollector()
    cpu, mem, rss = collector.system_metrics()
    assert 0.0 <= cpu <= 100.0
    assert 0.0 < mem <= 100.0
    assert rss > 0.0