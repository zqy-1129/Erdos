"""monitoring_minute_snapshots 服务端运行监测分钟快照（看板运维域）

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "monitoring_minute_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("minute_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("qps", sa.Float(), nullable=False),
        sa.Column("p50_ms", sa.Float(), nullable=False),
        sa.Column("p95_ms", sa.Float(), nullable=False),
        sa.Column("error_rate", sa.Float(), nullable=False),
        sa.Column("cpu_percent", sa.Float(), nullable=False),
        sa.Column("memory_percent", sa.Float(), nullable=False),
        sa.Column("db_query_p95_ms", sa.Float(), nullable=False),
        sa.Column("db_pool_usage", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_monitoring_minute_snapshots"),
        sa.UniqueConstraint("minute_ts", name="uq_monitoring_minute_snapshots_minute_ts"),
    )
    op.create_index(
        "ix_monitoring_minute_snapshots_minute_ts",
        "monitoring_minute_snapshots",
        ["minute_ts"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_monitoring_minute_snapshots_minute_ts",
        table_name="monitoring_minute_snapshots",
    )
    op.drop_table("monitoring_minute_snapshots")