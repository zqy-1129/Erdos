"""看板实时推送主题常量（契约化：主题名变更需同步前端与订阅方）。"""

PRESENCE_TOPIC = "presence.online_count"
EVENTS_TOPIC = "dashboard.events"
MONITORING_TOPIC = "monitoring.snapshot"
REGISTRATION_GIFT_TOPIC = "points.registration_grant"  # SP2-3 注册赠分领域事件（SP2-4 积分域消费）

# ----------------------------------------------------------------------
# SP2-7 异步消息队列主题（对齐《服务端架构》第 6 章）
# ----------------------------------------------------------------------
MQ_PAYMENT_CALLBACK = "payment.callback"  # 网关验签后 → 计费服务；至少一次，消费者幂等
MQ_POINTS_GRANT = "points.grant"  # 计费/账号 → 积分服务；充值入账、注册赠送、月赠
MQ_ENTITLEMENT_CHANGED = "entitlement.changed"  # 计费 → 权益服务；重建快照、通知客户端刷新
MQ_NOTIFICATION_SEND = "notification.send"  # 各服务 → 通知服务；幂等，重复投递去重
MQ_TELEMETRY_RAW = "telemetry.raw"  # 遥测摄入 → 数据平台；削峰缓冲，批量落 ClickHouse

MQ_TOPICS: frozenset[str] = frozenset({
    MQ_PAYMENT_CALLBACK,
    MQ_POINTS_GRANT,
    MQ_ENTITLEMENT_CHANGED,
    MQ_NOTIFICATION_SEND,
    MQ_TELEMETRY_RAW,
})