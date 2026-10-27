"""Stage 12 domain contracts: strict configuration, identity, anchoring and packing."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from buildlens.application.embeddings import EmbeddingRuntime
from buildlens.domain.lab import (
    LAB_CONTEXT_BYTE_LIMIT,
    PIPELINES,
    LabError,
    LabItem,
    anchored,
    pack_context,
    parse_config,
)
from buildlens.infrastructure.lab.bridge import CapabilityBridge
from buildlens.infrastructure.lab.fakes import HashEmbeddingProvider, hash_vector


def test_every_pipeline_default_config_validates_and_is_stable() -> None:
    for pipeline, spec in PIPELINES.items():
        raw: dict[str, Any] = {"name": "x", "pipeline": pipeline}
        if "native_chunks" not in spec.corpus_modes:
            raw["corpus"] = {"mode": spec.corpus_modes[0]}
        config = parse_config(raw)
        assert parse_config(config.as_dict()).digest == config.digest
        assert set(config.params) == set(spec.params)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ({"name": "x", "pipeline": "nope"}, "CONFIG_PIPELINE_UNKNOWN"),
        ({"name": "Bad Name", "pipeline": "langchain.vector"}, "CONFIG_INVALID"),
        ({"name": "x", "pipeline": "langchain.vector", "extra": 1}, "CONFIG_INVALID"),
        ({"name": "x", "pipeline": "langchain.vector", "params": {"bogus": 1}}, "CONFIG_INVALID"),
        (
            {"name": "x", "pipeline": "langchain.vector", "params": {"k": 0}},
            "CONFIG_PARAMETER_INVALID",
        ),
        (
            {"name": "x", "pipeline": "langchain.vector", "params": {"k": True}},
            "CONFIG_PARAMETER_INVALID",
        ),
        (
            {"name": "x", "pipeline": "langchain.ingest", "corpus": {"mode": "native_chunks"}},
            "CONFIG_CORPUS_MODE_UNSUPPORTED",
        ),
        (
            {
                "name": "x",
                "pipeline": "langchain.ingest",
                "corpus": {"mode": "artifact_sources"},
                "params": {"chunk_size": 100, "chunk_overlap": 100},
            },
            "CONFIG_PARAMETER_INVALID",
        ),
        (
            {
                "name": "x",
                "pipeline": "llamaindex.auto_merging",
                "corpus": {"mode": "artifact_sources"},
                "params": {"chunk_sizes": [128, 512]},
            },
            "CONFIG_PARAMETER_INVALID",
        ),
        (
            {
                "name": "x",
                "pipeline": "langchain.ingest",
                "corpus": {"mode": "artifact_sources"},
                "embedding": {"source": "reuse-stored"},
            },
            "CONFIG_INVALID",
        ),
        (
            {"name": "x", "pipeline": "langchain.vector", "contract": "stage99"},
            "CONFIG_CONTRACT_UNSUPPORTED",
        ),
    ],
)
def test_invalid_configs_are_rejected(raw: dict[str, Any], code: str) -> None:
    with pytest.raises(LabError) as caught:
        parse_config(raw)
    assert caught.value.code == code


def test_ingestion_identity_ignores_query_only_parameters() -> None:
    a = parse_config(
        {"name": "x", "pipeline": "langchain.vector", "params": {"k": 5, "search_type": "mmr"}}
    )
    b = parse_config({"name": "x", "pipeline": "langchain.vector", "params": {"k": 20}})
    assert a.digest != b.digest
    assert a.ingestion_identity() == b.ingestion_identity()


def test_anchoring_requires_an_exact_slice() -> None:
    source = "line one\r\nline two\nunicode é\n"
    assert anchored(source, 10, "line two")
    assert not anchored(source, 9, "line two")
    assert not anchored(source, None, "line two")
    assert anchored(source, source.index("é"), "é")


def _item(ref: str, size: int, rank: int) -> LabItem:
    return LabItem(ref, rank, "0" * 64, size, "a.py", "f" * 64, 0, size, True)


def test_shared_packing_is_whole_item_and_skip_if_not_fit() -> None:
    items = [
        _item("big", LAB_CONTEXT_BYTE_LIMIT - 10, 1),
        _item("too-big", 100, 2),
        _item("small", 5, 3),
        _item("big", 1, 4),
    ]
    packed, report = pack_context(items)
    assert [p.ref for p in packed] == ["big", "small"]
    assert report["skipped"] == ["too-big"]
    many = [_item(f"r{i}", 10, i) for i in range(30)]
    packed, _ = pack_context(many)
    assert len(packed) == 16


# ------------------------------------------------------------------ bridge limits


def _bridge(**kwargs: Any) -> CapabilityBridge:
    return CapabilityBridge(embedding=EmbeddingRuntime(HashEmbeddingProvider()), **kwargs)


def test_bridge_bounds_transfers_and_contract() -> None:
    bridge = _bridge()
    deadline = 1e12
    vectors = bridge("embed_documents", {"texts": ["load user"]}, deadline)["vectors"]
    assert len(vectors[0]) == 384
    params: dict[str, Any]
    for params in ({"texts": []}, {"texts": ["x"] * 65}, {"texts": ["x" * (300 * 1024)]}):
        with pytest.raises(LabError) as caught:
            bridge("embed_documents", params, deadline)
        assert caught.value.code == "LAB_TRANSFER_INVALID"
    with pytest.raises(LabError) as caught:
        bridge("embed_query", {"text": "x" * 5000}, deadline)
    assert caught.value.code == "LAB_TRANSFER_INVALID"
    mismatched = _bridge(expected_contract_digest="0" * 64)
    with pytest.raises(LabError) as caught:
        mismatched("embed_query", {"text": "x"}, deadline)
    assert caught.value.code == "LAB_EMBEDDING_CONTRACT_MISMATCH"
    busy = _bridge()
    busy.embedding.lock.acquire()
    with pytest.raises(LabError) as caught:
        busy("embed_query", {"text": "x"}, deadline)
    assert caught.value.code == "LAB_EMBEDDING_BUSY"
    with pytest.raises(LabError) as caught:
        bridge("tool_call", {"name": "shell", "args": {}}, deadline)
    assert caught.value.code == "LAB_TOOL_NOT_ALLOWED"
    with pytest.raises(LabError) as caught:
        bridge("generate", {}, deadline)
    assert caught.value.code == "LAB_GENERATION_NOT_AUTHORIZED"
    with pytest.raises(LabError) as caught:
        bridge("embed_query", {"text": "x"}, 0.0)
    assert caught.value.code == "LAB_WORKER_TIMEOUT"
    assert bridge.ledger.texts_embedded == 1


def test_bridge_tool_budget_is_enforced() -> None:
    from buildlens.domain.processing import Chunk, ChunkSpan

    chunk_id = uuid4()
    chunk = Chunk(
        chunk_id,
        uuid4(),
        "k",
        0,
        "a",
        0,
        "code",
        "x" * 400,
        "h",
        ChunkSpan(0, 400, 1, 1, 0, 0),
        locator="a.py",
    )

    class Corpus:
        chunks = (chunk,)

    bridge = _bridge(corpus=Corpus(), tool_budget_bytes=600)
    bridge("tool_call", {"name": "buildlens_read_chunk", "args": {"chunk_id": str(chunk_id)}}, 1e12)
    with pytest.raises(LabError) as caught:
        bridge(
            "tool_call", {"name": "buildlens_read_chunk", "args": {"chunk_id": str(chunk_id)}}, 1e12
        )
    assert caught.value.code == "LAB_TOOL_BUDGET_EXCEEDED"
    with pytest.raises(LabError) as caught:
        bridge(
            "tool_call", {"name": "buildlens_read_chunk", "args": {"chunk_id": str(uuid4())}}, 1e12
        )
    assert caught.value.code == "LAB_CHUNK_OUTSIDE_CORPUS"


def test_hash_embedding_is_deterministic_and_normalized() -> None:
    a, b = hash_vector("loadUser record"), hash_vector("loadUser record")
    assert a == b and abs(sum(v * v for v in a) - 1.0) < 1e-9
    assert hash_vector("") != tuple(0.0 for _ in a)


# ------------------------------------------------------------------ corpus filters


def test_kind_filter_is_rejected_for_artifact_sources() -> None:
    """Defect: artifacts have no chunk kind, so the filter was silently ignored."""
    with pytest.raises(LabError) as caught:
        parse_config(
            {
                "name": "k",
                "pipeline": "langchain.ingest",
                "corpus": {"mode": "artifact_sources", "filters": {"kind": "function"}},
            }
        )
    assert caught.value.code == "CONFIG_INVALID" and "kind" in caught.value.message
    allowed = parse_config(
        {
            "name": "k",
            "pipeline": "langchain.vector",
            "corpus": {"mode": "native_chunks", "filters": {"kind": "function"}},
        }
    )
    assert allowed.filters["kind"] == "function"


def test_gateway_filter_never_ignores_a_requested_kind() -> None:
    from buildlens.infrastructure.lab.corpus import _matches

    assert _matches("a.py", "python", "function", {"kind": "function"})
    assert not _matches("a.py", "python", "class", {"kind": "function"})
    assert not _matches("a.py", "python", None, {"kind": "function"})  # unknown never matches
    assert _matches("a.py", "python", None, {"kind": None})


# ------------------------------------------------------------------ generation status


@pytest.mark.parametrize("flag", [False, True])
def test_generation_is_never_advertised_while_unimplemented(flag: bool) -> None:
    """Defect: with the flag set the status said "authorized" although `generate` always fails."""
    from buildlens.infrastructure.lab.service import catalog

    status = catalog(None, lab_db_configured=False, generation_enabled=flag)["generation"]
    assert status["enabled"] is False and status["available"] is False
    assert status["implemented"] is False and status["flag_set"] is flag
    assert "authorized" not in status["state"] and "not implemented" in status["state"]
