"""LlamaIndex worker entry point: guard first, then framework imports, then serve."""

from __future__ import annotations

from buildlens.infrastructure.lab.workers.common import guard

guard.install_from_environment()

import importlib.metadata  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
from typing import Any  # noqa: E402

from llama_index.core import Settings  # noqa: E402
from llama_index.core.instrumentation import get_dispatcher  # noqa: E402

from buildlens.domain.lab import LabError  # noqa: E402
from buildlens.infrastructure.lab.workers.common.items import DocumentSet  # noqa: E402
from buildlens.infrastructure.lab.workers.common.runtime import (  # noqa: E402
    EmbeddingLedger,
    WorkerSession,
    serve,
)
from buildlens.infrastructure.lab.workers.llamaindex import pipelines  # noqa: E402
from buildlens.infrastructure.lab.workers.llamaindex.adapters import (  # noqa: E402
    BuildLensLlamaEmbedding,
    ForbiddenEmbedding,
    OfflineLLM,
    TraceEvents,
)

PACKAGES = (
    "llama-index-core",
    "llama-index-retrievers-bm25",
    "llama-index-workflows",
    "bm25s",
    "nltk",
    "tiktoken",
)

# Global Settings are process-wide in LlamaIndex. Install sentinels so any component that
# silently falls back to a global default fails loudly instead of resolving OpenAI/Mock.
Settings.llm = OfflineLLM(forbidden=True)
Settings.embed_model = ForbiddenEmbedding()
TRACE = TraceEvents()
get_dispatcher().add_event_handler(TRACE)


def versions() -> dict[str, str]:
    result = {"python": sys.version.split()[0]}
    for package in PACKAGES:
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = "not-installed"
    return result


def build_begin(session: WorkerSession, params: dict[str, Any]) -> Any:
    directory = str(params["directory"])
    if not os.path.isdir(directory):
        raise LabError("WORKER_INDEX_DIRECTORY_MISSING")
    session.state["pending"] = {
        "index_id": str(params["index_id"]),
        "config": dict(params["config"]),
        "directory": directory,
        "documents": DocumentSet(),
        "native": params["corpus_mode"] == "native_chunks",
        "embedding_source": str(params["embedding_source"]),
    }
    return {"ok": True}


def add_documents(session: WorkerSession, params: dict[str, Any]) -> Any:
    pending = session.state.get("pending")
    if pending is None:
        raise LabError("WORKER_BUILD_NOT_STARTED")
    pending["documents"].add(list(params["documents"]))
    return {"received": len(pending["documents"].documents)}


def build_commit(session: WorkerSession, params: dict[str, Any]) -> Any:
    pending = session.state.pop("pending", None)
    if pending is None:
        raise LabError("WORKER_BUILD_NOT_STARTED")
    TRACE.bind(session)
    documents: DocumentSet = pending["documents"]
    ledger = EmbeddingLedger(session, pending["embedding_source"], documents.reuse)
    state = pipelines.IndexState(
        index_id=pending["index_id"],
        config=pending["config"],
        documents=documents,
        ledger=ledger,
        native=pending["native"],
        directory=pending["directory"],
        embed_model=BuildLensLlamaEmbedding(ledger),
    )
    manifest = pipelines.build(session, state)
    manifest["embedding"] = ledger.summary()
    manifest["embedding_records"] = ledger.records[:5000]
    session.state.setdefault("indexes", {})[state.index_id] = state
    return manifest


def query(session: WorkerSession, params: dict[str, Any]) -> Any:
    TRACE.bind(session)
    state = session.state.setdefault("indexes", {}).get(params.get("index_id"))
    if state is None:
        raise LabError("LAB_INDEX_NOT_LOADED")
    pipelines.TEXTS.clear()
    if params.get("reload_from_storage") and state.index is not None:
        with session.tracer.step("load", "load_index_from_storage", "llama_index.storage"):
            state.index = pipelines.reload_index(state)
    result = pipelines.query(session, state, str(params["question"]), dict(params["config"]))
    result["embedding"] = state.ledger.summary()
    result["texts"] = dict(pipelines.TEXTS)
    return result


def drop(session: WorkerSession, params: dict[str, Any]) -> Any:
    session.state.setdefault("indexes", {}).pop(params.get("index_id"), None)
    return {"loaded": sorted(session.state.get("indexes", {}))}


def settings_probe(session: WorkerSession, params: dict[str, Any]) -> Any:
    """Prove the global-default sentinels are active (used by tests)."""
    outcomes = {}
    for name, action in (
        ("llm", lambda: Settings.llm.complete("x")),
        ("embed", lambda: Settings.embed_model.get_text_embedding("x")),
    ):
        try:
            action()
            outcomes[name] = "used"
        except LabError as error:
            outcomes[name] = error.code
    return outcomes


def tools_schema(session: WorkerSession, params: dict[str, Any]) -> Any:
    from llama_index.core.tools import FunctionTool

    def buildlens_search(query: str) -> str:
        """Search repository evidence (read-only)."""
        return query

    def buildlens_read_chunk(chunk_id: str) -> str:
        """Read one chunk's exact source (read-only)."""
        return chunk_id

    return {
        tool.metadata.name: tool.metadata.to_openai_tool()
        for tool in (
            FunctionTool.from_defaults(buildlens_search),
            FunctionTool.from_defaults(buildlens_read_chunk),
        )
    }


def load_directory(session: WorkerSession, params: dict[str, Any]) -> Any:
    from buildlens.infrastructure.lab.workers.llamaindex.loaders import load_directory as run

    return run(session, params)


if __name__ == "__main__":
    serve(
        "llamaindex",
        versions,
        {
            "build_begin": build_begin,
            "add_documents": add_documents,
            "build_commit": build_commit,
            "query": query,
            "drop": drop,
            "settings_probe": settings_probe,
            "tools_schema": tools_schema,
            "load_directory": load_directory,
        },
    )
