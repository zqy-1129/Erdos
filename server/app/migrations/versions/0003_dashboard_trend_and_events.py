"""presence_minute_agg 在线分钟桶 + dashboard_events 看板事件投影（P0 趋势与事件）

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "presence_minute_agg",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("minute_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("online_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_presence_minute_agg"),
        sa.UniqueConstraint("minute_ts", name="uq_presence_minute_agg_minute_ts"),
    )
    op.create_index(
        "ix_presence_minute_agg_minute_ts", "presence_minute_agg", ["minute_ts"]
    )

    op.create_table(
        "dashboard_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("dedup_key", sa.String(length=96), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_dashboard_events"),
        sa.UniqueConstraint("dedup_key", name="uq_dashboard_events_dedup_key"),
    )
    op.create_index("ix_dashboard_events_occurred_at", "dashboard_events", ["occurred_at"])
    op.create_index("ix_dashboard_events_type", "dashboard_events", ["type"])


def downgrade() -> None:
    op.drop_index("ix_dashboard_events_type", table_name="dashboard_events")
    op.drop_index("ix_dashboard_events_occurred_at", table_name="dashboard_events")
    op.drop_table("dashboard_events")
    op.drop_index("ix_presence_minute_agg_minute_ts", table_name="presence_minute_agg")
    op.drop_table("presence_minute_agg")