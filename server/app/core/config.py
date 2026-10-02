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
    dashboard_admin_roles: str = "admin,operator"  # 逗号分隔的看板访问角色

    # 运行监测（monitoring：采样、窗口与沉淀保留）
    monitoring_interval_seconds: float = 5.0  # 采样/推送周期
    monitoring_rate_window_seconds: float = 60.0  # QPS/错误率滑动窗口
    monitoring_retention_days: int = 7  # 分钟快照落库保留天数

    # 告警阈值（ERDOS_ALERT_* 覆盖；看板内红色状态与告警事件判据）
    alert_p95_ms: float = 500.0
    alert_error_rate: float = 0.05
    alert_qps: float = 100.0
    alert_cpu_percent: float = 85.0
    alert_memory_percent: float = 85.0
    alert_db_p95_ms: float = 200.0
    alert_db_pool_usage: float = 0.8

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

    def admin_roles(self) -> tuple[str, ...]:
        """解析看板管理角色元组。"""
        return tuple(part.strip() for part in self.dashboard_admin_roles.split(",") if part.strip())