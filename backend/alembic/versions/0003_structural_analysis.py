"""Add persisted Stage 3 Python structural analysis.

Revision ID: 0003_structural_analysis
Revises: 0002_repository_onboarding
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_structural_analysis"
down_revision: str | None = "0002_repository_onboarding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_artifacts_id_version", "artifacts", ["id", "project_version_id"]
    )
    op.create_table(
        "structural_analyses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("parser_version", sa.String(64), nullable=False),
        sa.Column("runtime_version", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(32), nullable=False),
        sa.Column("limits", postgresql.JSONB(), nullable=False),
        sa.Column("summary", postgresql.JSONB(), nullable=False),
        sa.Column("warnings", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_version_id"], ["project_versions.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("project_version_id", name="uq_structural_analyses_version"),
        sa.UniqueConstraint("id", "project_version_id", name="uq_structural_analyses_id_version"),
        sa.CheckConstraint(
            "outcome IN ('complete', 'partial', 'failed')", name="ck_structural_analyses_outcome"
        ),
        sa.CheckConstraint("completed_at >= started_at", name="ck_structural_analyses_times"),
    )
    op.create_table(
        "artifact_analyses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("structural_analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("read_started_at", sa.DateTime(timezone=True)),
        sa.Column("read_completed_at", sa.DateTime(timezone=True)),
        sa.Column("encoding", sa.String(64)),
        sa.Column("line_count", sa.Integer()),
        sa.Column("is_package_initializer", sa.Boolean()),
        sa.Column("diagnostics", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["structural_analysis_id", "project_version_id"],
            ["structural_analyses.id", "structural_analyses.project_version_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id", "project_version_id"],
            ["artifacts.id", "artifacts.project_version_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "structural_analysis_id", "artifact_id", name="uq_artifact_analyses_analysis_artifact"
        ),
        sa.UniqueConstraint(
            "id", "structural_analysis_id", name="uq_artifact_analyses_id_analysis"
        ),
        sa.CheckConstraint(
            "outcome IN ('parsed', 'failed', 'skipped')", name="ck_artifact_analyses_outcome"
        ),
        sa.CheckConstraint(
            "line_count IS NULL OR line_count >= 0", name="ck_artifact_analyses_lines"
        ),
    )
    op.create_table(
        "symbols",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("artifact_analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("structural_analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True)),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("qualified_name", sa.Text(), nullable=False),
        sa.Column("is_async", sa.Boolean(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("start_column", sa.Integer()),
        sa.Column("end_column", sa.Integer()),
        sa.Column("full_start_line", sa.Integer()),
        sa.Column("signature", postgresql.JSONB()),
        sa.Column("decorators", postgresql.JSONB(), nullable=False),
        sa.Column("type_parameters", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["artifact_analysis_id", "structural_analysis_id"],
            ["artifact_analyses.id", "artifact_analyses.structural_analysis_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id", "artifact_analysis_id"],
            ["symbols.id", "symbols.artifact_analysis_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("id", "artifact_analysis_id", name="uq_symbols_id_artifact_analysis"),
        sa.CheckConstraint("kind IN ('function', 'class', 'method')", name="ck_symbols_kind"),
        sa.CheckConstraint("ordinal >= 0", name="ck_symbols_ordinal"),
        sa.CheckConstraint(
            "parent_id IS NULL OR parent_id <> id", name="ck_symbols_not_self_parent"
        ),
    )
    op.create_table(
        "import_references",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("artifact_analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("structural_analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_symbol_id", postgresql.UUID(as_uuid=True)),
        sa.Column("statement_ordinal", sa.Integer(), nullable=False),
        sa.Column("item_ordinal", sa.Integer(), nullable=False),
        sa.Column("form", sa.String(16), nullable=False),
        sa.Column("from_module", sa.Text()),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("alias", sa.Text()),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("start_column", sa.Integer()),
        sa.Column("end_column", sa.Integer()),
        sa.Column("resolution_status", sa.String(16), nullable=False),
        sa.Column("resolution_reason", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(
            ["artifact_analysis_id", "structural_analysis_id"],
            ["artifact_analyses.id", "artifact_analyses.structural_analysis_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_symbol_id", "artifact_analysis_id"],
            ["symbols.id", "symbols.artifact_analysis_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("form IN ('import', 'from')", name="ck_import_references_form"),
        sa.CheckConstraint(
            "resolution_status = 'unresolved'", name="ck_import_references_unresolved"
        ),
        sa.CheckConstraint(
            "statement_ordinal >= 0 AND item_ordinal >= 0", name="ck_import_references_ordinals"
        ),
    )
    for table, columns, name in [
        ("structural_analyses", ["project_version_id"], "ix_structural_analyses_version"),
        ("artifact_analyses", ["structural_analysis_id", "locator"], "ix_artifact_analyses_order"),
        ("symbols", ["artifact_analysis_id", "start_line", "ordinal"], "ix_symbols_order"),
        (
            "import_references",
            ["artifact_analysis_id", "start_line", "statement_ordinal", "item_ordinal"],
            "ix_import_references_order",
        ),
    ]:
        op.create_index(name, table, columns)


def downgrade() -> None:
    for table, name in [
        ("import_references", "ix_import_references_order"),
        ("symbols", "ix_symbols_order"),
        ("artifact_analyses", "ix_artifact_analyses_order"),
        ("structural_analyses", "ix_structural_analyses_version"),
    ]:
        op.drop_index(name, table_name=table)
    op.drop_table("import_references")
    op.drop_table("symbols")
    op.drop_table("artifact_analyses")
    op.drop_table("structural_analyses")
    op.drop_constraint("uq_artifacts_id_version", "artifacts", type_="unique")
