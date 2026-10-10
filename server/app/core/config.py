"""应用配置（pydantic-settings，环境变量前缀 ERDOS_）。

约定：所有配置均可由环境变量覆盖，默认值面向本地开发（SQLite）；
切换 PostgreSQL 仅需替换 ERDOS_DATABASE_URL，业务代码零改动。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """服务端全局配置。"""

    model_config = SettingsConfigDict(
        env_prefix="ERDOS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 运行环境
    env: str = "dev"
    debug: bool = False
    log_level: str = "INFO"

    # 数据访问层（SQLite/PostgreSQL 双驱动，由 URL scheme 自动选择）
    database_url: str = "sqlite+aiosqlite:///./erdos.db"
    database_echo: bool = False

    # 网关
    metrics_enabled: bool = True
    request_id_header: str = "X-Request-Id"
    auth_enforce: bool = False  # SP2-2 接入 JWT 后在生产置为 True

    # 限流（滑动窗口）
    rate_limit_requests: int = 300
    rate_limit_window_seconds: float = 60.0
    audit_rate_limit_requests: int = 60

    # 看板（dashboard）
    presence_online_window_seconds: float = 90.0  # 在线判定滑动窗口（≈3 个心跳周期）
    presence_trend_retention_days: int = 30  # 在线分钟桶保留天数（看板收尾项）
    presence_aggregate_interval_seconds: float = 60.0  # 分钟桶进程内聚合落库周期（SP2-8 优化项 1）
    dashboard_admin_roles: str = "admin,operator"  # 逗号分隔的看板访问角色

    # 运行监测（monitoring：采样、窗口与沉淀保留）
    monitoring_interval_seconds: float = 5.0  # 采样/推送周期
    monitoring_rate_window_seconds: float = 60.0  # QPS/错误率滑动窗口
    monitoring_retention_days: int = 7  # 分钟快照落库保留天数
    monitoring_availability_min_requests: int = 50  # 可用性 P0 的最小样本门（低流量窗口不误报）

    # 告警阈值（ERDOS_ALERT_* 覆盖；看板内红色状态与告警事件判据）
    alert_p95_ms: float = 500.0
    alert_error_rate: float = 0.01  # 《服务端架构》§10 P1 口径：错误率 > 1%（原默认 5% 与文档不符）
    alert_qps: float = 100.0
    alert_cpu_percent: float = 85.0
    alert_memory_percent: float = 85.0
    alert_db_p95_ms: float = 200.0
    alert_db_pool_usage: float = 0.8

    # 告警 Webhook 外发（ERDOS_ALERT_WEBHOOK_URL；空=关闭外发，看板内告警不受影响）
    alert_webhook_url: str = ""
    alert_webhook_timeout_seconds: float = 3.0
    alert_webhook_retries: int = 2
    alert_webhook_backoff_seconds: float = 1.0

    # 月度可用性预算（99.5%）燃尽判级：比例相对"日窗口预算份额"，与月度同量纲
    slo_burn_page_ratio: float = 2.0  # 燃尽达允许速率 2 倍 -> P0
    slo_burn_warn_ratio: float = 1.0  # 已达预算速率（月底踩线）-> P1
    slo_coverage_floor: float = 0.5  # 日窗口观测覆盖率下限：没数据不等于健康

    # 认证授权（SP2-2）：JWT 双令牌、RBAC 四角色、防爆破、Ed25519
    auth_issuer: str = "erdos-server"
    auth_access_ttl_seconds: int = 900  # 访问令牌 15 分钟
    auth_refresh_ttl_days: int = 30  # 刷新令牌 30 天（轮换即吊销旧令牌）
    auth_lockout_threshold: int = 5  # 连续失败次数阈值（第 6 次触发锁定）
    auth_lockout_seconds: int = 900  # 锁定时长 15 分钟
    auth_signing_keys: str = ""  # "kid:hex32[,...]"；首个为签发密钥，其余为验签兜底；空仅 dev 允许并自动生成临时密钥
    auth_dev_users: str = ""  # dev 种子账号 "user:pass:role1,role2;..."，由 DevCredentialVerifier 解析

    # 账号域（SP2-3）：注册赠分、密码重置与撤销
    account_registration_gift_points: int = 100  # 注册赠分额度（领域事件，积分域入账）
    account_reset_token_ttl_seconds: int = 900  # 重置令牌有效期 15 分钟
    account_reset_resend_seconds: int = 60  # 重置请求重发间隔（PRD：60s/次）
    account_reset_daily_limit: int = 10  # 重置请求每日限额（PRD：每日 10 次）
    account_password_min_length: int = 8  # 密码强度：≥8 位含字母与数字

    # 积分域（SP2-4 资金域）：阶段许可有效期与离线对账
    license_ttl_seconds: int = 900  # 阶段许可有效期 15 分钟（客户端离线宽限）
    offline_sync_batch_limit: int = 200  # 单次离线对账批量上限（PRD：批量逐条回执）
    offline_sync_debt_threshold: int = 0  # 欠费冻结阈值：可用余额低于此值即冻结新任务

    # 计费订阅域（SP2-5 资金域）：下单/关单/退款/月赠
    order_close_minutes: int = 30  # 未支付订单自动关单时长（PRD：30 分钟）
    refund_grace_days: int = 7  # 订阅退款宽限期（PRD F-005：7 天内未用可退）
    subscription_monthly_grant_points: int = 400  # 订阅月赠额度（PRD：400 分/月）
    payment_callback_secret: str = ""  # 支付回调 HMAC 验签共享密钥（空=未配置，回调端点 fail-closed 拒绝）
    # 查单兜底（PRD DF-003 / EC-N7 / DEC-022）：回调丢失时服务端向渠道确认收款
    order_query_after_minutes: int = 5  # T+N 分钟后开始查单（PRD：T+5 分钟兜底补账）
    order_query_window_minutes: int = 5  # 同一订单的查单去重窗口（轮询打爆渠道的防护）
    order_reconcile_interval_seconds: int = 300  # 后台扫描周期；0=关闭（只靠客户端轮询与管理端触发）
    order_reconcile_batch_size: int = 50  # 单次扫描订单上限
    order_closed_recheck_hours: int = 24  # 已关单订单的收款复核间隔（回调也丢了时的最后一道兜底）
    order_closed_recheck_days: int = 7  # 复核只回溯这么久的关单（防陈旧 closed 订单挤掉待补账的新单）

    # 通知与调度域（SP2-7）：验证码限流与续费提醒
    verification_resend_seconds: int = 60  # 验证码重发间隔（PRD：60s/次）
    verification_daily_limit: int = 10  # 验证码每日限额（PRD：每日 10 次）
    renewal_remind_days: int = 3  # 续费提醒提前天数（PRD：到期前 3 天）
    # 调度器后台循环（月赠/到期冻结/对账的自动触发；周期口径取自《服务端架构》调度器表）
    scheduler_enabled: bool = True  # false 时三大任务只剩管理端手动触发
    scheduler_tick_seconds: int = 60  # 轮询到点的间隔
    scheduler_tz_offset_hours: int = 8  # 东八区（无夏令时，固定偏移即可）
    scheduler_stale_after_seconds: int = 1800  # 卡死批次可重占阈值（崩溃当月的月赠不能永久不再发放）
    scheduler_runs_retention_days: int = 30  # scheduler_runs 批次账本保留天数（每日对账顺带裁剪）

    # Redis（SP2-7 多实例迁移）：跨进程防爆破/验证码限流
    redis_url: str | None = None  # 如 redis://127.0.0.1:6379/0；未配置时回退进程内实现

    def admin_roles(self) -> tuple[str, ...]:
        """解析看板管理角色元组。"""
        return tuple(part.strip() for part in self.dashboard_admin_roles.split(",") if part.strip())