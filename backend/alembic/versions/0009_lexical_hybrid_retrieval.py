"""Add immutable lexical normalization and PostgreSQL full-text indexes."""

from alembic import op

revision = "0009_lexical_hybrid_retrieval"
down_revision = "0008_persistent_vector_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE FUNCTION public.buildlens_lexical_normalize_v1(value text)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        STRICT
        AS $function$
          SELECT translate(
            regexp_replace(
              regexp_replace(
                value, '([A-Z]+)([A-Z][a-z])', '\1 \2', 'g'
              ),
              '([a-z0-9])([A-Z])', '\1 \2', 'g'
            ),
            '_./:-' || chr(92), '      '
          )
        $function$
        """
    )
    op.execute(
        """
        CREATE INDEX ix_chunks_lexical_content_v1 ON chunks USING gin (
          to_tsvector(
            'pg_catalog.simple'::regconfig,
            public.buildlens_lexical_normalize_v1(content)
          )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_symbols_lexical_qualified_name_v1 ON symbols USING gin (
          to_tsvector(
            'pg_catalog.simple'::regconfig,
            public.buildlens_lexical_normalize_v1(qualified_name)
          )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_artifact_processings_lexical_locator_v1
        ON artifact_processings USING gin (
          to_tsvector(
            'pg_catalog.simple'::regconfig,
            public.buildlens_lexical_normalize_v1(locator)
          )
        )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_artifact_processings_lexical_locator_v1", table_name="artifact_processings")
    op.drop_index("ix_symbols_lexical_qualified_name_v1", table_name="symbols")
    op.drop_index("ix_chunks_lexical_content_v1", table_name="chunks")
    op.execute("DROP FUNCTION public.buildlens_lexical_normalize_v1(text)")
