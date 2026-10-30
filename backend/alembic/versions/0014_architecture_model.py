"""Stage 14 Milestone A: architecture model (additive; projection of the semantic catalog)."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0014_architecture_model"
down_revision = "0013_semantic_config_outcome"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "architecture_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("project_version_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("catalog_run_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_digest", sa.String(length=64), nullable=False),
        sa.Column("structural_analysis_id", sa.Uuid(), nullable=False),
        sa.Column("structural_revision", sa.Integer(), nullable=False),
        sa.Column("architecture_contract", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("contract_digest", sa.String(length=64), nullable=False),
        sa.Column("input_authority_digest", sa.String(length=64), nullable=False),
        sa.Column("graph_digest", sa.String(length=64), nullable=True),
        sa.Column("architecture_digest", sa.String(length=64), nullable=True),
        sa.Column("portable_digest", sa.String(length=64), nullable=True),
        sa.Column("coverage", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("diagnostics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("superseded", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("quarantined", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("captured_active_architecture_run_id", sa.Uuid(), nullable=True),
        sa.Column("captured_latest_revision", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint(
            (
                "(outcome = 'failed') = (graph_digest IS NULL) AND (graph_digest IS "
                "NULL) = (architecture_digest IS NULL) AND (graph_digest IS NULL) = "
                "(portable_digest IS NULL) AND (graph_digest IS NULL OR (graph_digest ~ "
                "'^[0-9a-f]{64}$' AND architecture_digest ~ '^[0-9a-f]{64}$' AND "
                "portable_digest ~ '^[0-9a-f]{64}$'))"
            ),
            name="ck_architecture_runs_digests",
        ),
        sa.CheckConstraint(
            (
                "input_authority_digest ~ '^[0-9a-f]{64}$' AND contract_digest ~ "
                "'^[0-9a-f]{64}$' AND catalog_digest ~ '^[0-9a-f]{64}$'"
            ),
            name="ck_architecture_runs_authority",
        ),
        sa.CheckConstraint(
            "outcome IN ('complete', 'partial', 'failed')", name="ck_architecture_runs_outcome"
        ),
        sa.CheckConstraint("completed_at >= started_at", name="ck_architecture_runs_times"),
        sa.CheckConstraint("revision > 0", name="ck_architecture_runs_revision"),
        sa.ForeignKeyConstraint(
            ["captured_active_architecture_run_id", "project_version_id"],
            ["architecture_runs.id", "architecture_runs.project_version_id"],
            name="fk_architecture_runs_captured_active",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["catalog_run_id", "project_version_id"],
            ["semantic_catalog_runs.id", "semantic_catalog_runs.project_version_id"],
            name="fk_architecture_run_catalog",
        ),
        sa.ForeignKeyConstraint(
            ["project_version_id", "project_id"],
            ["project_versions.id", "project_versions.project_id"],
            name="fk_architecture_run_snapshot_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["structural_analysis_id", "project_version_id"],
            ["structural_analyses.id", "structural_analyses.project_version_id"],
            name="fk_architecture_run_structure",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "catalog_run_id", name="uq_architecture_runs_id_catalog"),
        sa.UniqueConstraint("id", "project_version_id", name="uq_architecture_runs_id_snapshot"),
        sa.UniqueConstraint(
            "project_version_id", "revision", name="uq_architecture_runs_snapshot_revision"
        ),
    )
    op.create_index(
        "ix_architecture_runs_snapshot",
        "architecture_runs",
        ["project_version_id", "revision"],
        unique=False,
    )
    op.create_table(
        "architecture_diagnostics",
        sa.Column("architecture_run_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("diagnostic_digest", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "diagnostic_digest ~ '^[0-9a-f]{64}$'", name="ck_architecture_diagnostics_digest"
        ),
        sa.CheckConstraint(
            "tier IN ('observed', 'derived', 'inferred', 'unknown')",
            name="ck_architecture_diagnostics_tier",
        ),
        sa.ForeignKeyConstraint(
            ["architecture_run_id"], ["architecture_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("architecture_run_id", "ordinal"),
    )
    op.create_table(
        "architecture_evidence",
        sa.Column("architecture_run_id", sa.Uuid(), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("derived", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("renderer_version", sa.String(length=64), nullable=False),
        sa.Column("rendered_text", sa.Text(), nullable=False),
        sa.CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="ck_architecture_evidence_digest"),
        sa.CheckConstraint(
            (
                "jsonb_typeof(source_refs) = 'array' AND (kind = 'derivation' OR "
                "jsonb_array_length(source_refs) > 0)"
            ),
            name="ck_architecture_evidence_refs",
        ),
        sa.CheckConstraint(
            "kind IN ('import_statements', 'inventory_artifact', 'derivation')",
            name="ck_architecture_evidence_kind",
        ),
        sa.ForeignKeyConstraint(
            ["architecture_run_id"], ["architecture_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("architecture_run_id", "digest"),
    )
    op.create_table(
        "architecture_nodes",
        sa.Column("architecture_run_id", sa.Uuid(), nullable=False),
        sa.Column("node_key", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("parent_key", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("locator", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=64), nullable=True),
        sa.Column("catalog_entity_key", sa.Text(), nullable=True),
        sa.Column("artifact_identity_id", sa.Uuid(), nullable=True),
        sa.Column("coverage_state", sa.String(length=32), nullable=False),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("node_digest", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            (
                "coverage_state IN ('analysed', 'partial', 'not_current', "
                "'structure_failed', 'not_analysed', 'not_code', 'derived')"
            ),
            name="ck_architecture_nodes_coverage",
        ),
        sa.CheckConstraint(
            (
                "kind IN ('workspace', 'container', 'module', 'external_package', "
                "'router', 'api_endpoint', 'client_call', 'external_destination', "
                "'db_table', 'frontend_route', 'layer_candidate', 'functional_slice')"
            ),
            name="ck_architecture_nodes_kind",
        ),
        sa.CheckConstraint("node_digest ~ '^[0-9a-f]{64}$'", name="ck_architecture_nodes_digest"),
        sa.ForeignKeyConstraint(
            ["architecture_run_id", "parent_key"],
            ["architecture_nodes.architecture_run_id", "architecture_nodes.node_key"],
            name="fk_architecture_node_parent",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["architecture_run_id"], ["architecture_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("architecture_run_id", "node_key"),
        sa.UniqueConstraint("architecture_run_id", "ordinal", name="uq_architecture_nodes_ordinal"),
    )
    op.create_table(
        "architecture_components",
        sa.Column("architecture_run_id", sa.Uuid(), nullable=False),
        sa.Column("component_key", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("class_set", sa.String(length=4), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=True),
        sa.Column("witness", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("component_digest", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "class_set IN ('S0', 'S1', 'S2', 'S3', 'S4')", name="ck_architecture_components_set"
        ),
        sa.CheckConstraint(
            "component_digest ~ '^[0-9a-f]{64}$'", name="ck_architecture_components_digest"
        ),
        sa.CheckConstraint(
            "level IN ('module', 'container')", name="ck_architecture_components_level"
        ),
        sa.ForeignKeyConstraint(
            ["architecture_run_id", "scope_key"],
            ["architecture_nodes.architecture_run_id", "architecture_nodes.node_key"],
            name="fk_architecture_component_scope",
        ),
        sa.ForeignKeyConstraint(
            ["architecture_run_id"], ["architecture_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("architecture_run_id", "component_key"),
        sa.UniqueConstraint(
            "architecture_run_id", "ordinal", name="uq_architecture_components_ordinal"
        ),
    )
    op.create_table(
        "architecture_diagnostic_subjects",
        sa.Column("architecture_run_id", sa.Uuid(), nullable=False),
        sa.Column("diagnostic_ordinal", sa.Integer(), nullable=False),
        sa.Column("node_key", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["architecture_run_id", "diagnostic_ordinal"],
            ["architecture_diagnostics.architecture_run_id", "architecture_diagnostics.ordinal"],
            name="fk_architecture_subject_diagnostic",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["architecture_run_id", "node_key"],
            ["architecture_nodes.architecture_run_id", "architecture_nodes.node_key"],
            name="fk_architecture_subject_node",
        ),
        sa.PrimaryKeyConstraint("architecture_run_id", "diagnostic_ordinal", "node_key"),
    )
    op.create_table(
        "architecture_edges",
        sa.Column("architecture_run_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("edge_key", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("target_key", sa.Text(), nullable=True),
        sa.Column("target_text", sa.Text(), nullable=True),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("resolution_status", sa.String(length=16), nullable=True),
        sa.Column("import_class", sa.String(length=32), nullable=True),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("conditions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("edge_digest", sa.String(length=64), nullable=False),
        sa.CheckConstraint("edge_digest ~ '^[0-9a-f]{64}$'", name="ck_architecture_edges_digest"),
        sa.CheckConstraint(
            (
                "import_class IS NULL OR import_class IN ('unconditional', "
                "'presumed_unconditional', 'conditional', 'elision_dependent', "
                "'type_level', 'deferred')"
            ),
            name="ck_architecture_edges_class",
        ),
        sa.CheckConstraint(
            (
                "kind IN ('imports', 'reexports', 'depends_on', 'defines_router', "
                "'mounts_router', 'defines_endpoint', 'issues_request', "
                "'calls_endpoint', 'targets_destination', 'maps_table', "
                "'migrates_table', 'references_table', 'uses_model', "
                "'declares_dependency', 'routes_to', 'member_of')"
            ),
            name="ck_architecture_edges_kind",
        ),
        sa.CheckConstraint(
            (
                "resolution_status IS NULL OR resolution_status IN ('resolved', "
                "'ambiguous', 'not_found', 'external', 'not_attempted')"
            ),
            name="ck_architecture_edges_resolution",
        ),
        sa.CheckConstraint(
            "tier IN ('observed', 'derived', 'inferred', 'unknown')",
            name="ck_architecture_edges_tier",
        ),
        sa.CheckConstraint(
            "(target_key IS NULL) <> (target_text IS NULL)", name="ck_architecture_edges_target"
        ),
        sa.CheckConstraint("count > 0", name="ck_architecture_edges_count"),
        sa.ForeignKeyConstraint(
            ["architecture_run_id", "source_key"],
            ["architecture_nodes.architecture_run_id", "architecture_nodes.node_key"],
            name="fk_architecture_edge_source",
        ),
        sa.ForeignKeyConstraint(
            ["architecture_run_id", "target_key"],
            ["architecture_nodes.architecture_run_id", "architecture_nodes.node_key"],
            name="fk_architecture_edge_target",
        ),
        sa.ForeignKeyConstraint(
            ["architecture_run_id"], ["architecture_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("architecture_run_id", "ordinal"),
        sa.UniqueConstraint("architecture_run_id", "edge_key", name="uq_architecture_edges_key"),
    )
    op.create_index(
        "ix_architecture_edges_source",
        "architecture_edges",
        ["architecture_run_id", "source_key"],
        unique=False,
    )
    op.create_index(
        "ix_architecture_edges_target",
        "architecture_edges",
        ["architecture_run_id", "target_key"],
        unique=False,
    )
    op.create_table(
        "architecture_component_members",
        sa.Column("architecture_run_id", sa.Uuid(), nullable=False),
        sa.Column("component_key", sa.Text(), nullable=False),
        sa.Column("node_key", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["architecture_run_id", "component_key"],
            [
                "architecture_components.architecture_run_id",
                "architecture_components.component_key",
            ],
            name="fk_architecture_member_component",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["architecture_run_id", "node_key"],
            ["architecture_nodes.architecture_run_id", "architecture_nodes.node_key"],
            name="fk_architecture_member_node",
        ),
        sa.PrimaryKeyConstraint("architecture_run_id", "component_key", "node_key"),
    )
    op.create_table(
        "architecture_evidence_links",
        sa.Column("architecture_run_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("node_key", sa.Text(), nullable=True),
        sa.Column("edge_ordinal", sa.Integer(), nullable=True),
        sa.Column("component_key", sa.Text(), nullable=True),
        sa.Column("diagnostic_ordinal", sa.Integer(), nullable=True),
        sa.Column("evidence_digest", sa.String(length=64), nullable=True),
        sa.Column("catalog_run_id", sa.Uuid(), nullable=True),
        sa.Column("catalog_fact_digest", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            (
                "(evidence_digest IS NULL) <> (catalog_fact_digest IS NULL) AND "
                "(catalog_fact_digest IS NULL) = (catalog_run_id IS NULL)"
            ),
            name="ck_architecture_links_target",
        ),
        sa.CheckConstraint(
            "num_nonnulls(node_key, edge_ordinal, component_key, diagnostic_ordinal) = 1",
            name="ck_architecture_links_owner",
        ),
        sa.ForeignKeyConstraint(
            ["architecture_run_id", "catalog_run_id"],
            ["architecture_runs.id", "architecture_runs.catalog_run_id"],
            name="fk_architecture_link_catalog_run",
        ),
        sa.ForeignKeyConstraint(
            ["architecture_run_id", "component_key"],
            [
                "architecture_components.architecture_run_id",
                "architecture_components.component_key",
            ],
            name="fk_architecture_link_component",
        ),
        sa.ForeignKeyConstraint(
            ["architecture_run_id", "diagnostic_ordinal"],
            ["architecture_diagnostics.architecture_run_id", "architecture_diagnostics.ordinal"],
            name="fk_architecture_link_diagnostic",
        ),
        sa.ForeignKeyConstraint(
            ["architecture_run_id", "edge_ordinal"],
            ["architecture_edges.architecture_run_id", "architecture_edges.ordinal"],
            name="fk_architecture_link_edge",
        ),
        sa.ForeignKeyConstraint(
            ["architecture_run_id", "evidence_digest"],
            ["architecture_evidence.architecture_run_id", "architecture_evidence.digest"],
            name="fk_architecture_link_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["architecture_run_id", "node_key"],
            ["architecture_nodes.architecture_run_id", "architecture_nodes.node_key"],
            name="fk_architecture_link_node",
        ),
        sa.ForeignKeyConstraint(
            ["architecture_run_id"], ["architecture_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["catalog_run_id", "catalog_fact_digest"],
            ["semantic_facts.catalog_run_id", "semantic_facts.fact_digest"],
            name="fk_architecture_link_catalog_fact",
        ),
        sa.PrimaryKeyConstraint("architecture_run_id", "ordinal"),
    )
    op.create_index(
        "ix_architecture_links_edge",
        "architecture_evidence_links",
        ["architecture_run_id", "edge_ordinal"],
        unique=False,
    )
    op.create_index(
        "ix_architecture_links_node",
        "architecture_evidence_links",
        ["architecture_run_id", "node_key"],
        unique=False,
    )
    op.add_column(
        "project_versions", sa.Column("active_architecture_run_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_project_versions_active_architecture",
        "project_versions",
        "architecture_runs",
        ["active_architecture_run_id", "id"],
        ["id", "project_version_id"],
        initially="DEFERRED",
        deferrable=True,
    )


def downgrade() -> None:
    used = op.get_bind().execute(sa.text("SELECT count(*) FROM architecture_runs")).scalar_one()
    if used:
        raise RuntimeError(
            "Stage 14 architecture runs exist; downgrading would discard them. "
            "Restore a verified pre-upgrade backup instead."
        )
    op.drop_constraint(
        "fk_project_versions_active_architecture", "project_versions", type_="foreignkey"
    )
    op.drop_column("project_versions", "active_architecture_run_id")
    op.drop_index("ix_architecture_links_node", table_name="architecture_evidence_links")
    op.drop_index("ix_architecture_links_edge", table_name="architecture_evidence_links")
    op.drop_table("architecture_evidence_links")
    op.drop_table("architecture_component_members")
    op.drop_index("ix_architecture_edges_target", table_name="architecture_edges")
    op.drop_index("ix_architecture_edges_source", table_name="architecture_edges")
    op.drop_table("architecture_edges")
    op.drop_table("architecture_diagnostic_subjects")
    op.drop_table("architecture_components")
    op.drop_table("architecture_nodes")
    op.drop_table("architecture_evidence")
    op.drop_table("architecture_diagnostics")
    op.drop_index("ix_architecture_runs_snapshot", table_name="architecture_runs")
    op.drop_table("architecture_runs")
