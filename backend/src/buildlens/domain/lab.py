"""Stage 12 framework-laboratory contracts; framework-free and database-free.

The laboratory compares native BuildLens retrieval with LangChain and LlamaIndex pipelines.
Everything here is plain data: experiment configuration and its canonical identity, the
pipeline catalogue with strict parameter schemas, result items and normalized trace steps.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

LAB_CONTRACT_VERSION = "stage12.lab.v1"
LAB_PACKING_VERSION = "stage12.lab-packing.v1"
TRACE_LIMIT_BYTES = 4 * 1024 * 1024
LAB_CONTEXT_ITEM_LIMIT = 16
LAB_CONTEXT_BYTE_LIMIT = 32_000
LAB_RETRIEVAL_DEPTH = 32

Family: TypeAlias = Literal["native", "langchain", "llamaindex", "langchain_postgres"]
CorpusMode: TypeAlias = Literal["native_chunks", "artifact_sources", "synthetic_sandbox"]
EmbeddingSource: TypeAlias = Literal[
    "reuse-stored", "compute", "reuse-or-compute", "deterministic-fake"
]
ScoringTier: TypeAlias = Literal["approved", "span", "derived", "constructed"]
StepKind: TypeAlias = Literal[
    "load",
    "split",
    "embed",
    "store",
    "retrieve",
    "fuse",
    "rerank",
    "postprocess",
    "expand",
    "merge",
    "pack",
    "prompt",
    "generate",
    "parse",
    "validate",
    "tool",
    "agent",
    "bridge",
    "other",
]
STEP_KINDS: frozenset[str] = frozenset(StepKind.__args__)  # type: ignore[attr-defined]


class LabError(ValueError):
    """Allow-listed laboratory failure code; messages never contain source or secrets."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message or code
        super().__init__(code)


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        _plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def sha256_hex(value: bytes | str) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def lab_digest(value: Any) -> str:
    return sha256_hex(canonical_json(value))


# --------------------------------------------------------------------------------------
# Parameter schemas
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Param:
    kind: Literal["int", "float", "bool", "choice", "int_list", "float_list"]
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    help: str = ""

    def validate(self, name: str, value: Any) -> Any:
        if self.kind == "bool":
            if type(value) is not bool:
                raise LabError("CONFIG_PARAMETER_INVALID", f"{name} must be a boolean")
            return value
        if self.kind == "choice":
            if value not in self.choices:
                raise LabError("CONFIG_PARAMETER_INVALID", f"{name} must be one of {self.choices}")
            return value
        if self.kind in {"int_list", "float_list"}:
            if not isinstance(value, list) or not 1 <= len(value) <= 6:
                raise LabError("CONFIG_PARAMETER_INVALID", f"{name} must be a list of 1-6 values")
            scalar = Param(
                "int" if self.kind == "int_list" else "float", 0, self.minimum, self.maximum
            )
            return [scalar.validate(name, item) for item in value]
        if self.kind == "int":
            if type(value) is not int:
                raise LabError("CONFIG_PARAMETER_INVALID", f"{name} must be an integer")
        elif type(value) not in {int, float} or value != value:
            raise LabError("CONFIG_PARAMETER_INVALID", f"{name} must be a finite number")
        if self.minimum is not None and value < self.minimum:
            raise LabError("CONFIG_PARAMETER_INVALID", f"{name} must be >= {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise LabError("CONFIG_PARAMETER_INVALID", f"{name} must be <= {self.maximum}")
        return float(value) if self.kind == "float" else value


_K = Param("int", LAB_RETRIEVAL_DEPTH, 1, 100, help="Ranked retrieval depth")
_CHUNK = Param("int", 1024, 64, 8192, help="Splitter chunk size (characters)")
_OVERLAP = Param("int", 0, 0, 2048, help="Splitter chunk overlap")
_RERANK = Param("bool", False, help="Rerank through the BuildLens cross-encoder bridge")
_MODE = Param("choice", "hybrid", choices=("exact", "lexical", "semantic", "hybrid"))
_SYNTH = Param("bool", True, help="Run the offline synthesis chain (mechanics only)")
_MAX_SENTENCE = Param(
    "int", 4000, 200, 50_000, help="Drop sentence nodes longer than this (embedding window limit)"
)


@dataclass(frozen=True, slots=True)
class PipelineSpec:
    id: str
    family: Family
    label: str
    outcome: str  # learning outcome / experiment identifier from the Stage 12 plan
    corpus_modes: tuple[CorpusMode, ...]
    params: dict[str, Param] = field(default_factory=dict)
    native_chunk_identity: bool = False  # items are BuildLens chunks -> Tier A eligible
    needs_lab_database: bool = False
    extra: str | None = None  # Python extra/environment that must be installed
    consumes_documents: bool = True  # False: queries native retrieval only (no index)


_NATIVE_ONLY: tuple[CorpusMode, ...] = ("native_chunks",)
_SOURCES: tuple[CorpusMode, ...] = ("artifact_sources", "synthetic_sandbox")
_ANY: tuple[CorpusMode, ...] = ("native_chunks", "artifact_sources", "synthetic_sandbox")

PIPELINES: dict[str, PipelineSpec] = {
    spec.id: spec
    for spec in (
        PipelineSpec(
            "native.context",
            "native",
            "Native BuildLens Stage 9 context",
            "N0",
            _NATIVE_ONLY,
            {"mode": _MODE, "rerank": _RERANK},
            native_chunk_identity=True,
        ),
        PipelineSpec(
            "langchain.native_retriever",
            "langchain",
            "LangChain LCEL over native BuildLens retrieval",
            "LC-A",
            _NATIVE_ONLY,
            {"mode": _MODE, "rerank": _RERANK, "synthesis": _SYNTH},
            native_chunk_identity=True,
            consumes_documents=False,
            extra="lab-langchain",
        ),
        PipelineSpec(
            "langchain.vector",
            "langchain",
            "LangChain InMemoryVectorStore retriever",
            "LC-B",
            _ANY,
            {
                "k": _K,
                "search_type": Param(
                    "choice",
                    "similarity",
                    choices=("similarity", "mmr", "similarity_score_threshold"),
                ),
                "fetch_k": Param("int", 64, 1, 200),
                "lambda_mult": Param("float", 0.5, 0.0, 1.0),
                "score_threshold": Param("float", 0.2, -1.0, 1.0),
                "synthesis": _SYNTH,
            },
            native_chunk_identity=True,
            extra="lab-langchain",
        ),
        PipelineSpec(
            "langchain.ensemble",
            "langchain",
            "LangChain EnsembleRetriever (vector + BM25 weighted RRF)",
            "AR-1",
            _ANY,
            {
                "k": _K,
                "vector_weight": Param("float", 0.5, 0.0, 1.0),
                "c": Param("int", 60, 1, 1000),
                "dedupe_by": Param("choice", "chunk_id", choices=("chunk_id", "page_content")),
            },
            native_chunk_identity=True,
            extra="lab-langchain",
        ),
        PipelineSpec(
            "langchain.ingest",
            "langchain",
            "LangChain-native splitting + vector store + compression",
            "LC-C",
            _SOURCES,
            {
                "k": _K,
                "splitter": Param(
                    "choice",
                    "recursive_language",
                    choices=("recursive_language", "recursive", "markdown_header"),
                ),
                "chunk_size": _CHUNK,
                "chunk_overlap": _OVERLAP,
                "rerank": _RERANK,
                "synthesis": _SYNTH,
            },
            extra="lab-langchain",
        ),
        PipelineSpec(
            "langchain.parent_document",
            "langchain",
            "LangChain ParentDocumentRetriever (child search, parent return)",
            "AR-2",
            _SOURCES,
            {
                "k": Param("int", 8, 1, 50),
                "child_chunk_size": Param("int", 400, 64, 4096),
                "parent_chunk_size": Param("int", 2000, 0, 16384, help="0 = whole artifact"),
            },
            extra="lab-langchain",
        ),
        PipelineSpec(
            "langchain.auto_merge",
            "langchain",
            "Hand-built LCEL auto-merging (LangChain has no native equivalent)",
            "AR-3",
            _SOURCES,
            {
                "k": _K,
                "chunk_sizes": Param("int_list", [2048, 512, 128], 32, 16384),
                "merge_ratio": Param("float", 0.5, 0.01, 1.0),
            },
            extra="lab-langchain",
        ),
        PipelineSpec(
            "langchain.sentence_window",
            "langchain",
            "Hand-built LCEL sentence-window expansion",
            "AR-4",
            _SOURCES,
            {"k": _K, "window_size": Param("int", 3, 0, 20), "max_sentence_chars": _MAX_SENTENCE},
            extra="lab-langchain",
        ),
        PipelineSpec(
            "langchain.agent",
            "langchain",
            "Bounded read-only create_agent over BuildLens tools",
            "AG-1",
            _NATIVE_ONLY,
            {
                "max_model_calls": Param("int", 6, 1, 12),
                "max_tool_calls": Param("int", 6, 1, 12),
                "evidence_budget_bytes": Param("int", 32_000, 1000, 64_000),
                "tool_top_k": Param("int", 8, 1, 10),
            },
            native_chunk_identity=True,
            consumes_documents=False,
            extra="lab-langchain",
        ),
        PipelineSpec(
            "langchain.pg_custom",
            "langchain",
            "Custom LangChain VectorStore on pgvector (lab database)",
            "LS-1",
            _NATIVE_ONLY,
            {"k": _K},
            native_chunk_identity=True,
            needs_lab_database=True,
            extra="lab-langchain",
        ),
        PipelineSpec(
            "langchain_postgres.pgvector",
            "langchain_postgres",
            "Official langchain-postgres PGVectorStore (isolated environment)",
            "LS-2",
            _NATIVE_ONLY,
            {"k": _K},
            native_chunk_identity=True,
            needs_lab_database=True,
            extra="isolated:langchain-postgres",
        ),
        PipelineSpec(
            "llamaindex.native_retriever",
            "llamaindex",
            "LlamaIndex query engine over native BuildLens retrieval",
            "LI-A",
            _NATIVE_ONLY,
            {
                "mode": _MODE,
                "rerank": _RERANK,
                "response_mode": Param(
                    "choice",
                    "compact",
                    choices=(
                        "compact",
                        "refine",
                        "tree_summarize",
                        "simple_summarize",
                        "accumulate",
                        "no_text",
                    ),
                ),
            },
            native_chunk_identity=True,
            consumes_documents=False,
            extra="lab-llamaindex",
        ),
        PipelineSpec(
            "llamaindex.vector",
            "llamaindex",
            "LlamaIndex VectorStoreIndex / BM25 over BuildLens chunks",
            "LI-B",
            _ANY,
            {
                "k": _K,
                "retriever": Param("choice", "vector", choices=("vector", "bm25")),
                "prev_next": Param("int", 0, 0, 4, help="PrevNextNodePostprocessor num_nodes"),
                "similarity_cutoff": Param("float", -1.0, -1.0, 1.0, help="-1 disables"),
                "long_context_reorder": Param("bool", False),
                "metadata_in_embedding": Param(
                    "bool", False, help="Let LlamaIndex prefix metadata into embedded text"
                ),
                "response_mode": Param(
                    "choice", "no_text", choices=("no_text", "compact", "tree_summarize")
                ),
            },
            native_chunk_identity=True,
            extra="lab-llamaindex",
        ),
        PipelineSpec(
            "llamaindex.fusion",
            "llamaindex",
            "LlamaIndex QueryFusionRetriever (vector + BM25)",
            "AR-1",
            _ANY,
            {
                "k": _K,
                "fusion_mode": Param(
                    "choice",
                    "reciprocal_rerank",
                    choices=("reciprocal_rerank", "relative_score", "dist_based_score", "simple"),
                ),
            },
            native_chunk_identity=True,
            extra="lab-llamaindex",
        ),
        PipelineSpec(
            "llamaindex.ingest",
            "llamaindex",
            "LlamaIndex IngestionPipeline + VectorStoreIndex",
            "LI-C",
            _SOURCES,
            {
                "k": _K,
                "parser": Param(
                    "choice", "sentence", choices=("sentence", "markdown", "token", "code")
                ),
                "chunk_size": _CHUNK,
                "chunk_overlap": _OVERLAP,
                "metadata_in_embedding": Param("bool", False),
                "rerank": _RERANK,
                "response_mode": Param("choice", "compact", choices=("compact", "no_text")),
            },
            extra="lab-llamaindex",
        ),
        PipelineSpec(
            "llamaindex.parent_child",
            "llamaindex",
            "LlamaIndex RecursiveRetriever small-to-big (IndexNode children)",
            "AR-2",
            _SOURCES,
            {
                "k": Param("int", 8, 1, 50),
                "child_chunk_size": Param("int", 256, 32, 4096),
                "parent_chunk_size": Param("int", 1024, 128, 16384),
            },
            extra="lab-llamaindex",
        ),
        PipelineSpec(
            "llamaindex.auto_merging",
            "llamaindex",
            "LlamaIndex HierarchicalNodeParser + AutoMergingRetriever",
            "AR-3",
            _SOURCES,
            {
                "k": _K,
                "chunk_sizes": Param("int_list", [2048, 512, 128], 32, 16384),
                "simple_ratio_thresh": Param("float", 0.5, 0.01, 1.0),
            },
            extra="lab-llamaindex",
        ),
        PipelineSpec(
            "llamaindex.sentence_window",
            "llamaindex",
            "LlamaIndex SentenceWindowNodeParser + MetadataReplacementPostProcessor",
            "AR-4",
            _SOURCES,
            {"k": _K, "window_size": Param("int", 3, 0, 20), "max_sentence_chars": _MAX_SENTENCE},
            extra="lab-llamaindex",
        ),
        PipelineSpec(
            "llamaindex.workflow",
            "llamaindex",
            "LlamaIndex Workflow re-expressing native Stage 9 retrieval",
            "LI-WF",
            _NATIVE_ONLY,
            {"mode": _MODE, "rerank": _RERANK},
            native_chunk_identity=True,
            consumes_documents=False,
            extra="lab-llamaindex",
        ),
        PipelineSpec(
            "llamaindex.pgvector",
            "llamaindex",
            "LlamaIndex PGVectorStore (lab database)",
            "LS-3",
            _NATIVE_ONLY,
            {"k": _K},
            native_chunk_identity=True,
            needs_lab_database=True,
            extra="lab-pgvector",
        ),
    )
}


# --------------------------------------------------------------------------------------
# Experiment configuration
# --------------------------------------------------------------------------------------

_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_FILTER_LIMITS = {"locator_prefix": 2048, "language": 64, "kind": 32}


@dataclass(frozen=True, slots=True)
class ExperimentConfig:
    name: str
    pipeline: str
    corpus_mode: CorpusMode
    include_unsupported: bool
    embedding_source: EmbeddingSource
    params: dict[str, Any]
    filters: dict[str, str | None]
    contract: str = LAB_CONTRACT_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "name": self.name,
            "pipeline": self.pipeline,
            "corpus": {
                "mode": self.corpus_mode,
                "include_unsupported": self.include_unsupported,
                "filters": dict(self.filters),
            },
            "embedding": {"source": self.embedding_source},
            "params": dict(self.params),
        }

    @property
    def digest(self) -> str:
        return lab_digest(self.as_dict())

    @property
    def spec(self) -> PipelineSpec:
        return PIPELINES[self.pipeline]

    def ingestion_identity(self) -> dict[str, Any]:
        """Everything that changes index contents (not query-time parameters)."""
        query_only = {
            "k",
            "search_type",
            "fetch_k",
            "lambda_mult",
            "score_threshold",
            "synthesis",
            "rerank",
            "response_mode",
            "vector_weight",
            "c",
            "dedupe_by",
            "fusion_mode",
            "prev_next",
            "similarity_cutoff",
            "long_context_reorder",
            "merge_ratio",
            "simple_ratio_thresh",
            "mode",
            "retriever",
            "max_model_calls",
            "max_tool_calls",
            "evidence_budget_bytes",
            "tool_top_k",
        }
        return {
            "pipeline": self.pipeline,
            "corpus_mode": self.corpus_mode,
            "include_unsupported": self.include_unsupported,
            "embedding_source": self.embedding_source,
            "params": {k: v for k, v in self.params.items() if k not in query_only},
        }


def parse_config(raw: Any) -> ExperimentConfig:
    """Strict validation with defaults filled in; unknown keys are rejected everywhere."""
    if not isinstance(raw, dict):
        raise LabError("CONFIG_INVALID", "Configuration must be an object")
    allowed = {"contract", "name", "pipeline", "corpus", "embedding", "params"}
    if set(raw) - allowed:
        raise LabError("CONFIG_INVALID", f"Unknown keys: {sorted(set(raw) - allowed)}")
    if raw.get("contract", LAB_CONTRACT_VERSION) != LAB_CONTRACT_VERSION:
        raise LabError("CONFIG_CONTRACT_UNSUPPORTED")
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise LabError("CONFIG_INVALID", "name must match [a-z0-9][a-z0-9._-]{0,79}")
    pipeline = raw.get("pipeline")
    if pipeline not in PIPELINES:
        raise LabError("CONFIG_PIPELINE_UNKNOWN", f"Unknown pipeline {pipeline!r}")
    spec = PIPELINES[pipeline]
    corpus = raw.get("corpus", {})
    if not isinstance(corpus, dict) or set(corpus) - {"mode", "include_unsupported", "filters"}:
        raise LabError("CONFIG_INVALID", "corpus has unknown keys")
    mode = corpus.get("mode", spec.corpus_modes[0])
    if mode not in spec.corpus_modes:
        raise LabError("CONFIG_CORPUS_MODE_UNSUPPORTED", f"{pipeline} supports {spec.corpus_modes}")
    include_unsupported = corpus.get("include_unsupported", False)
    if type(include_unsupported) is not bool or (
        include_unsupported and mode != "artifact_sources"
    ):
        raise LabError("CONFIG_INVALID", "include_unsupported requires artifact_sources")
    filters_raw = corpus.get("filters") or {}
    if not isinstance(filters_raw, dict) or set(filters_raw) - set(_FILTER_LIMITS):
        raise LabError("CONFIG_INVALID", "filters has unknown keys")
    filters: dict[str, str | None] = {}
    for key, limit in _FILTER_LIMITS.items():
        value = filters_raw.get(key)
        if value is not None and (
            not isinstance(value, str) or not value or len(value.encode()) > limit
        ):
            raise LabError("CONFIG_INVALID", f"filter {key} is invalid")
        filters[key] = value
    if filters["kind"] is not None and mode != "native_chunks":
        # Whole artifacts have no chunk kind; the filter used to be silently ignored.
        raise LabError("CONFIG_INVALID", "filter kind applies to native_chunks only")
    embedding = raw.get("embedding", {})
    if not isinstance(embedding, dict) or set(embedding) - {"source"}:
        raise LabError("CONFIG_INVALID", "embedding has unknown keys")
    default_source = "reuse-stored" if mode == "native_chunks" else "compute"
    source = embedding.get("source", default_source)
    if source not in {"reuse-stored", "compute", "reuse-or-compute", "deterministic-fake"}:
        raise LabError("CONFIG_INVALID", "embedding.source is invalid")
    if source in {"reuse-stored", "reuse-or-compute"} and mode != "native_chunks":
        if source == "reuse-stored":
            raise LabError(
                "CONFIG_INVALID", "reuse-stored requires native_chunks (no stored vectors exist)"
            )
    params_raw = raw.get("params", {})
    if not isinstance(params_raw, dict) or set(params_raw) - set(spec.params):
        raise LabError(
            "CONFIG_INVALID", f"Unknown params: {sorted(set(params_raw) - set(spec.params))}"
        )
    params = {
        key: param.validate(key, params_raw.get(key, param.default))
        for key, param in sorted(spec.params.items())
    }
    if "chunk_overlap" in params and params["chunk_overlap"] >= params.get("chunk_size", 1 << 30):
        raise LabError("CONFIG_PARAMETER_INVALID", "chunk_overlap must be < chunk_size")
    if "chunk_sizes" in params and params["chunk_sizes"] != sorted(
        params["chunk_sizes"], reverse=True
    ):
        raise LabError("CONFIG_PARAMETER_INVALID", "chunk_sizes must be descending")
    return ExperimentConfig(
        name=name,
        pipeline=pipeline,
        corpus_mode=mode,
        include_unsupported=include_unsupported,
        embedding_source=source,
        params=params,
        filters=filters,
    )


# --------------------------------------------------------------------------------------
# Results and traces
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LabItem:
    """One ranked or delivered unit. Source text is referenced by hash, not duplicated."""

    ref: str
    rank: int
    text_sha256: str
    text_bytes: int
    locator: str | None
    artifact_fingerprint: str | None
    start_offset: int | None
    end_offset: int | None
    anchored: bool
    chunk_id: str | None = None
    score: float | None = None
    score_kind: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "rank": self.rank,
            "text_sha256": self.text_sha256,
            "text_bytes": self.text_bytes,
            "locator": self.locator,
            "artifact_fingerprint": self.artifact_fingerprint,
            "span": {"start_offset": self.start_offset, "end_offset": self.end_offset}
            if self.anchored
            else None,
            "anchored": self.anchored,
            "chunk_id": self.chunk_id,
            "score": self.score,
            "score_kind": self.score_kind,
            "detail": self.detail,
        }

    @staticmethod
    def from_dict(value: dict[str, Any]) -> LabItem:
        span = value.get("span") or {}
        return LabItem(
            ref=str(value["ref"]),
            rank=int(value["rank"]),
            text_sha256=str(value["text_sha256"]),
            text_bytes=int(value["text_bytes"]),
            locator=value.get("locator"),
            artifact_fingerprint=value.get("artifact_fingerprint"),
            start_offset=span.get("start_offset"),
            end_offset=span.get("end_offset"),
            anchored=bool(value.get("anchored")) and bool(span),
            chunk_id=value.get("chunk_id"),
            score=value.get("score"),
            score_kind=value.get("score_kind"),
            detail=dict(value.get("detail") or {}),
        )


@dataclass(frozen=True, slots=True)
class LabStep:
    seq: int
    kind: str
    name: str
    framework_event: str
    duration_ms: float
    attributes: dict[str, Any] = field(default_factory=dict)
    item_refs: tuple[str, ...] = ()
    parent: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind if self.kind in STEP_KINDS else "other",
            "name": self.name,
            "framework_event": self.framework_event,
            "duration_ms": round(self.duration_ms, 3),
            "attributes": self.attributes,
            "item_refs": list(self.item_refs),
            "parent": self.parent,
        }


@dataclass(frozen=True, slots=True)
class LabResult:
    pipeline: str
    question: str
    retrieved: tuple[LabItem, ...]
    context: tuple[LabItem, ...]
    steps: tuple[dict[str, Any], ...]
    answer: dict[str, Any] | None
    timings: dict[str, float]
    diagnostics: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "pipeline": self.pipeline,
            "question": self.question,
            "retrieved": [item.as_dict() for item in self.retrieved],
            "context": [item.as_dict() for item in self.context],
            "steps": list(self.steps),
            "answer": self.answer,
            "timings": self.timings,
            "diagnostics": list(self.diagnostics),
        }


def anchored(document_text: str, start: int | None, text: str) -> bool:
    """A node is anchored only when its exact text occupies [start, start+len) of the source."""
    return (
        start is not None
        and 0 <= start <= len(document_text)
        and document_text[start : start + len(text)] == text
    )


def pack_context(items: list[LabItem]) -> tuple[tuple[LabItem, ...], dict[str, Any]]:
    """Shared lab packing: rank order, whole items, skip-if-does-not-fit, same native caps."""
    packed: list[LabItem] = []
    used = 0
    skipped: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item.ref in seen:
            continue
        seen.add(item.ref)
        if len(packed) >= LAB_CONTEXT_ITEM_LIMIT:
            skipped.append(item.ref)
            continue
        if used + item.text_bytes > LAB_CONTEXT_BYTE_LIMIT:
            skipped.append(item.ref)
            continue
        packed.append(item)
        used += item.text_bytes
    return tuple(packed), {
        "version": LAB_PACKING_VERSION,
        "item_limit": LAB_CONTEXT_ITEM_LIMIT,
        "byte_limit": LAB_CONTEXT_BYTE_LIMIT,
        "used_bytes": used,
        "packed": len(packed),
        "skipped": skipped,
        "note": "Content bytes only; native packing measures canonical JSON incl. framing.",
    }
