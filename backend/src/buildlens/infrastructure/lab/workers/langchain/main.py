"""LangChain worker entry point: guard first, then framework imports, then serve."""

from __future__ import annotations

from buildlens.infrastructure.lab.workers.common import guard

guard.install_from_environment()

import importlib.metadata  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
from typing import Any  # noqa: E402

from buildlens.domain.lab import LabError  # noqa: E402
from buildlens.infrastructure.lab.workers.common.items import DocumentSet  # noqa: E402
from buildlens.infrastructure.lab.workers.common.runtime import (  # noqa: E402
    EmbeddingLedger,
    WorkerSession,
    serve,
)
from buildlens.infrastructure.lab.workers.langchain import pipelines  # noqa: E402

PACKAGES = (
    "langchain",
    "langchain-core",
    "langchain-classic",
    "langchain-text-splitters",
    "langchain-community",
    "langgraph",
    "bm25s",
)


def versions() -> dict[str, str]:
    result = {"python": sys.version.split()[0]}
    for package in PACKAGES:
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = "not-installed"
    return result


def _pending(session: WorkerSession) -> dict[str, Any]:
    pending = session.state.get("pending")
    if pending is None:
        raise LabError("WORKER_BUILD_NOT_STARTED")
    return dict(pending)


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
    pending = _pending(session)
    pending["documents"].add(list(params["documents"]))
    return {"received": len(pending["documents"].documents)}


def build_commit(session: WorkerSession, params: dict[str, Any]) -> Any:
    pending = _pending(session)
    session.state.pop("pending")
    documents: DocumentSet = pending["documents"]
    ledger = EmbeddingLedger(session, pending["embedding_source"], documents.reuse)
    state = pipelines.IndexState(
        index_id=pending["index_id"],
        config=pending["config"],
        documents=documents,
        ledger=ledger,
        native=pending["native"],
    )
    manifest = pipelines.build(session, state, pending["directory"])
    manifest["embedding"] = ledger.summary()
    manifest["embedding_records"] = ledger.records[:5000]
    session.state.setdefault("indexes", {})[state.index_id] = state
    return manifest


def query(session: WorkerSession, params: dict[str, Any]) -> Any:
    index_id = params.get("index_id")
    indexes = session.state.setdefault("indexes", {})
    state = indexes.get(index_id)
    if state is None:
        raise LabError("LAB_INDEX_NOT_LOADED")
    pipelines.TEXTS.clear()
    result = pipelines.query(session, state, str(params["question"]), dict(params["config"]))
    result["embedding"] = state.ledger.summary()
    result["texts"] = {sha: text for sha, text in pipelines.TEXTS.items()}
    return result


def drop(session: WorkerSession, params: dict[str, Any]) -> Any:
    session.state.setdefault("indexes", {}).pop(params.get("index_id"), None)
    return {"loaded": sorted(session.state.get("indexes", {}))}


def tools_schema(session: WorkerSession, params: dict[str, Any]) -> Any:
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from buildlens.infrastructure.lab.workers.langchain.agent import ReadArgs, SearchArgs

    return {
        "buildlens_search": convert_to_openai_tool(SearchArgs),
        "buildlens_read_chunk": convert_to_openai_tool(ReadArgs),
    }


def load_directory(session: WorkerSession, params: dict[str, Any]) -> Any:
    from buildlens.infrastructure.lab.workers.langchain.loaders import load_directory as run

    return run(session, params)


if __name__ == "__main__":
    serve(
        "langchain",
        versions,
        {
            "build_begin": build_begin,
            "add_documents": add_documents,
            "build_commit": build_commit,
            "query": query,
            "drop": drop,
            "tools_schema": tools_schema,
            "load_directory": load_directory,
        },
    )
