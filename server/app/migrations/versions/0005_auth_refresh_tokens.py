"""auth_refresh_tokens 刷新令牌存储表（SP2-2 认证授权中心）

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_index(
        "ix_auth_refresh_tokens_expires_at", table_name="auth_refresh_tokens"
    )
    op.drop_index("ix_auth_refresh_tokens_subject", table_name="auth_refresh_tokens")
    op.drop_index("ix_auth_refresh_tokens_token_hash", table_name="auth_refresh_tokens")
    op.drop_table("auth_refresh_tokens")