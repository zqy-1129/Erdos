"""看板实时推送主题常量（契约化：主题名变更需同步前端与订阅方）。"""

PRESENCE_TOPIC = "presence.online_count"
EVENTS_TOPIC = "dashboard.events"
MONITORING_TOPIC = "monitoring.snapshot"
REGISTRATION_GIFT_TOPIC = "points.registration_grant"  # SP2-3 注册赠分领域事件（SP2-4 积分域消费）