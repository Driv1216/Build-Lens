"""Stage 15 Milestone A: flow evidence runs (additive; bound to one structural analysis).

New tables only, plus one nullable pointer column on ``project_versions``. No existing row,
column, constraint or pointer is changed (INV-3).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0017_flow_evidence"
down_revision = "0016_architecture_rules"
branch_labels = None
depends_on = None

_HEX = "~ '^[0-9a-f]{64}$'"


def _span_columns() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("start_line", sa.Integer(), nullable=True),
        sa.Column("end_line", sa.Integer(), nullable=True),
        sa.Column("start_column", sa.Integer(), nullable=True),
        sa.Column("end_column", sa.Integer(), nullable=True),
        sa.Column("start_offset", sa.Integer(), nullable=True),
        sa.Column("end_offset", sa.Integer(), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "flow_evidence_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("project_version_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("structural_analysis_id", sa.Uuid(), nullable=False),
        sa.Column("structural_revision", sa.Integer(), nullable=False),
        sa.Column("contract", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("contract_digest", sa.String(length=64), nullable=False),
        sa.Column("input_authority_digest", sa.String(length=64), nullable=False),
        sa.Column("evidence_digest", sa.String(length=64), nullable=True),
        sa.Column("coverage", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("diagnostics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("superseded", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("captured_active_flow_evidence_run_id", sa.Uuid(), nullable=True),
        sa.Column("captured_latest_revision", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "outcome IN ('complete', 'partial', 'failed')", name="ck_flow_evidence_runs_outcome"
        ),
        sa.CheckConstraint("completed_at >= started_at", name="ck_flow_evidence_runs_times"),
        sa.CheckConstraint("revision > 0", name="ck_flow_evidence_runs_revision"),
        sa.CheckConstraint(
            f"contract_digest {_HEX} AND input_authority_digest {_HEX} AND "
            f"(evidence_digest IS NULL OR evidence_digest {_HEX})",
            name="ck_flow_evidence_runs_digests",
        ),
        sa.CheckConstraint(
            "(outcome = 'failed') = (evidence_digest IS NULL) AND "
            "(outcome = 'failed') = (failure_code IS NOT NULL)",
            name="ck_flow_evidence_runs_failure",
        ),
        sa.ForeignKeyConstraint(
            ["project_version_id", "project_id"],
            ["project_versions.id", "project_versions.project_id"],
            name="fk_flow_evidence_run_snapshot_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["structural_analysis_id", "project_version_id"],
            ["structural_analyses.id", "structural_analyses.project_version_id"],
            name="fk_flow_evidence_run_structure",
        ),
        sa.ForeignKeyConstraint(
            ["captured_active_flow_evidence_run_id", "project_version_id"],
            ["flow_evidence_runs.id", "flow_evidence_runs.project_version_id"],
            name="fk_flow_evidence_runs_captured_active",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "project_version_id", name="uq_flow_evidence_runs_id_snapshot"),
        sa.UniqueConstraint(
            "id", "structural_analysis_id", name="uq_flow_evidence_runs_id_structure"
        ),
        sa.UniqueConstraint(
            "project_version_id", "revision", name="uq_flow_evidence_runs_snapshot_revision"
        ),
    )
    op.create_table(
        "flow_artifact_coverage",
        sa.Column("flow_evidence_run_id", sa.Uuid(), nullable=False),
        sa.Column("structural_analysis_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_analysis_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("extractor", sa.String(length=64), nullable=True),
        sa.Column("contract_digest", sa.String(length=64), nullable=True),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("execution_mode", sa.String(length=16), nullable=False),
        sa.Column("reuse_source_run_id", sa.Uuid(), nullable=True),
        sa.Column("evidence_digest", sa.String(length=64), nullable=True),
        sa.Column("scope_count", sa.Integer(), nullable=False),
        sa.Column("callsite_count", sa.Integer(), nullable=False),
        sa.Column("diagnostics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "state IN ('extracted', 'partial', 'failed', 'not_current', 'not_extracted', "
            "'not_applicable')",
            name="ck_flow_artifact_coverage_state",
        ),
        sa.CheckConstraint(
            "execution_mode IN ('extracted', 'reused', 'none')",
            name="ck_flow_artifact_coverage_mode",
        ),
        sa.CheckConstraint(
            "scope_count >= 0 AND callsite_count >= 0", name="ck_flow_artifact_coverage_counts"
        ),
        sa.CheckConstraint(
            "(state IN ('extracted', 'partial')) = (evidence_digest IS NOT NULL)",
            name="ck_flow_artifact_coverage_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["flow_evidence_run_id", "structural_analysis_id"],
            ["flow_evidence_runs.id", "flow_evidence_runs.structural_analysis_id"],
            name="fk_flow_artifact_coverage_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_analysis_id", "structural_analysis_id"],
            ["artifact_analyses.id", "artifact_analyses.structural_analysis_id"],
            name="fk_flow_artifact_coverage_artifact",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("flow_evidence_run_id", "artifact_analysis_id"),
    )
    op.create_table(
        "flow_scopes",
        sa.Column("flow_evidence_run_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_analysis_id", sa.Uuid(), nullable=False),
        sa.Column("scope_key", sa.String(length=512), nullable=False),
        sa.Column("parent_scope_key", sa.String(length=512), nullable=True),
        sa.Column("owner_symbol_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("callable_kind", sa.String(length=32), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        *_span_columns(),
        sa.Column("truncated", sa.String(length=64), nullable=True),
        sa.Column("cfg", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("scope_digest", sa.String(length=64), nullable=False),
        sa.CheckConstraint(f"scope_digest {_HEX}", name="ck_flow_scopes_digest"),
        sa.ForeignKeyConstraint(
            ["flow_evidence_run_id", "artifact_analysis_id"],
            [
                "flow_artifact_coverage.flow_evidence_run_id",
                "flow_artifact_coverage.artifact_analysis_id",
            ],
            name="fk_flow_scopes_coverage",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_symbol_id", "artifact_analysis_id"],
            ["symbols.id", "symbols.artifact_analysis_id"],
            name="fk_flow_scopes_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("flow_evidence_run_id", "artifact_analysis_id", "scope_key"),
    )
    op.create_index("ix_flow_scopes_owner", "flow_scopes", ["owner_symbol_id"], unique=False)
    op.create_table(
        "flow_callsites",
        sa.Column("flow_evidence_run_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_analysis_id", sa.Uuid(), nullable=False),
        sa.Column("scope_key", sa.String(length=512), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("cfg_node", sa.Integer(), nullable=False),
        sa.Column("copies", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("callee", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("awaited", sa.Boolean(), nullable=False),
        *_span_columns(),
        sa.Column("text_key", sa.String(length=600), nullable=True),
        sa.CheckConstraint("ordinal >= 0 AND cfg_node >= 0", name="ck_flow_callsites_ordinal"),
        sa.ForeignKeyConstraint(
            ["flow_evidence_run_id", "artifact_analysis_id", "scope_key"],
            [
                "flow_scopes.flow_evidence_run_id",
                "flow_scopes.artifact_analysis_id",
                "flow_scopes.scope_key",
            ],
            name="fk_flow_callsites_scope",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "flow_evidence_run_id", "artifact_analysis_id", "scope_key", "ordinal"
        ),
    )
    op.create_index(
        "ix_flow_callsites_position",
        "flow_callsites",
        ["flow_evidence_run_id", "artifact_analysis_id", "start_line"],
        unique=False,
    )
    op.create_table(
        "flow_jsx_bindings",
        sa.Column("flow_evidence_run_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_analysis_id", sa.Uuid(), nullable=False),
        sa.Column("scope_key", sa.String(length=512), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("cfg_node", sa.Integer(), nullable=False),
        sa.Column("element", sa.Text(), nullable=False),
        sa.Column("intrinsic", sa.Boolean(), nullable=False),
        sa.Column("attribute", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *_span_columns(),
        sa.CheckConstraint("ordinal >= 0 AND cfg_node >= 0", name="ck_flow_jsx_bindings_ordinal"),
        sa.ForeignKeyConstraint(
            ["flow_evidence_run_id", "artifact_analysis_id", "scope_key"],
            [
                "flow_scopes.flow_evidence_run_id",
                "flow_scopes.artifact_analysis_id",
                "flow_scopes.scope_key",
            ],
            name="fk_flow_jsx_bindings_scope",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "flow_evidence_run_id", "artifact_analysis_id", "scope_key", "ordinal"
        ),
    )
    op.create_table(
        "flow_display_texts",
        sa.Column("flow_evidence_run_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_analysis_id", sa.Uuid(), nullable=False),
        sa.Column("text_key", sa.String(length=600), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("flags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["flow_evidence_run_id", "artifact_analysis_id"],
            [
                "flow_artifact_coverage.flow_evidence_run_id",
                "flow_artifact_coverage.artifact_analysis_id",
            ],
            name="fk_flow_display_texts_coverage",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("flow_evidence_run_id", "artifact_analysis_id", "text_key"),
    )
    op.create_table(
        "flow_framework_facts",
        sa.Column("flow_evidence_run_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("ecosystem", sa.String(length=16), nullable=False),
        sa.Column("package", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=True),
        sa.Column("version_constraint", sa.String(length=256), nullable=True),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "ecosystem IN ('python', 'npm')", name="ck_flow_framework_facts_ecosystem"
        ),
        sa.CheckConstraint(
            "source_kind IN ('lockfile', 'requirements_pin', 'declared')",
            name="ck_flow_framework_facts_source",
        ),
        sa.CheckConstraint(f"input_fingerprint {_HEX}", name="ck_flow_framework_facts_fingerprint"),
        sa.ForeignKeyConstraint(
            ["flow_evidence_run_id"],
            ["flow_evidence_runs.id"],
            name="fk_flow_framework_facts_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("flow_evidence_run_id", "ordinal"),
    )
    op.add_column(
        "project_versions", sa.Column("active_flow_evidence_run_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_project_versions_active_flow_evidence",
        "project_versions",
        "flow_evidence_runs",
        ["active_flow_evidence_run_id", "id"],
        ["id", "project_version_id"],
        initially="DEFERRED",
        deferrable=True,
    )


def downgrade() -> None:
    used = op.get_bind().execute(sa.text("SELECT count(*) FROM flow_evidence_runs")).scalar_one()
    if used:
        raise RuntimeError(
            "Stage 15 flow evidence runs exist; downgrading would discard them. "
            "Restore a verified pre-upgrade backup instead."
        )
    op.drop_constraint(
        "fk_project_versions_active_flow_evidence", "project_versions", type_="foreignkey"
    )
    op.drop_column("project_versions", "active_flow_evidence_run_id")
    op.drop_table("flow_framework_facts")
    op.drop_table("flow_display_texts")
    op.drop_table("flow_jsx_bindings")
    op.drop_index("ix_flow_callsites_position", table_name="flow_callsites")
    op.drop_table("flow_callsites")
    op.drop_index("ix_flow_scopes_owner", table_name="flow_scopes")
    op.drop_table("flow_scopes")
    op.drop_table("flow_artifact_coverage")
    op.drop_table("flow_evidence_runs")
