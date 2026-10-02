"""通知发送记录与调度批次表（SP2-7 通知服务与调度器）

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_send_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("message_id", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("template_id", sa.String(length=32), nullable=False),
        sa.Column("target", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_notification_send_logs"),
        sa.UniqueConstraint("message_id", name="uq_notification_send_logs_message"),
    )
    op.create_index("ix_notification_send_logs_status", "notification_send_logs", ["status"])

    op.create_table(
        "scheduler_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_name", sa.String(length=32), nullable=False),
        sa.Column("batch_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("result", sa.String(length=256), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_scheduler_runs"),
        sa.UniqueConstraint("task_name", "batch_key", name="uq_scheduler_runs_batch"),
    )
    op.create_index("ix_scheduler_runs_task_name", "scheduler_runs", ["task_name"])


def downgrade() -> None:
    op.drop_index("ix_scheduler_runs_task_name", table_name="scheduler_runs")
    op.drop_table("scheduler_runs")
    op.drop_index("ix_notification_send_logs_status", table_name="notification_send_logs")
    op.drop_table("notification_send_logs")
