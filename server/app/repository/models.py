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