"""audit_logs 审计日志表

Revision ID: 0001
Revises:
Create Date: 2026-10-01

表结构为契约冻结项：本迁移与 app/repository/models.py 的 AuditLog 模型必须
保持一致（由 tests/test_migrations.py 列名一致性断言守护）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_key", sa.String(length=64), nullable=True),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("client_ip", sa.String(length=45), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
        sa.UniqueConstraint("request_key", name="uq_audit_logs_request_key"),
    )
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"], unique=False)
    op.create_index("ix_audit_logs_actor_id", "audit_logs", ["actor_id"], unique=False)
    op.create_index("ix_audit_logs_actor_type", "audit_logs", ["actor_type"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_audit_logs_actor_type", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_table("audit_logs")