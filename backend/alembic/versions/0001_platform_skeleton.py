"""Create Stage 1 project persistence tables.

Revision ID: 0001_platform_skeleton
Revises:
Create Date: 2026-09-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_platform_skeleton"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("char_length(trim(name)) > 0", name="ck_projects_name_not_blank"),
        sa.CheckConstraint(
            "char_length(trim(root_path)) > 0",
            name="ck_projects_root_path_not_blank",
        ),
        sa.UniqueConstraint("root_path", name="uq_projects_root_path"),
    )
    op.create_table(
        "project_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_project_versions_project_id", "project_versions", ["project_id"])
    op.create_table(
        "artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("project_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=100), nullable=False),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("char_length(trim(kind)) > 0", name="ck_artifacts_kind_not_blank"),
        sa.CheckConstraint("char_length(trim(locator)) > 0", name="ck_artifacts_locator_not_blank"),
        sa.ForeignKeyConstraint(
            ["project_version_id"], ["project_versions.id"], ondelete="CASCADE"
        ),
    )
    op.create_index("ix_artifacts_project_version_id", "artifacts", ["project_version_id"])


def downgrade() -> None:
    op.drop_index("ix_artifacts_project_version_id", table_name="artifacts")
    op.drop_table("artifacts")
    op.drop_index("ix_project_versions_project_id", table_name="project_versions")
    op.drop_table("project_versions")
    op.drop_table("projects")
