"""遥测事件表（SP4-1 遥测管道）

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telemetry_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_name", sa.String(length=32), nullable=False),
        sa.Column("distinct_id", sa.String(length=128), nullable=False),
        sa.Column("props", sa.JSON(), nullable=False),
        sa.Column("app_version", sa.String(length=32), nullable=False),
        sa.Column("os", sa.String(length=16), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("event_ts", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_telemetry_events"),
    )
    op.create_index("ix_telemetry_events_event_name", "telemetry_events", ["event_name"])
    op.create_index("ix_telemetry_events_event_ts", "telemetry_events", ["event_ts"])


def downgrade() -> None:
    op.drop_index("ix_telemetry_events_event_ts", table_name="telemetry_events")
    op.drop_index("ix_telemetry_events_event_name", table_name="telemetry_events")
    op.drop_table("telemetry_events")
