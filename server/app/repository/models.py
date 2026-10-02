"""ORM 模型（服务端域表唯一建表方，与《数据模型设计》对齐）。

表结构为契约冻结项：变更必须过数据模型评审，并同步 Alembic 迁移脚本。
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Integer,
    MetaData,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.clock import utc_now

# 统一约束命名约定：保证 create_all 与 Alembic 迁移生成的对象名一致
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """ORM 基类：统一命名约定 + 自增主键。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)


class AuditLog(Base):
    """审计日志（覆盖登录/购买/权益变更/许可签发/离线对账等关键操作）。"""

    __tablename__ = "audit_logs"

    request_key: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, doc="Idempotency-Key，幂等去重键"
    )
    actor_type: Mapped[str] = mapped_column(String(32), index=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    resource_type: Mapped[str] = mapped_column(String(32))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    client_ip: Mapped[str | None] = mapped_column(String(45))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PresenceSession(Base):
    """在线会话（看板 FR-1）：记录每个用户-设备最近一次心跳时间。"""

    __tablename__ = "presence_sessions"
    __table_args__ = (
        UniqueConstraint("user_id", "device_id", name="uq_presence_sessions_user_device"),
    )

    user_id: Mapped[str] = mapped_column(String(64))
    device_id: Mapped[str | None] = mapped_column(String(64))
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    client_ip: Mapped[str | None] = mapped_column(String(45))


class UsersDailyStats(Base):
    """用户总量日快照（看板 FR-2）：由账号域事件驱动的日终聚合写入。"""

    __tablename__ = "users_daily_stats"

    stat_date: Mapped[date] = mapped_column(Date, unique=True)
    total_users: Mapped[int] = mapped_column(Integer, default=0)
    new_users: Mapped[int] = mapped_column(Integer, default=0)
    active_users: Mapped[int] = mapped_column(Integer, default=0)


class PresenceMinuteAgg(Base):
    """在线数分钟桶（看板趋势模块）：心跳路径实时写，同在读数以最新值为准。"""

    __tablename__ = "presence_minute_agg"

    minute_ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), unique=True, index=True
    )
    online_count: Mapped[int] = mapped_column(Integer, default=0)


class DashboardEvent(Base):
    """看板事件投影（事件时间关联系统）：统一承载上线/审计/积分等事件。

    dedup_key 唯一保证事件投影幂等（同 Key 只投影一次）。
    """

    __tablename__ = "dashboard_events"

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info")
    actor_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    dedup_key: Mapped[str | None] = mapped_column(String(96), unique=True)


class MonitoringMinuteSnapshot(Base):
    """服务端运行监测分钟快照（每分钟一行，采样器聚合写入）。"""

    __tablename__ = "monitoring_minute_snapshots"

    minute_ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), unique=True, index=True
    )
    qps: Mapped[float] = mapped_column(default=0.0)
    p50_ms: Mapped[float] = mapped_column(default=0.0)
    p95_ms: Mapped[float] = mapped_column(default=0.0)
    error_rate: Mapped[float] = mapped_column(default=0.0)
    cpu_percent: Mapped[float] = mapped_column(default=0.0)
    memory_percent: Mapped[float] = mapped_column(default=0.0)
    db_query_p95_ms: Mapped[float] = mapped_column(default=0.0)
    db_pool_usage: Mapped[float] = mapped_column(default=0.0)


class Account(Base):
    """用户账号（《数据模型设计》users）：邮箱/手机至少其一，bcrypt 哈希。

    id 为 uuid 主键（契约）；注销为软删除（deleted_at + 匿名化），流水保留可审计。
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(  # type: ignore[assignment]  # 契约 uuid 主键，覆盖 Base 自增 id
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    email: Mapped[str | None] = mapped_column(String(255), unique=True)
    phone: Mapped[str | None] = mapped_column(String(32), unique=True)
    password_hash: Mapped[str] = mapped_column(String(72))
    status: Mapped[str] = mapped_column(String(16), default="active")
    role: Mapped[str] = mapped_column(String(16), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Device(Base):
    """设备登记（《数据模型设计》devices）：指纹防刷与注册赠分标记。"""

    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(  # type: ignore[assignment]  # 契约 uuid 主键
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    platform: Mapped[str | None] = mapped_column(String(32))
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    first_gift_used: Mapped[bool] = mapped_column(Boolean, default=False)


class AuthRefreshToken(Base):
    """刷新令牌（《数据模型设计》refresh_tokens）：只存哈希，支持轮换吊销与设备撤销。

    roles 为签发时角色快照（实现扩展列，随 SP2-3 数据模型评审收口）；
    user_id/device_id 为逻辑外键（不建 FK 约束，注销时同事务清理）。
    """

    __tablename__ = "auth_refresh_tokens"

    id: Mapped[str] = mapped_column(  # type: ignore[assignment]  # 契约 uuid 主键
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    device_id: Mapped[str | None] = mapped_column(String(36), index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    replaced_by_hash: Mapped[str | None] = mapped_column(String(64))


class PointAccount(Base):
    """积分账户（《数据模型设计》point_accounts）：双余额（购买/月度）+ 乐观锁 + 欠费冻结。

    purchased_balance 永不过期；monthly_balance 月底清零（SP2-5 月赠发放）；
    version 乐观锁保证并发扣减不超扣；frozen 为欠费冻结新任务开关。
    主键为 user_id（一用户一账户），覆盖 Base 的自增 id。
    """

    __tablename__ = "point_accounts"

    id: Mapped[str | None] = mapped_column(  # type: ignore[assignment]  # 覆盖 Base 自增主键
        String(36), nullable=True, primary_key=False
    )
    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    purchased_balance: Mapped[int] = mapped_column(Integer, default=0)
    monthly_balance: Mapped[int] = mapped_column(Integer, default=0)
    frozen: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class PointLedger(Base):
    """积分流水（《数据模型设计》point_ledgers）：追加式、终态不可迁移、exec_id 幂等。

    status 状态机：reserved → confirmed / refunded（终态）；grant / offline_sync 直接终态。
    unique(user_id, exec_id, kind) 保证同一业务键只入账一次。
    """

    __tablename__ = "point_ledgers"

    __table_args__ = (
        UniqueConstraint("user_id", "exec_id", "kind", name="uq_point_ledgers_exec"),
    )

    id: Mapped[str] = mapped_column(  # type: ignore[assignment]  # 契约 uuid 主键
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    exec_id: Mapped[str] = mapped_column(String(64), index=True)
    delta: Mapped[int] = mapped_column(Integer)
    balance_type: Mapped[str] = mapped_column(String(16))  # purchased / monthly
    kind: Mapped[str] = mapped_column(String(16))  # grant / reserve / confirm / refund / offline_sync
    status: Mapped[str] = mapped_column(String(16), default="reserved")  # reserved / confirmed / refunded
    source: Mapped[str] = mapped_column(String(32), default="stage")  # register_gift / stage / offline ...
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    stage: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class StageGrant(Base):
    """阶段许可（《数据模型设计》stage_grants）：Ed25519 签名，客户端离线验签启动阶段。

    exec_id 为许可幂等键；signature 对 payload 规范化字节签名；expires_at 防回拨。
    主键为 exec_id，覆盖 Base 的自增 id。
    """

    __tablename__ = "stage_grants"

    id: Mapped[str | None] = mapped_column(  # type: ignore[assignment]  # 覆盖 Base 自增主键
        String(36), nullable=True, primary_key=False
    )
    exec_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    stage: Mapped[str] = mapped_column(String(16))
    points: Mapped[int] = mapped_column(Integer)
    signature: Mapped[str] = mapped_column(String(128))
    key_version: Mapped[str] = mapped_column(String(16))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / used / expired


# ----------------------------------------------------------------------
# 计费订阅域（SP2-5 资金域核心）：products / orders / subscriptions / payment_callbacks
# ----------------------------------------------------------------------
class Product(Base):
    """商品（《数据模型设计》products）：定价配置化，运营可后台调价。

    type: subscription / points_pack；price_cents 一律整数分（禁止浮点金额）；
    points 为积分包面额或订阅月赠额度；duration_days 订阅周期天数（积分包为 0）。
    """

    __tablename__ = "products"

    __table_args__ = (
        UniqueConstraint("code", name="uq_products_code"),
    )

    id: Mapped[str] = mapped_column(  # type: ignore[assignment]  # 契约 uuid 主键
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    code: Mapped[str] = mapped_column(String(32))  # 稳定商品编码，如 sub_monthly / pack_400
    type: Mapped[str] = mapped_column(String(16), index=True)  # subscription / points_pack
    name: Mapped[str] = mapped_column(String(64))
    price_cents: Mapped[int] = mapped_column(Integer)
    points: Mapped[int] = mapped_column(Integer, default=0)  # 月赠积分额度 / 积分包面额
    duration_days: Mapped[int] = mapped_column(Integer, default=0)  # 订阅周期天数
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Order(Base):
    """订单（《数据模型设计》orders）：状态机 created → paid / closed / refunded。

    idempotency_key unique 保证同一下单请求只生成一单；30 分钟未支付自动关单；
    channel 支付通道（wechat / alipay / mock）。
    """

    __tablename__ = "orders"

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_orders_idempotency"),
    )

    id: Mapped[str] = mapped_column(  # type: ignore[assignment]  # 契约 uuid 主键
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    product_id: Mapped[str] = mapped_column(String(36), index=True)
    price_cents: Mapped[int] = mapped_column(Integer)
    channel: Mapped[str] = mapped_column(String(16), default="mock")
    status: Mapped[str] = mapped_column(String(16), default="created", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Subscription(Base):
    """订阅（《数据模型设计》subscriptions）：last_monthly_grant_at 为月赠幂等依据。

    plan: monthly / yearly；status: active / expired / cancelled；
    到期前 3 天提醒（SP2-7 调度），到期冻结新任务。
    """

    __tablename__ = "subscriptions"

    __table_args__ = (
        UniqueConstraint("user_id", "plan", name="uq_subscriptions_user_plan"),
    )

    id: Mapped[str] = mapped_column(  # type: ignore[assignment]  # 契约 uuid 主键
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    plan: Mapped[str] = mapped_column(String(16))  # monthly / yearly
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_monthly_grant_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class PaymentCallback(Base):
    """支付回调审计（《数据模型设计》payment_callbacks）：payment_no unique 去重。

    raw_digest 回调原文摘要（验签审计）；processed 是否已入账；
    unique(payment_no) 保证重复回调只处理一次。
    """

    __tablename__ = "payment_callbacks"

    __table_args__ = (
        UniqueConstraint("payment_no", name="uq_payment_callbacks_no"),
    )

    id: Mapped[str] = mapped_column(  # type: ignore[assignment]  # 契约 uuid 主键
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    payment_no: Mapped[str] = mapped_column(String(64))
    order_id: Mapped[str] = mapped_column(String(36), index=True)
    raw_digest: Mapped[str] = mapped_column(String(128))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)


# ----------------------------------------------------------------------
# 权益快照域（SP2-6 权益内容服务）：entitlement_snapshots
# ----------------------------------------------------------------------
class EntitlementSnapshot(Base):
    """权益快照（《数据模型设计》entitlement_snapshots）：聚合订阅+积分状态并 Ed25519 签名。

    payload 为规范化 JSON（sort_keys），客户端凭服务端公钥离线验签；
    issued_at 为签发时间，客户端防回拨依据。
    """

    __tablename__ = "entitlement_snapshots"

    __table_args__ = (
        UniqueConstraint("user_id", "issued_at", name="uq_entitlement_snapshots_issued"),
    )

    id: Mapped[str] = mapped_column(  # type: ignore[assignment]  # 契约 uuid 主键
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    signature: Mapped[str] = mapped_column(String(128))
    key_version: Mapped[str] = mapped_column(String(16))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


# ----------------------------------------------------------------------
# 内容库域（SP2-6）：problems / paper_templates / cases / content_manifests
# ----------------------------------------------------------------------
class Problem(Base):
    """历年真题库（《数据模型设计》problems）：business_id 稳定编号主键。

    attachments 存 [{name, oss_key, sha256, media_type}]，数据文件走 OSS；
    visibility 控制 public（示例）/ member（会员刷题）。
    """

    __tablename__ = "problems"

    id: Mapped[str | None] = mapped_column(  # type: ignore[assignment]  # 覆盖 Base 自增主键
        String(36), nullable=True, primary_key=False
    )
    business_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    competition: Mapped[str] = mapped_column(String(16))
    year: Mapped[int] = mapped_column(Integer)
    problem_code: Mapped[str] = mapped_column(String(8))
    title: Mapped[str] = mapped_column(String(128))
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    prompt_zh: Mapped[str] = mapped_column(String(8192))
    prompt_en: Mapped[str] = mapped_column(String(8192), default="")
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    scoring: Mapped[str] = mapped_column(String(2048), default="")
    dataset_hint: Mapped[str] = mapped_column(String(2048), default="")
    visibility: Mapped[str] = mapped_column(String(16), default="public")  # public / member
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class PaperTemplate(Base):
    """论文模板库（《数据模型设计》paper_templates）：oss_key+sha256 存文件位置与校验。

    tier 控制 member_only / free（基础模板免费）。
    """

    __tablename__ = "paper_templates"

    id: Mapped[str | None] = mapped_column(  # type: ignore[assignment]  # 覆盖 Base 自增主键
        String(36), nullable=True, primary_key=False
    )
    business_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    competition: Mapped[str] = mapped_column(String(16))
    format: Mapped[str] = mapped_column(String(16))  # latex / docx
    oss_key: Mapped[str] = mapped_column(String(256))
    sha256: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    changelog: Mapped[str] = mapped_column(String(512), default="")
    tier: Mapped[str] = mapped_column(String(16), default="member_only")  # member_only / free
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Case(Base):
    """往届案例库（《数据模型设计》cases）：compliance_note 强制非空。

    compliance_note 为「仅作方法参照，禁止大段抄袭」合规声明，客户端强制展示。
    """

    __tablename__ = "cases"

    id: Mapped[str | None] = mapped_column(  # type: ignore[assignment]  # 覆盖 Base 自增主键
        String(36), nullable=True, primary_key=False
    )
    business_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    problem_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(128))
    award: Mapped[str] = mapped_column(String(32), default="")
    method_tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    oss_key: Mapped[str] = mapped_column(String(256))
    sha256: Mapped[str] = mapped_column(String(64))
    compliance_note: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ContentManifest(Base):
    """内容增量同步清单（《数据模型设计》content_manifest）：按 scope 分版本。

    items 存 [{business_id, sha256}]，客户端比对 version 做增量拉取。
    """

    __tablename__ = "content_manifests"

    __table_args__ = (
        UniqueConstraint("scope", name="uq_content_manifests_scope"),
    )

    id: Mapped[str] = mapped_column(  # type: ignore[assignment]  # 契约 uuid 主键
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    scope: Mapped[str] = mapped_column(String(32))  # problems / templates / cases
    version: Mapped[int] = mapped_column(Integer, default=1)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


# ----------------------------------------------------------------------
# 通知与调度域（SP2-7）：notification_send_logs / scheduler_runs
# ----------------------------------------------------------------------
class NotificationSendLog(Base):
    """通知发送记录（《服务端架构》通知服务全链路审计）：发送结果落库。

    message_id 唯一为幂等去重键（同一条消息重复投递只发送一次）；
    channel 渠道（email / sms）；status 状态（queued / sent / failed）。
    """

    __tablename__ = "notification_send_logs"

    __table_args__ = (
        UniqueConstraint("message_id", name="uq_notification_send_logs_message"),
    )

    id: Mapped[str] = mapped_column(  # type: ignore[assignment]  # 契约 uuid 主键
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    message_id: Mapped[str] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(String(16))  # email / sms
    template_id: Mapped[str] = mapped_column(String(32))
    target: Mapped[str] = mapped_column(String(128))  # 邮箱 / 手机号
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    error: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SchedulerRun(Base):
    """调度任务批次记录：task_name + batch_key 唯一，幂等批次。

    重复触发（同 task_name + batch_key）跳过，避免月赠/对账重复执行。
    """

    __tablename__ = "scheduler_runs"

    __table_args__ = (
        UniqueConstraint("task_name", "batch_key", name="uq_scheduler_runs_batch"),
    )

    id: Mapped[str] = mapped_column(  # type: ignore[assignment]  # 契约 uuid 主键
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    task_name: Mapped[str] = mapped_column(String(32), index=True)
    batch_key: Mapped[str] = mapped_column(String(64))  # 幂等批次键（如月赠的年月）
    status: Mapped[str] = mapped_column(String(16), default="running")  # running / done / failed
    result: Mapped[str | None] = mapped_column(String(256), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TelemetryEventRecord(Base):
    """遥测事件（SP4-1，SQLite 降级，对齐《数据模型设计》telemetry_events 宽表）。

    隐私红线：props 已经白名单过滤（无题面/Key/路径字段）；
    TTL 90 天由应用层清理（生产 ClickHouse 按月分区 TTL）。
    """

    __tablename__ = "telemetry_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_name: Mapped[str] = mapped_column(String(32), index=True)
    distinct_id: Mapped[str] = mapped_column(String(128))
    props: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    app_version: Mapped[str] = mapped_column(String(32), default="")
    os: Mapped[str] = mapped_column(String(16), default="")
    channel: Mapped[str] = mapped_column(String(16), default="stable")
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)