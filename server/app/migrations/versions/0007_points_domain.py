"""积分域表：point_accounts + point_ledgers + stage_grants（SP2-4 积分服务）

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "point_accounts",
        sa.Column("id", sa.String(length=36), nullable=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("purchased_balance", sa.Integer(), nullable=False),
        sa.Column("monthly_balance", sa.Integer(), nullable=False),
        sa.Column("frozen", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", name="pk_point_accounts"),
    )

    op.create_table(
        "point_ledgers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("exec_id", sa.String(length=64), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("balance_type", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("stage", sa.String(length=16), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_point_ledgers"),
        sa.UniqueConstraint("user_id", "exec_id", "kind", name="uq_point_ledgers_exec"),
    )
    op.create_index("ix_point_ledgers_user_id", "point_ledgers", ["user_id"])
    op.create_index("ix_point_ledgers_exec_id", "point_ledgers", ["exec_id"])

    op.create_table(
        "stage_grants",
        sa.Column("id", sa.String(length=36), nullable=True),
        sa.Column("exec_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("stage", sa.String(length=16), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("signature", sa.String(length=128), nullable=False),
        sa.Column("key_version", sa.String(length=16), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("exec_id", name="pk_stage_grants"),
    )
    op.create_index("ix_stage_grants_user_id", "stage_grants", ["user_id"])
    op.create_index("ix_stage_grants_expires_at", "stage_grants", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_stage_grants_expires_at", table_name="stage_grants")
    op.drop_index("ix_stage_grants_user_id", table_name="stage_grants")
    op.drop_table("stage_grants")

    op.drop_index("ix_point_ledgers_exec_id", table_name="point_ledgers")
    op.drop_index("ix_point_ledgers_user_id", table_name="point_ledgers")
    op.drop_table("point_ledgers")

    op.drop_table("point_accounts")
