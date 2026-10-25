"""Stage 13: additive multi-language structural evidence (no backfill, no rewritten rows)."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import SchemaItem

from alembic import op

revision = "0010_multilanguage_structure"
down_revision = "0009_lexical_hybrid_retrieval"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=True)


def _span_columns() -> list[SchemaItem]:
    return [
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("start_column", sa.Integer(), nullable=True),
        sa.Column("end_column", sa.Integer(), nullable=True),
    ]


def _owned_by_extraction(*extra: SchemaItem) -> list[SchemaItem]:
    return [
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("extraction_id", _UUID, nullable=False),
        sa.Column("artifact_analysis_id", _UUID, nullable=False),
        *extra,
        sa.ForeignKeyConstraint(
            ["extraction_id", "artifact_analysis_id"],
            ["artifact_extractions.id", "artifact_extractions.artifact_analysis_id"],
            ondelete="CASCADE",
        ),
    ]


def upgrade() -> None:
    op.drop_constraint("ck_symbols_kind", "symbols", type_="check")
    op.create_check_constraint(
        "ck_symbols_kind",
        "symbols",
        "kind IN ('function', 'class', 'method', 'interface', 'type_alias', 'enum', 'namespace')",
    )
    for name, kind in (
        ("declaration_form", sa.String(32)),
        ("declaration_space", sa.String(16)),
        ("name_source", sa.String(32)),
        ("exported", sa.String(16)),
    ):
        op.add_column("symbols", sa.Column(name, kind, nullable=True))
    op.add_column("symbols", sa.Column("start_offset", sa.Integer(), nullable=True))
    op.add_column("symbols", sa.Column("end_offset", sa.Integer(), nullable=True))
    op.add_column("symbols", sa.Column("additional_spans", postgresql.JSONB(), nullable=True))
    op.create_check_constraint(
        "ck_symbols_declaration_space",
        "symbols",
        "declaration_space IS NULL OR declaration_space IN ('value', 'type', 'namespace')",
    )
    op.create_check_constraint(
        "ck_symbols_offsets",
        "symbols",
        "(start_offset IS NULL) = (end_offset IS NULL) AND "
        "(start_offset IS NULL OR (start_offset >= 0 AND end_offset >= start_offset))",
    )
    op.add_column("symbol_identities", sa.Column("declaration_space", sa.String(16), nullable=True))

    op.drop_constraint("ck_import_references_form", "import_references", type_="check")
    op.create_check_constraint(
        "ck_import_references_form",
        "import_references",
        "form IN ('import', 'from', 'es_import', 'es_import_type', 'dynamic_import', 'require')",
    )

    op.create_table(
        "artifact_extractions",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("artifact_analysis_id", _UUID, nullable=False),
        sa.Column("structural_analysis_id", _UUID, nullable=False),
        sa.Column("extractor", sa.String(64), nullable=False),
        sa.Column("contract", postgresql.JSONB(), nullable=False),
        sa.Column("contract_digest", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("execution_mode", sa.String(16), nullable=False),
        sa.Column("reuse_source_extraction_id", _UUID, nullable=True),
        sa.Column("input_fingerprint", sa.String(64), nullable=True),
        sa.Column("diagnostics", postgresql.JSONB(), nullable=False),
        sa.Column("artifact_facts", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint(
            "artifact_analysis_id", "extractor", name="uq_artifact_extractions_analysis_extractor"
        ),
        sa.UniqueConstraint(
            "id", "artifact_analysis_id", name="uq_artifact_extractions_id_analysis"
        ),
        sa.ForeignKeyConstraint(
            ["artifact_analysis_id", "structural_analysis_id"],
            ["artifact_analyses.id", "artifact_analyses.structural_analysis_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reuse_source_extraction_id"],
            ["artifact_extractions.id"],
            name="fk_artifact_extraction_reuse",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(
            "outcome IN ('extracted', 'partial', 'failed', 'skipped')",
            name="ck_artifact_extractions_outcome",
        ),
        sa.CheckConstraint(
            "execution_mode IN ('extracted', 'reused')", name="ck_artifact_extractions_mode"
        ),
        sa.CheckConstraint(
            "(execution_mode = 'reused') = (reuse_source_extraction_id IS NOT NULL)",
            name="ck_artifact_extractions_reuse",
        ),
        sa.CheckConstraint(
            "contract_digest ~ '^[0-9a-f]{64}$'", name="ck_artifact_extractions_contract_digest"
        ),
    )
    op.create_index(
        "ix_artifact_extractions_analysis", "artifact_extractions", ["structural_analysis_id"]
    )

    op.add_column("artifact_analyses", sa.Column("language", sa.String(32), nullable=True))
    op.add_column("artifact_analyses", sa.Column("declaration_extraction_id", _UUID, nullable=True))
    op.create_check_constraint(
        "ck_artifact_analyses_declaration_authority",
        "artifact_analyses",
        "outcome <> 'parsed' OR language IS NULL OR language = 'Python' "
        "OR declaration_extraction_id IS NOT NULL",
    )
    op.create_foreign_key(
        "fk_artifact_analysis_declaration_extraction",
        "artifact_analyses",
        "artifact_extractions",
        ["declaration_extraction_id", "id"],
        ["id", "artifact_analysis_id"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "symbol_facts",
        *_owned_by_extraction(
            sa.Column("symbol_id", _UUID, nullable=False),
            sa.Column("facts", postgresql.JSONB(), nullable=False),
        ),
        sa.UniqueConstraint("extraction_id", "symbol_id", name="uq_symbol_facts_extraction_symbol"),
        sa.ForeignKeyConstraint(
            ["symbol_id", "artifact_analysis_id"],
            ["symbols.id", "symbols.artifact_analysis_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_symbol_facts_symbol", "symbol_facts", ["symbol_id"])

    op.create_table(
        "symbol_references",
        *_owned_by_extraction(
            sa.Column("owner_symbol_id", _UUID, nullable=True),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("target", sa.Text(), nullable=False),
            *_span_columns(),
        ),
        sa.UniqueConstraint("extraction_id", "ordinal", name="uq_symbol_references_ordinal"),
        sa.ForeignKeyConstraint(
            ["owner_symbol_id", "artifact_analysis_id"],
            ["symbols.id", "symbols.artifact_analysis_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "kind IN ('call', 'base', 'decorator', 'jsx_element', 'hook_call', 'raise', "
            "'type_ref')",
            name="ck_symbol_references_kind",
        ),
        sa.CheckConstraint(
            "char_length(target) BETWEEN 1 AND 512", name="ck_symbol_references_target"
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_symbol_references_ordinal"),
    )
    op.create_index("ix_symbol_references_owner", "symbol_references", ["owner_symbol_id"])

    op.create_table(
        "export_references",
        *_owned_by_extraction(
            sa.Column("owner_symbol_id", _UUID, nullable=True),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("local_name", sa.Text(), nullable=True),
            sa.Column("source_module", sa.Text(), nullable=True),
            *_span_columns(),
        ),
        sa.UniqueConstraint("extraction_id", "ordinal", name="uq_export_references_ordinal"),
        sa.ForeignKeyConstraint(
            ["owner_symbol_id", "artifact_analysis_id"],
            ["symbols.id", "symbols.artifact_analysis_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "kind IN ('named', 'default', 'reexport', 'reexport_all', 'commonjs', 'dunder_all')",
            name="ck_export_references_kind",
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_export_references_ordinal"),
    )

    op.add_column(
        "artifact_processings", sa.Column("fingerprint_scheme", sa.String(64), nullable=True)
    )
    op.add_column("processing_runs", sa.Column("processor_set", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    used = bind.execute(
        sa.text(
            "SELECT (SELECT count(*) FROM artifact_extractions) "
            "+ (SELECT count(*) FROM symbols WHERE kind NOT IN ('function', 'class', 'method') "
            "   OR declaration_form IS NOT NULL OR start_offset IS NOT NULL) "
            "+ (SELECT count(*) FROM import_references WHERE form NOT IN ('import', 'from')) "
            "+ (SELECT count(*) FROM artifact_analyses WHERE language IS NOT NULL) "
            "+ (SELECT count(*) FROM artifact_processings WHERE fingerprint_scheme IS NOT NULL) "
            "+ (SELECT count(*) FROM processing_runs WHERE processor_set IS NOT NULL)"
        )
    ).scalar_one()
    if used:
        raise RuntimeError(
            "Stage 13 multi-language evidence exists; downgrading would discard it. "
            "Restore a verified pre-upgrade backup instead."
        )
    op.drop_column("processing_runs", "processor_set")
    op.drop_column("artifact_processings", "fingerprint_scheme")
    op.drop_table("export_references")
    op.drop_table("symbol_references")
    op.drop_table("symbol_facts")
    op.drop_constraint(
        "fk_artifact_analysis_declaration_extraction", "artifact_analyses", type_="foreignkey"
    )
    op.drop_constraint(
        "ck_artifact_analyses_declaration_authority", "artifact_analyses", type_="check"
    )
    op.drop_column("artifact_analyses", "declaration_extraction_id")
    op.drop_column("artifact_analyses", "language")
    op.drop_table("artifact_extractions")
    op.drop_constraint("ck_import_references_form", "import_references", type_="check")
    op.create_check_constraint(
        "ck_import_references_form", "import_references", "form IN ('import', 'from')"
    )
    op.drop_column("symbol_identities", "declaration_space")
    op.drop_constraint("ck_symbols_offsets", "symbols", type_="check")
    op.drop_constraint("ck_symbols_declaration_space", "symbols", type_="check")
    for name in (
        "additional_spans",
        "end_offset",
        "start_offset",
        "exported",
        "name_source",
        "declaration_space",
        "declaration_form",
    ):
        op.drop_column("symbols", name)
    op.drop_constraint("ck_symbols_kind", "symbols", type_="check")
    op.create_check_constraint(
        "ck_symbols_kind", "symbols", "kind IN ('function', 'class', 'method')"
    )
