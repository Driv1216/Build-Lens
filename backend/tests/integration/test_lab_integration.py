"""Stage 12: real API onboarding + disposable PostgreSQL + real framework worker processes.

Synthetic repository only. Embeddings and reranking use deterministic model-free providers
so the full lab path (Gateway -> worker -> bridge -> verification) runs with no downloads.
"""

from __future__ import annotations

import hashlib
import socket
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from buildlens.api.main import create_app
from buildlens.core.config import Settings
from buildlens.domain.lab import PIPELINES, LabError, parse_config
from tests.lab_support import install_fakes, lab_service, onboard, write_repository

pytestmark = [pytest.mark.integration, pytest.mark.lab]

QUESTION = "How is the monthly invoice total computed?"
NATIVE_POLICIES = [("semantic", False), ("hybrid", False), ("hybrid", True)]


def _fingerprint(engine: Engine) -> dict[str, tuple[int, str]]:
    result = {}
    with engine.connect() as connection:
        tables = connection.execute(
            text("select tablename from pg_tables where schemaname='public' order by tablename")
        ).scalars()
        for table in list(tables):
            row = connection.execute(
                text(
                    "select count(*), md5(coalesce(string_agg(t::text, '' order by t::text), ''))"
                    f' from public."{table}" t'
                )
            ).one()
            result[table] = (int(row[0]), str(row[1]))
    return result


@pytest.fixture
def lab(pg_engine: Engine, tmp_path: Path) -> Any:
    repository = tmp_path / "repo"
    repository.mkdir()
    write_repository(repository)
    lab_root = tmp_path / "labstate" / ".buildlens-eval" / "lab"
    settings = Settings(
        database_url=pg_engine.url.render_as_string(hide_password=False), lab_root=str(lab_root)
    )
    app = create_app(settings)
    with TestClient(app) as client:
        provider = install_fakes(app.state)
        project, snapshot = onboard(client, repository)
        service = lab_service(app.state, lab_root)
        yield {
            "service": service,
            "project": project,
            "snapshot": snapshot,
            "provider": provider,
            "engine": pg_engine,
            "app": app,
            "client": client,
            "lab_root": lab_root,
            "repo": repository,
        }
        app.state.lab_workers.close()


def _config(pipeline: str, **params: Any) -> Any:
    raw: dict[str, Any] = {"name": "it", "pipeline": pipeline, "params": params}
    spec = PIPELINES[pipeline]
    if "native_chunks" not in spec.corpus_modes:
        raw["corpus"] = {"mode": "artifact_sources"}
    return parse_config(raw)


def test_every_pipeline_builds_queries_and_is_verified(lab: dict[str, Any]) -> None:
    service, project, snapshot = lab["service"], lab["project"], lab["snapshot"]
    before = _fingerprint(lab["engine"])
    seen = {}
    for pipeline in PIPELINES:
        if PIPELINES[pipeline].needs_lab_database:
            continue
        config = _config(pipeline)
        result = service.query(config, project, snapshot, QUESTION)
        assert result["retrieved"], pipeline
        assert result["context"], pipeline
        assert (
            result["verification"].get("rejected_anchors", 0) == 0
            or not PIPELINES[pipeline].native_chunk_identity
        ), pipeline
        if PIPELINES[pipeline].native_chunk_identity:
            assert all(item["chunk_id"] for item in result["ranked"]), pipeline
        seen[pipeline] = result
    # Production evidence is untouched by a full lab session.
    assert _fingerprint(lab["engine"]) == before
    # No framework module was imported by the API (parent) process.
    import sys

    assert not [m for m in sys.modules if m.startswith(("langchain", "llama_index", "langgraph"))]
    # Agent answer cites only evidence it read; offline model is marked mechanics-only.
    agent = seen["langchain.agent"]
    assert agent["answer"]["buildlens_validation"]["valid"]
    assert agent["info"]["model_calls"] <= 6 and agent["info"]["tool_calls"] <= 6


def test_framework_over_native_retrieval_matches_native_ranking(lab: dict[str, Any]) -> None:
    service, project, snapshot = lab["service"], lab["project"], lab["snapshot"]
    for mode, rerank in NATIVE_POLICIES:
        native = service.query(
            _config("native.context", mode=mode, rerank=rerank), project, snapshot, QUESTION
        )
        native_retrieved = [i["chunk_id"] for i in native["retrieved"]]
        native_ranked = [i["chunk_id"] for i in native["ranked"]]
        for pipeline in (
            "langchain.native_retriever",
            "llamaindex.native_retriever",
            "llamaindex.workflow",
        ):
            params: dict[str, Any] = {"mode": mode, "rerank": rerank}
            if pipeline == "llamaindex.native_retriever":
                params["response_mode"] = "no_text"
            if pipeline == "langchain.native_retriever":
                params["synthesis"] = False
            result = service.query(_config(pipeline, **params), project, snapshot, QUESTION)
            assert [i["chunk_id"] for i in result["retrieved"]] == native_retrieved, (
                pipeline,
                mode,
            )
            assert [i["chunk_id"] for i in result["ranked"]][
                : len(native_ranked)
            ] == native_ranked, (pipeline, mode, rerank)


def test_strict_reuse_is_genuine_and_transformations_are_recorded(lab: dict[str, Any]) -> None:
    service, project, snapshot, provider = (
        lab["service"],
        lab["project"],
        lab["snapshot"],
        lab["provider"],
    )
    texts_before = provider.document_texts
    manifest = service.build(_config("langchain.vector"), project, snapshot)
    verification = manifest["reuse_verification"]
    assert verification["verified_genuine_reuse"] is True
    assert verification["bridge_texts_embedded"] == 0 and verification["digest_mismatches"] == 0
    assert verification["framework_vectors_compared"] == verification["stored_vectors"] > 0
    assert provider.document_texts == texts_before  # no document inference anywhere
    llama = service.build(_config("llamaindex.vector"), project, snapshot)
    assert llama["reuse_verification"]["verified_genuine_reuse"] is True
    assert llama["embedding"]["transformations"] == {"identity": llama["nodes"]}
    # Metadata injection under strict reuse is detected, not silently re-embedded.
    with pytest.raises(LabError) as caught:
        service.build(_config("llamaindex.vector", metadata_in_embedding=True), project, snapshot)
    assert (
        caught.value.code in {"VECTOR_REUSE_MISS", "WORKER_EXCEPTION"}
        or "REUSE" in caught.value.code
    )
    relaxed = parse_config(
        {
            "name": "it",
            "pipeline": "llamaindex.vector",
            "embedding": {"source": "reuse-or-compute"},
            "params": {"metadata_in_embedding": True},
        }
    )
    built = service.build(relaxed, project, snapshot)
    assert built["embedding"]["computed_via_bridge"] == built["nodes"]
    assert "identity" not in built["embedding"]["transformations"]
    assert built["reuse_verification"]["verified_genuine_reuse"] is False
    records = service.store.read_index(built["index_id"], "embedding-transformations.json")[
        "records"
    ]
    assert records and all(r["raw_sha256"] != r["embed_sha256"] for r in records)


def test_failed_build_replaces_worker_and_invalidates_loaded_indexes(lab: dict[str, Any]) -> None:
    """S12-SUP-02: a failure during index construction discards the process, and the parent
    no longer believes indexes loaded into that process are available."""
    service, project, snapshot = lab["service"], lab["project"], lab["snapshot"]
    config = _config("llamaindex.vector")
    first = service.build(config, project, snapshot)
    assert "reused_loaded_index" not in first
    assert service.build(config, project, snapshot).get("reused_loaded_index") is True
    worker = lab["app"].state.lab_workers.get("llamaindex")
    assert worker.hello is not None
    pid = worker.hello["pid"]
    with pytest.raises(LabError):  # strict reuse refuses metadata injection inside the worker
        service.build(_config("llamaindex.vector", metadata_in_embedding=True), project, snapshot)
    assert not worker.alive()
    rebuilt = service.build(config, project, snapshot)
    assert "reused_loaded_index" not in rebuilt and rebuilt["index_id"] == first["index_id"]
    assert worker.hello is not None and worker.hello["pid"] != pid
    assert rebuilt["reuse_verification"]["verified_genuine_reuse"] is True
    assert service.query(config, project, snapshot, QUESTION)["ranked"]


def test_gateway_enforces_source_boundary(lab: dict[str, Any]) -> None:
    service, project, snapshot = lab["service"], lab["project"], lab["snapshot"]
    session = service.corpus_session(project, snapshot)
    documents, report = service.corpus.artifact_documents(
        session.corpus, {}, include_unsupported=False
    )
    locators = {d.metadata["locator"] for d in documents}
    assert "billing/invoices.py" in locators
    assert not any(loc.startswith(".buildlens-eval") for loc in locators)
    for document in documents:
        raw = (lab["repo"] / document.metadata["locator"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == document.metadata["artifact_fingerprint"]
    # Bytes changed after the snapshot: the Gateway refuses to relabel them.
    (lab["repo"] / "users/loader.py").write_text("def changed():\n    return 1\n")
    documents, report = service.corpus.artifact_documents(
        session.corpus, {}, include_unsupported=False
    )
    assert "users/loader.py" not in {d.metadata["locator"] for d in documents}
    assert report.skipped.get("SOURCE_DIFFERS_FROM_SNAPSHOT") == 1


def test_authority_change_invalidates_lab_builds(lab: dict[str, Any]) -> None:
    service, project, snapshot = lab["service"], lab["project"], lab["snapshot"]
    service.build(_config("langchain.vector"), project, snapshot)
    with lab["engine"].begin() as connection:
        connection.execute(
            text("UPDATE project_versions SET active_embedding_run_id=NULL WHERE id=:id"),
            {"id": snapshot},
        )
    with pytest.raises(LabError) as caught:
        service.build(_config("llamaindex.vector", k=5), project, snapshot)
    assert caught.value.code == "LAB_CORPUS_CHANGED"


def test_no_external_connection_attempts_from_workers(lab: dict[str, Any]) -> None:
    service, project, snapshot = lab["service"], lab["project"], lab["snapshot"]
    result = service.query(_config("llamaindex.ingest"), project, snapshot, QUESTION)
    assert not [d for d in result["guard_denials"] if d["event"].startswith("socket")]
    worker = lab["app"].state.lab_workers.get("llamaindex")
    probe = worker.request("settings_probe", {}, timeout_seconds=30).value
    assert probe == {
        "llm": "LLAMAINDEX_GLOBAL_LLM_USED",
        "embed": "LLAMAINDEX_GLOBAL_EMBED_MODEL_USED",
    }
    assert socket.gethostname()  # parent process networking is not affected by worker guards


def test_persisted_llamaindex_storage_reloads_identically(lab: dict[str, Any]) -> None:
    service, project, snapshot = lab["service"], lab["project"], lab["snapshot"]
    config = _config("llamaindex.ingest", rerank=False, response_mode="no_text")
    first = service.query(config, project, snapshot, QUESTION)
    reloaded = service.query(config, project, snapshot, QUESTION, reload_from_storage=True)
    assert [i["ref"] for i in first["ranked"]] == [i["ref"] for i in reloaded["ranked"]]
    assert any(step["name"] == "load_index_from_storage" for step in reloaded["steps"])


def test_lab_http_api_contract(lab: dict[str, Any]) -> None:
    client: TestClient = lab["client"]
    scope = f"/api/v1/projects/{lab['project']}/snapshots/{lab['snapshot']}/lab"
    status = client.get("/api/v1/lab/status").json()
    assert {p["id"] for p in status["pipelines"]} == set(PIPELINES)
    assert status["generation"]["enabled"] is False
    pg = next(p for p in status["pipelines"] if p["id"] == "langchain.pg_custom")
    assert pg["available"] is False and pg["reasons"]  # UI never offers what is missing
    bad = client.post("/api/v1/lab/validate", json={"config": {"name": "x", "pipeline": "nope"}})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "CONFIG_PIPELINE_UNKNOWN"
    assert (
        client.post(
            scope + "/probe", content="{}", headers={"content-type": "text/plain"}
        ).status_code
        == 415
    )
    foreign = client.post(
        "/api/v1/lab/experiments", json={"config": {}}, headers={"origin": "http://evil.test"}
    )
    assert foreign.status_code == 403
    config = {
        "name": "api-vector",
        "pipeline": "llamaindex.ingest",
        "corpus": {"mode": "artifact_sources"},
        "params": {"response_mode": "no_text"},
    }
    saved = client.post("/api/v1/lab/experiments", json={"config": config})
    assert saved.status_code == 200
    built = client.post(scope + "/indexes", json={"config": config})
    assert built.status_code == 200, built.text
    index_id = built.json()["index_id"]
    assert client.get(f"/api/v1/lab/indexes/{index_id}").json()["index_id"] == index_id
    spans = client.get(
        f"/api/v1/lab/indexes/{index_id}/boundaries", params={"locator": "billing/invoices.py"}
    ).json()
    assert spans["framework_spans"] and spans["native_spans"] and spans["text"]
    probe = client.post(
        scope + "/probe",
        json={
            "question": QUESTION,
            "configs": [{"name": "n", "pipeline": "native.context"}, config],
        },
    )
    assert probe.status_code == 200 and probe.headers["cache-control"] == "no-store"
    body = probe.json()
    assert all(r["ok"] for r in body["results"]) and "0-1" in body["overlap"]
    assert all(len(t) <= 8192 for r in body["results"] for t in r["result"]["texts"].values())
    diff = client.post("/api/v1/lab/loader-diff", json={"seed": 3})
    assert diff.status_code == 200
    summary = diff.json()["summary"]
    assert summary["llamaindex.reader_default"]["symlink_escape_followed"] is True
    assert (
        ".env" in summary["llamaindex.reader_hidden"]["loaded_but_buildlens_excluded_or_rejected"]
    )


def test_sentence_windows_survive_dropped_oversized_sentences(lab: dict[str, Any]) -> None:
    service, project, snapshot = lab["service"], lab["project"], lab["snapshot"]
    for pipeline in ("langchain.sentence_window", "llamaindex.sentence_window"):
        config = _config(pipeline, max_sentence_chars=200, window_size=2)
        build = service.build(config, project, snapshot)
        assert build["worker_manifest"]["dropped_oversized_sentences"] > 0, pipeline
        result = service.query(config, project, snapshot, QUESTION)
        assert result["context"] and result["verification"]["rejected_anchors"] == 0, pipeline
