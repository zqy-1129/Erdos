"""presence_sessions 在线会话表 + users_daily_stats 用户日快照表（看板 P1）

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "presence_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("device_id", sa.String(length=64), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("client_ip", sa.String(length=45), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_presence_sessions"),
        sa.UniqueConstraint(
            "user_id", "device_id", name="uq_presence_sessions_user_device"
        ),
    )
    op.create_index(
        "ix_presence_sessions_last_seen_at", "presence_sessions", ["last_seen_at"]
    )

    op.create_table(
        "users_daily_stats",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("stat_date", sa.Date(), nullable=False),
        sa.Column("total_users", sa.Integer(), nullable=False),
        sa.Column("new_users", sa.Integer(), nullable=False),
        sa.Column("active_users", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users_daily_stats"),
        sa.UniqueConstraint("stat_date", name="uq_users_daily_stats_stat_date"),
    )


def downgrade() -> None:
    op.drop_table("users_daily_stats")
    op.drop_index("ix_presence_sessions_last_seen_at", table_name="presence_sessions")
    op.drop_table("presence_sessions")