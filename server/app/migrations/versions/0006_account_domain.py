"""users + devices 账号域表；auth_refresh_tokens 对齐契约重建（SP2-3 账号服务）

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("password_hash", sa.String(length=72), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint("phone", name="uq_users_phone"),
    )

    op.create_table(
        "devices",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_gift_used", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_devices"),
    )
    op.create_index("ix_devices_user_id", "devices", ["user_id"])
    op.create_index("ix_devices_fingerprint", "devices", ["fingerprint"])

    # auth_refresh_tokens 按 4.2 契约重建：uuid 主键、user_id、device_id fk 语义（SP2-3 前数据为开发数据，允许丢弃）
    op.drop_index("ix_auth_refresh_tokens_expires_at", table_name="auth_refresh_tokens")
    op.drop_index("ix_auth_refresh_tokens_subject", table_name="auth_refresh_tokens")
    op.drop_index("ix_auth_refresh_tokens_token_hash", table_name="auth_refresh_tokens")
    op.drop_table("auth_refresh_tokens")
    op.create_table(
        "auth_refresh_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("roles", sa.JSON(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("device_id", sa.String(length=36), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_hash", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_auth_refresh_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_auth_refresh_tokens_token_hash"),
    )
    op.create_index(
        "ix_auth_refresh_tokens_token_hash", "auth_refresh_tokens", ["token_hash"], unique=True
    )
    op.create_index("ix_auth_refresh_tokens_user_id", "auth_refresh_tokens", ["user_id"])
    op.create_index("ix_auth_refresh_tokens_device_id", "auth_refresh_tokens", ["device_id"])
    op.create_index("ix_auth_refresh_tokens_expires_at", "auth_refresh_tokens", ["expires_at"])
    op.create_index("ix_auth_refresh_tokens_revoked_at", "auth_refresh_tokens", ["revoked_at"])


def downgrade() -> None:
    op.drop_index("ix_auth_refresh_tokens_revoked_at", table_name="auth_refresh_tokens")
    op.drop_index("ix_auth_refresh_tokens_expires_at", table_name="auth_refresh_tokens")
    op.drop_index("ix_auth_refresh_tokens_device_id", table_name="auth_refresh_tokens")
    op.drop_index("ix_auth_refresh_tokens_user_id", table_name="auth_refresh_tokens")
    op.drop_index("ix_auth_refresh_tokens_token_hash", table_name="auth_refresh_tokens")
    op.drop_table("auth_refresh_tokens")
    op.create_table(
        "auth_refresh_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=64), nullable=False),
        sa.Column("roles", sa.JSON(), nullable=False),
        sa.Column("device_id", sa.String(length=64), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_hash", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_auth_refresh_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_auth_refresh_tokens_token_hash"),
    )
    op.create_index("ix_auth_refresh_tokens_token_hash", "auth_refresh_tokens", ["token_hash"], unique=True)
    op.create_index("ix_auth_refresh_tokens_subject", "auth_refresh_tokens", ["subject"])
    op.create_index("ix_auth_refresh_tokens_expires_at", "auth_refresh_tokens", ["expires_at"])

    op.drop_index("ix_devices_fingerprint", table_name="devices")
    op.drop_index("ix_devices_user_id", table_name="devices")
    op.drop_table("devices")
    op.drop_table("users")