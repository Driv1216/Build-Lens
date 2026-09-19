"""Bind identities and chunk symbols to their owning artifact evidence."""

import sqlalchemy as sa

from alembic import op

revision = "0007_evidence_ownership"
down_revision = "0006_foundation_repair"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = [
        ("artifacts", "project_id"),
        ("artifact_analyses", "artifact_identity_id"),
        ("symbols", "artifact_identity_id"),
        ("chunks", "structural_artifact_analysis_id"),
    ]
    for table, column in columns:
        op.add_column(table, sa.Column(column, sa.Uuid(), nullable=True))
    op.execute(
        "UPDATE artifacts a SET project_id = v.project_id FROM project_versions v WHERE "
        "v.id = a.project_version_id"
    )
    op.execute(
        "UPDATE artifact_analyses a SET artifact_identity_id = f.stable_identity_id FROM "
        "artifacts f WHERE f.id = a.artifact_id"
    )
    op.execute(
        "UPDATE symbols s SET artifact_identity_id = a.artifact_identity_id FROM "
        "artifact_analyses a WHERE a.id = s.artifact_analysis_id"
    )
    op.execute(
        "UPDATE chunks c SET structural_artifact_analysis_id = "
        "a.structural_artifact_analysis_id FROM artifact_processings a WHERE a.id = "
        "c.artifact_processing_id"
    )
    for name, table, columns in [
        ("uq_artifact_id_identity", "artifacts", ["id", "stable_identity_id"]),
        ("uq_analysis_id_identity", "artifact_analyses", ["id", "artifact_identity_id"]),
        (
            "uq_processing_id_structure",
            "artifact_processings",
            ["id", "structural_artifact_analysis_id"],
        ),
        (
            "uq_symbol_id_structure_identity",
            "symbols",
            ["id", "artifact_analysis_id", "stable_identity_id"],
        ),
    ]:
        op.create_unique_constraint(name, table, columns)
    keys = [
        (
            "fk_artifact_project",
            "artifacts",
            ["project_version_id", "project_id"],
            "project_versions",
            ["id", "project_id"],
        ),
        (
            "fk_artifact_identity_project",
            "artifacts",
            ["stable_identity_id", "project_id"],
            "artifact_identities",
            ["id", "project_id"],
        ),
        (
            "fk_analysis_artifact_identity",
            "artifact_analyses",
            ["artifact_id", "artifact_identity_id"],
            "artifacts",
            ["id", "stable_identity_id"],
        ),
        (
            "fk_symbol_artifact_identity",
            "symbols",
            ["artifact_analysis_id", "artifact_identity_id"],
            "artifact_analyses",
            ["id", "artifact_identity_id"],
        ),
        (
            "fk_symbol_identity_artifact",
            "symbols",
            ["stable_identity_id", "artifact_identity_id"],
            "symbol_identities",
            ["id", "artifact_identity_id"],
        ),
        (
            "fk_chunk_processing_structure",
            "chunks",
            ["artifact_processing_id", "structural_artifact_analysis_id"],
            "artifact_processings",
            ["id", "structural_artifact_analysis_id"],
        ),
        (
            "fk_chunk_symbol_owner",
            "chunks",
            ["symbol_id", "structural_artifact_analysis_id", "symbol_identity_id"],
            "symbols",
            ["id", "artifact_analysis_id", "stable_identity_id"],
        ),
        (
            "fk_structure_captured_active",
            "structural_analyses",
            ["captured_active_analysis_id", "project_version_id"],
            "structural_analyses",
            ["id", "project_version_id"],
        ),
        (
            "fk_processing_captured_active",
            "processing_runs",
            ["captured_active_processing_run_id", "project_version_id"],
            "processing_runs",
            ["id", "project_version_id"],
        ),
    ]
    for name, table, local, target, remote in keys:
        op.create_foreign_key(
            name,
            table,
            target,
            local,
            remote,
            deferrable=True,
            initially="DEFERRED",
            postgresql_not_valid=True,
        )
        conditions = " AND ".join(f'a."{c}" IS NOT NULL' for c in local)
        match = " AND ".join(
            f'a."{c}" = b."{other}"' for c, other in zip(local, remote, strict=True)
        )
        op.execute(f'''DO $$ BEGIN IF NOT EXISTS (
            SELECT 1 FROM "{table}" a WHERE {conditions} AND NOT EXISTS
            (SELECT 1 FROM "{target}" b WHERE {match})) THEN
            ALTER TABLE "{table}" VALIDATE CONSTRAINT "{name}"; END IF; END $$''')
    checks = [
        ("ck_artifact_project_required", "artifacts", "project_id IS NOT NULL"),
        (
            "ck_symbol_identity_required",
            "symbols",
            "stable_identity_id IS NOT NULL AND artifact_identity_id IS NOT NULL",
        ),
        (
            "ck_chunk_symbol_owner_required",
            "chunks",
            "(symbol_id IS NULL AND symbol_identity_id IS NULL) OR (symbol_id IS NOT NULL AND "
            "symbol_identity_id IS NOT NULL AND structural_artifact_analysis_id IS NOT NULL)",
        ),
    ]
    for name, table, condition in checks:
        op.create_check_constraint(name, table, condition, postgresql_not_valid=True)
        op.execute(f'''DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM "{table}" WHERE NOT ({condition}))
            THEN ALTER TABLE "{table}" VALIDATE CONSTRAINT "{name}"; END IF; END $$''')
    for name, table, columns in [
        ("ix_processing_artifact_lookup", "artifact_processings", ["artifact_id"]),
        ("ix_chunks_symbol_identity", "chunks", ["symbol_identity_id"]),
        ("ix_symbols_analysis", "symbols", ["structural_analysis_id"]),
        ("ix_imports_analysis", "import_references", ["structural_analysis_id"]),
    ]:
        op.create_index(name, table, columns)


def downgrade() -> None:
    raise RuntimeError("Ownership repair downgrade requires restoring a compatible backup.")
