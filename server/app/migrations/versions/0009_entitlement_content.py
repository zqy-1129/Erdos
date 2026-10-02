"""权益快照与内容库域表（SP2-6 权益内容服务）

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entitlement_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("signature", sa.String(length=128), nullable=False),
        sa.Column("key_version", sa.String(length=16), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_entitlement_snapshots"),
        sa.UniqueConstraint("user_id", "issued_at", name="uq_entitlement_snapshots_issued"),
    )
    op.create_index("ix_entitlement_snapshots_user_id", "entitlement_snapshots", ["user_id"])

    op.create_table(
        "problems",
        sa.Column("id", sa.String(length=36), nullable=True),
        sa.Column("business_id", sa.String(length=64), nullable=False),
        sa.Column("competition", sa.String(length=16), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("problem_code", sa.String(length=8), nullable=False),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("prompt_zh", sa.String(length=8192), nullable=False),
        sa.Column("prompt_en", sa.String(length=8192), nullable=False),
        sa.Column("attachments", sa.JSON(), nullable=False),
        sa.Column("scoring", sa.String(length=2048), nullable=False),
        sa.Column("dataset_hint", sa.String(length=2048), nullable=False),
        sa.Column("visibility", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("business_id", name="pk_problems"),
    )

    op.create_table(
        "paper_templates",
        sa.Column("id", sa.String(length=36), nullable=True),
        sa.Column("business_id", sa.String(length=64), nullable=False),
        sa.Column("competition", sa.String(length=16), nullable=False),
        sa.Column("format", sa.String(length=16), nullable=False),
        sa.Column("oss_key", sa.String(length=256), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("changelog", sa.String(length=512), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("business_id", name="pk_paper_templates"),
    )

    op.create_table(
        "cases",
        sa.Column("id", sa.String(length=36), nullable=True),
        sa.Column("business_id", sa.String(length=64), nullable=False),
        sa.Column("problem_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("award", sa.String(length=32), nullable=False),
        sa.Column("method_tags", sa.JSON(), nullable=False),
        sa.Column("oss_key", sa.String(length=256), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("compliance_note", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("business_id", name="pk_cases"),
    )
    op.create_index("ix_cases_problem_id", "cases", ["problem_id"])

    op.create_table(
        "content_manifests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_content_manifests"),
        sa.UniqueConstraint("scope", name="uq_content_manifests_scope"),
    )


def downgrade() -> None:
    op.drop_table("content_manifests")
    op.drop_index("ix_cases_problem_id", table_name="cases")
    op.drop_table("cases")
    op.drop_table("paper_templates")
    op.drop_table("problems")
    op.drop_index("ix_entitlement_snapshots_user_id", table_name="entitlement_snapshots")
    op.drop_table("entitlement_snapshots")
