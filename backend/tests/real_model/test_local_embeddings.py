import math
import os
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from buildlens.infrastructure.embeddings.supervisor import SupervisedSentenceTransformer


@pytest.mark.real_model
@pytest.mark.integration
def test_pinned_model_runs_offline_with_stable_semantic_ranking(pg_engine: Engine) -> None:
    cache = Path(os.environ["BUILDLENS_EMBEDDINGS_CACHE_DIR"])
    provider = SupervisedSentenceTransformer(cache)
    try:
        assert provider.status().available
        documents = provider.embed_documents(
            [
                "A puppy plays with a ball in the garden.",
                "Apply database schema migrations before starting the API.",
                "The ocean contains many species of fish.",
            ]
        )
        query = provider.embed_query("How should I update the database schema?")
        assert len(documents) == 3
        assert all(len(value.values) == 384 for value in documents)
        assert all(
            math.isclose(math.sqrt(sum(x * x for x in value.values)), 1.0, abs_tol=1e-4)
            for value in [*documents, query]
        )
        scores = [
            sum(a * b for a, b in zip(value.values, query.values, strict=True))
            for value in documents
        ]
        assert max(range(len(scores)), key=scores.__getitem__) == 1
        with pg_engine.begin() as connection:
            connection.execute(text("CREATE TEMP TABLE real_vectors (embedding vector)"))
            for value in documents:
                connection.execute(
                    text("INSERT INTO real_vectors VALUES (CAST(:embedding AS vector))"),
                    {"embedding": str(list(value.values))},
                )
            database_scores = list(
                connection.scalars(
                    text(
                        "SELECT 1 - (embedding <=> CAST(:query AS vector)) "
                        "FROM real_vectors ORDER BY embedding <=> CAST(:query AS vector)"
                    ),
                    {"query": str(list(query.values))},
                )
            )
        assert (
            max(
                abs(float(left) - right)
                for left, right in zip(database_scores, sorted(scores, reverse=True), strict=True)
            )
            <= 1e-5
        )
        repeated = provider.embed_documents(
            ["Apply database schema migrations before starting the API."]
        )[0]
        assert repeated.values == pytest.approx(documents[1].values, abs=1e-6)
        prefix = "garden " * 254
        prefix_vector, complete_vector = provider.embed_documents(
            [prefix, prefix + "database schema migration"]
        )
        assert complete_vector.window_count == 2
        assert (
            max(
                abs(left - right)
                for left, right in zip(prefix_vector.values, complete_vector.values, strict=True)
            )
            > 1e-4
        )
    finally:
        provider.close()
