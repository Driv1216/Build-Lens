"""Add metadata-only repository snapshots.

Revision ID: 0002_repository_onboarding
Revises: 0001_platform_skeleton
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_repository_onboarding"
down_revision: str | None = "0001_platform_skeleton"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("project_versions", sa.Column("canonical_root", sa.Text(), nullable=True))
    op.add_column(
        "project_versions", sa.Column("scan_started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "project_versions",
        sa.Column("scan_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("project_versions", sa.Column("scan_status", sa.String(16), nullable=True))
    op.add_column("project_versions", sa.Column("policy_version", sa.String(32), nullable=True))
    op.add_column("project_versions", sa.Column("git_metadata", postgresql.JSONB(), nullable=True))
    op.add_column("project_versions", sa.Column("warnings", postgresql.JSONB(), nullable=True))
    op.add_column("project_versions", sa.Column("summary", postgresql.JSONB(), nullable=True))
    op.create_check_constraint(
        "ck_project_versions_stage2_status",
        "project_versions",
        "scan_status IS NULL OR scan_status IN ('complete', 'partial')",
    )
    op.create_check_constraint(
        "ck_project_versions_stage2_times",
        "project_versions",
        "scan_completed_at IS NULL OR scan_started_at IS NOT NULL "
        "AND scan_completed_at >= scan_started_at",
    )
    op.create_check_constraint(
        "ck_project_versions_stage2_together",
        "project_versions",
        "scan_status IS NULL OR (canonical_root IS NOT NULL "
        "AND scan_started_at IS NOT NULL AND scan_completed_at IS NOT NULL "
        "AND policy_version IS NOT NULL AND git_metadata IS NOT NULL "
        "AND warnings IS NOT NULL AND summary IS NOT NULL)",
    )
    op.create_index(
        "ix_project_versions_latest_snapshot",
        "project_versions",
        ["project_id", sa.text("scan_completed_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("scan_completed_at IS NOT NULL"),
    )
    op.add_column("artifacts", sa.Column("inventory_status", sa.String(16), nullable=True))
    op.add_column("artifacts", sa.Column("size_bytes", sa.BigInteger(), nullable=True))
    op.add_column("artifacts", sa.Column("language", sa.String(64), nullable=True))
    op.add_column("artifacts", sa.Column("tags", postgresql.ARRAY(sa.Text()), nullable=True))
    op.add_column("artifacts", sa.Column("content_hint", sa.String(16), nullable=True))
    op.add_column("artifacts", sa.Column("ignore_status", sa.String(16), nullable=True))
    op.add_column(
        "artifacts", sa.Column("exclusion_reasons", postgresql.ARRAY(sa.Text()), nullable=True)
    )
    op.add_column("artifacts", sa.Column("pruned", sa.Boolean(), nullable=True))
    op.create_check_constraint(
        "ck_artifacts_stage2_status",
        "artifacts",
        "inventory_status IS NULL OR inventory_status IN ('included', 'excluded')",
    )
    op.create_check_constraint(
        "ck_artifacts_stage2_size", "artifacts", "size_bytes IS NULL OR size_bytes >= 0"
    )
    op.create_check_constraint(
        "ck_artifacts_stage2_excluded_reason",
        "artifacts",
        "inventory_status IS DISTINCT FROM 'excluded' OR cardinality(exclusion_reasons) > 0",
    )
    op.create_check_constraint(
        "ck_artifacts_stage2_pruned", "artifacts", "pruned IS NOT TRUE OR kind = 'directory'"
    )
    op.create_check_constraint(
        "ck_artifacts_stage2_locator",
        "artifacts",
        "inventory_status IS NULL OR (locator <> '' "
        "AND locator !~ '(^|/)\\.\\.?(/|$)' AND octet_length(locator) <= 2048)",
    )
    op.create_index(
        "uq_artifacts_snapshot_locator",
        "artifacts",
        ["project_version_id", "locator"],
        unique=True,
        postgresql_where=sa.text("inventory_status IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_artifacts_snapshot_locator", table_name="artifacts")
    for name in [
        "ck_artifacts_stage2_locator",
        "ck_artifacts_stage2_pruned",
        "ck_artifacts_stage2_excluded_reason",
        "ck_artifacts_stage2_size",
        "ck_artifacts_stage2_status",
    ]:
        op.drop_constraint(name, "artifacts", type_="check")
    for name in [
        "pruned",
        "exclusion_reasons",
        "ignore_status",
        "content_hint",
        "tags",
        "language",
        "size_bytes",
        "inventory_status",
    ]:
        op.drop_column("artifacts", name)
    op.drop_index("ix_project_versions_latest_snapshot", table_name="project_versions")
    for name in [
        "ck_project_versions_stage2_together",
        "ck_project_versions_stage2_times",
        "ck_project_versions_stage2_status",
    ]:
        op.drop_constraint(name, "project_versions", type_="check")
    for name in [
        "summary",
        "warnings",
        "git_metadata",
        "policy_version",
        "scan_status",
        "scan_completed_at",
        "scan_started_at",
        "canonical_root",
    ]:
        op.drop_column("project_versions", name)
