"""Stage 12 real-model gate: lab adapters over the pinned MiniLM and cross-encoder.

Requires both explicitly acquired model caches and a disposable PostgreSQL admin URL.
Verifies genuine stored-vector reuse, bridge recomputation parity within 1e-5, and that
framework-over-native retrieval reproduces native reranked ranking with the real model.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from buildlens.api.main import create_app
from buildlens.core.config import Settings
from buildlens.domain.lab import parse_config
from tests.lab_support import lab_service, onboard, write_repository

QUESTION = "How is the monthly invoice total computed?"


@pytest.mark.real_model
@pytest.mark.integration
@pytest.mark.lab
def test_lab_adapters_with_real_models(pg_engine: Engine, tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    write_repository(repository)
    lab_root = tmp_path / "state" / ".buildlens-eval" / "lab"
    settings = Settings(
        database_url=pg_engine.url.render_as_string(hide_password=False),
        lab_root=str(lab_root),
        embeddings_cache_dir=os.environ["BUILDLENS_EMBEDDINGS_CACHE_DIR"],
        reranker_cache_dir=os.environ["BUILDLENS_RERANKER_CACHE_DIR"],
    )
    app = create_app(settings)
    with TestClient(app) as client:
        project, snapshot = onboard(client, repository)  # real MiniLM embeddings
        service = lab_service(app.state, lab_root)
        try:
            build = service.build(
                parse_config({"name": "v", "pipeline": "langchain.vector"}), project, snapshot
            )
            verification = build["reuse_verification"]
            assert verification["verified_genuine_reuse"] is True
            assert verification["bridge_texts_embedded"] == 0
            # Recompute stored chunks through the bridge with the real model: same vectors.
            session = service.corpus_session(project, snapshot)
            stored = service.corpus.stored_vectors(session.corpus)
            chunks = list(session.corpus.chunks)[:6]
            provider = app.state.embedding_runtime.provider
            fresh = provider.embed_documents([c.content for c in chunks])
            for chunk, vector in zip(chunks, fresh, strict=True):
                expected = stored[str(chunk.id)]
                assert max(abs(a - b) for a, b in zip(vector.values, expected, strict=True)) < 1e-5
                assert math.isclose(sum(v * v for v in vector.values), 1.0, abs_tol=1e-4)
            native = service.query(
                parse_config(
                    {
                        "name": "n",
                        "pipeline": "native.context",
                        "params": {"mode": "hybrid", "rerank": True},
                    }
                ),
                project,
                snapshot,
                QUESTION,
            )
            for pipeline, extra in (
                ("langchain.native_retriever", {"synthesis": False}),
                ("llamaindex.native_retriever", {"response_mode": "no_text"}),
                ("llamaindex.workflow", {}),
            ):
                result = service.query(
                    parse_config(
                        {
                            "name": "f",
                            "pipeline": pipeline,
                            "params": {"mode": "hybrid", "rerank": True, **extra},
                        }
                    ),
                    project,
                    snapshot,
                    QUESTION,
                )
                assert [i["chunk_id"] for i in result["ranked"]][: len(native["ranked"])] == [
                    i["chunk_id"] for i in native["ranked"]
                ], pipeline
            computed = service.build(
                parse_config(
                    {
                        "name": "c",
                        "pipeline": "llamaindex.ingest",
                        "corpus": {"mode": "artifact_sources"},
                        "params": {"response_mode": "no_text"},
                    }
                ),
                project,
                snapshot,
            )
            assert computed["embedding"]["computed_via_bridge"] == computed["nodes"] > 0
        finally:
            app.state.lab_workers.close()
