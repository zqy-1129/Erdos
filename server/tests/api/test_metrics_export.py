"""/metrics 导出运行期计数：告警值班路由与总线消费者失败必须"有人看"。

这两处计数此前只有测试读取（`AlertRouter.routed_counts`、`MessageBus.delivery_failures`），
也就是本项目反复出现的"能力已实现、零消费方"——计数存在但没人看，等于没有。
现在挂到 Prometheus 抓取上：抓取时刷新（而不是随采样循环），因为监测可以关，
而告警路由与总线失败和监测无关。

契约面不变：`/metrics` 是 `include_in_schema=False` 的运维端点，不在 openapi 里，
所以这批改动不产生任何契约 diff（冻结日不动契约）。
"""

from types import SimpleNamespace

from app.core.topics import EVENTS_TOPIC
from app.domain.alerts.ports import BusinessAlert
from app.domain.alerts.severity import Channel, Severity
from app.infra.metrics import export_runtime_counters


async def test_metrics_exports_alert_routing_and_bus_failures(client, app) -> None:
    """抓取一次就能读到：P0 走 feishu 通道计数 +1，坏消费者按主题计数 +1。"""
    from app.core.clock import utc_now

    await app.state.alert_outlet.emit(
        BusinessAlert(
            key="availability", severity=Severity.P0, message="可用性跌破 99.5%", value=0.98
        ),
        utc_now(),
        dedupe=False,
    )

    async def bad(message) -> None:  # noqa: ANN001
        raise RuntimeError("消费者炸了")

    await app.state.message_bus.subscribe(EVENTS_TOPIC, bad)
    await app.state.message_bus.publish(EVENTS_TOPIC, {"probe": True})

    body = (await client.get("/metrics")).text
    assert 'erdos_alert_routed_total{channel="feishu"} 1.0' in body
    assert f'erdos_bus_delivery_failures_total{{topic="{EVENTS_TOPIC}"}} 1.0' in body


async def test_export_tolerates_unwired_state() -> None:
    """装配未完成时抓取不得 500：`/metrics` 可能在 lifespan 结束前被抓到。"""
    export_runtime_counters(SimpleNamespace())
    export_runtime_counters(SimpleNamespace(alert_outlet=None, message_bus=None))


async def test_export_mirrors_source_of_truth(client, app) -> None:
    """指标是镜像不是第二份账本：再路由一条 P2，抓取值跟着变。"""
    from app.core.clock import utc_now

    outlet = app.state.alert_outlet
    await outlet.emit(
        BusinessAlert(key="db_pool_usage", severity=Severity.P2, message="连接池水位高", value=0.9),
        utc_now(),
        dedupe=False,
    )
    body = (await client.get("/metrics")).text
    assert 'erdos_alert_routed_total{channel="message"} 1.0' in body
    assert outlet.routed_counts[Channel.MESSAGE] == 1
