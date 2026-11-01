"""Stage 15 Milestone B: module facts for flow evidence, and immutable flow model runs.

Additive only: new tables plus one nullable pointer column on ``project_versions`` (INV-3).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0018_flow_model"
down_revision = "0017_flow_evidence"
branch_labels = None
depends_on = None

_HEX = "~ '^[0-9a-f]{64}$'"
_JSON = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "flow_module_facts",
        sa.Column("flow_evidence_run_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("facts", _JSON, nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.CheckConstraint("kind IN ('tsconfig', 'package')", name="ck_flow_module_facts_kind"),
        sa.CheckConstraint(f"input_fingerprint {_HEX}", name="ck_flow_module_facts_fingerprint"),
        sa.ForeignKeyConstraint(
            ["flow_evidence_run_id"],
            ["flow_evidence_runs.id"],
            name="fk_flow_module_facts_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("flow_evidence_run_id", "ordinal"),
    )
    op.create_table(
        "flow_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("project_version_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("flow_evidence_run_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_digest", sa.String(length=64), nullable=False),
        sa.Column("catalog_run_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_digest", sa.String(length=64), nullable=False),
        sa.Column("architecture_run_id", sa.Uuid(), nullable=False),
        sa.Column("architecture_graph_digest", sa.String(length=64), nullable=False),
        sa.Column("structural_analysis_id", sa.Uuid(), nullable=False),
        sa.Column("contract", _JSON, nullable=False),
        sa.Column("contract_digest", sa.String(length=64), nullable=False),
        sa.Column("input_authority_digest", sa.String(length=64), nullable=False),
        sa.Column("graph_digest", sa.String(length=64), nullable=True),
        sa.Column("portable_digest", sa.String(length=64), nullable=True),
        sa.Column("coverage", _JSON, nullable=False),
        sa.Column("diagnostics", _JSON, nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("superseded", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("captured_active_flow_run_id", sa.Uuid(), nullable=True),
        sa.Column("captured_latest_revision", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "outcome IN ('complete', 'partial', 'failed')", name="ck_flow_runs_outcome"
        ),
        sa.CheckConstraint("completed_at >= started_at", name="ck_flow_runs_times"),
        sa.CheckConstraint("revision > 0", name="ck_flow_runs_revision"),
        sa.CheckConstraint(
            "(outcome = 'failed') = (graph_digest IS NULL) AND "
            "(graph_digest IS NULL) = (portable_digest IS NULL) AND "
            "(outcome = 'failed') = (failure_code IS NOT NULL) AND "
            f"(graph_digest IS NULL OR (graph_digest {_HEX} AND portable_digest {_HEX}))",
            name="ck_flow_runs_digests",
        ),
        sa.CheckConstraint(
            f"contract_digest {_HEX} AND input_authority_digest {_HEX} AND "
            f"evidence_digest {_HEX} AND catalog_digest {_HEX} AND architecture_graph_digest {_HEX}",  # noqa: E501
            name="ck_flow_runs_authority",
        ),
        sa.ForeignKeyConstraint(
            ["project_version_id", "project_id"],
            ["project_versions.id", "project_versions.project_id"],
            name="fk_flow_run_snapshot_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["flow_evidence_run_id", "structural_analysis_id"],
            ["flow_evidence_runs.id", "flow_evidence_runs.structural_analysis_id"],
            name="fk_flow_run_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["catalog_run_id", "project_version_id"],
            ["semantic_catalog_runs.id", "semantic_catalog_runs.project_version_id"],
            name="fk_flow_run_catalog",
        ),
        sa.ForeignKeyConstraint(
            ["architecture_run_id", "project_version_id"],
            ["architecture_runs.id", "architecture_runs.project_version_id"],
            name="fk_flow_run_architecture",
        ),
        sa.ForeignKeyConstraint(
            ["captured_active_flow_run_id", "project_version_id"],
            ["flow_runs.id", "flow_runs.project_version_id"],
            name="fk_flow_runs_captured_active",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "project_version_id", name="uq_flow_runs_id_snapshot"),
        sa.UniqueConstraint(
            "project_version_id", "revision", name="uq_flow_runs_snapshot_revision"
        ),
    )
    op.create_table(
        "flow_evidence_items",
        sa.Column("flow_run_id", sa.Uuid(), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("refs", _JSON, nullable=False),
        sa.Column("derived", _JSON, nullable=False),
        sa.CheckConstraint(f"digest {_HEX}", name="ck_flow_evidence_items_digest"),
        sa.ForeignKeyConstraint(
            ["flow_run_id"], ["flow_runs.id"], name="fk_flow_evidence_items_run", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("flow_run_id", "digest"),
    )
    op.create_table(
        "flow_nodes",
        sa.Column("flow_run_id", sa.Uuid(), nullable=False),
        sa.Column("node_key", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("locator", sa.Text(), nullable=True),
        sa.Column("attributes", _JSON, nullable=False),
        sa.Column("evidence", _JSON, nullable=False),
        sa.Column("node_digest", sa.String(length=64), nullable=False),
        sa.CheckConstraint(f"node_digest {_HEX}", name="ck_flow_nodes_digest"),
        sa.ForeignKeyConstraint(
            ["flow_run_id"], ["flow_runs.id"], name="fk_flow_nodes_run", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("flow_run_id", "node_key"),
    )
    op.create_table(
        "flow_edges",
        sa.Column("flow_run_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("edge_key", sa.Text(), nullable=False),
        sa.Column("relation", sa.String(length=32), nullable=False),
        sa.Column("family", sa.String(length=16), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("target_key", sa.Text(), nullable=True),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("rule", sa.String(length=64), nullable=False),
        sa.Column("conditions", _JSON, nullable=False),
        sa.Column("attributes", _JSON, nullable=False),
        sa.Column("evidence", _JSON, nullable=False),
        sa.Column("edge_digest", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "tier IN ('observed', 'derived', 'inferred', 'unknown')", name="ck_flow_edges_tier"
        ),
        sa.CheckConstraint(
            "family IN ('resolution', 'invocation', 'effect', 'response')",
            name="ck_flow_edges_family",
        ),
        sa.CheckConstraint(f"edge_digest {_HEX}", name="ck_flow_edges_digest"),
        sa.ForeignKeyConstraint(
            ["flow_run_id"], ["flow_runs.id"], name="fk_flow_edges_run", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("flow_run_id", "ordinal"),
        sa.UniqueConstraint("flow_run_id", "edge_key", name="uq_flow_edges_key"),
    )
    op.create_index(
        "ix_flow_edges_source", "flow_edges", ["flow_run_id", "source_key"], unique=False
    )
    op.create_index(
        "ix_flow_edges_target", "flow_edges", ["flow_run_id", "target_key"], unique=False
    )
    op.create_table(
        "flow_diagnostics",
        sa.Column("flow_run_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("attributes", _JSON, nullable=False),
        sa.Column("diagnostic_digest", sa.String(length=64), nullable=False),
        sa.CheckConstraint(f"diagnostic_digest {_HEX}", name="ck_flow_diagnostics_digest"),
        sa.ForeignKeyConstraint(
            ["flow_run_id"], ["flow_runs.id"], name="fk_flow_diagnostics_run", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("flow_run_id", "ordinal"),
    )
    op.create_table(
        "flow_trace_records",
        sa.Column("flow_run_id", sa.Uuid(), nullable=False),
        sa.Column("trace_digest", sa.String(length=64), nullable=False),
        sa.Column("query", _JSON, nullable=False),
        sa.Column("result", _JSON, nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(f"trace_digest {_HEX}", name="ck_flow_trace_records_digest"),
        sa.CheckConstraint(
            "reason IN ('pinned', 'explanation')", name="ck_flow_trace_records_reason"
        ),
        sa.ForeignKeyConstraint(
            ["flow_run_id"], ["flow_runs.id"], name="fk_flow_trace_records_run", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("flow_run_id", "trace_digest"),
    )
    op.add_column("project_versions", sa.Column("active_flow_run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_project_versions_active_flow",
        "project_versions",
        "flow_runs",
        ["active_flow_run_id", "id"],
        ["id", "project_version_id"],
        initially="DEFERRED",
        deferrable=True,
    )


def downgrade() -> None:
    used = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT (SELECT count(*) FROM flow_runs) + (SELECT count(*) FROM flow_module_facts)"
            )
        )
        .scalar_one()
    )
    if used:
        raise RuntimeError(
            "Stage 15 flow model runs or module facts exist; downgrading would discard them. "
            "Restore a verified pre-upgrade backup instead."
        )
    op.drop_constraint("fk_project_versions_active_flow", "project_versions", type_="foreignkey")
    op.drop_column("project_versions", "active_flow_run_id")
    op.drop_table("flow_trace_records")
    op.drop_table("flow_diagnostics")
    op.drop_index("ix_flow_edges_target", table_name="flow_edges")
    op.drop_index("ix_flow_edges_source", table_name="flow_edges")
    op.drop_table("flow_edges")
    op.drop_table("flow_nodes")
    op.drop_table("flow_evidence_items")
    op.drop_table("flow_runs")
    op.drop_table("flow_module_facts")
