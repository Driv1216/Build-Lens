"""LangChain pipeline construction and querying inside the worker process."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_classic.retrievers import (
    ContextualCompressionRetriever,
    EnsembleRetriever,
    ParentDocumentRetriever,
)
from langchain_core.documents import Document
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import (
    RunnableConfig,
    RunnableLambda,
    RunnableParallel,
    RunnablePassthrough,
)
from langchain_core.stores import InMemoryStore
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_text_splitters import (
    Language,
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

from buildlens.domain.lab import LabError, LabItem, pack_context
from buildlens.infrastructure.lab.workers.common.items import (
    CITED_ANSWER_SCHEMA,
    DocumentSet,
    evidence_block,
    item,
    validate_citations,
)
from buildlens.infrastructure.lab.workers.common.runtime import (
    EmbeddingLedger,
    WorkerSession,
    float32_digest,
)
from buildlens.infrastructure.lab.workers.langchain.adapters import (
    BM25Retriever,
    BuildLensEmbeddings,
    CrossEncoderCompressor,
    NativeBridgeRetriever,
    OfflineChatModel,
    TraceCallback,
)

SYSTEM_PROMPT = (
    "Answer only from the numbered evidence. Cite evidence identifiers like [E1] for every "
    "statement. If the evidence is insufficient, return status insufficient_evidence."
)
_LANGUAGES = {
    ".py": Language.PYTHON,
    ".md": Language.MARKDOWN,
    ".ts": Language.TS,
    ".tsx": Language.TS,
    ".js": Language.JS,
    ".jsx": Language.JS,
    ".go": Language.GO,
    ".rs": Language.RUST,
    ".java": Language.JAVA,
}


@dataclass(slots=True)
class IndexState:
    index_id: str
    config: dict[str, Any]
    documents: DocumentSet
    ledger: EmbeddingLedger
    native: bool
    store: InMemoryVectorStore | None = None
    nodes: dict[str, Document] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)  # artifact ref -> text
    extra: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)


def _documents(state: IndexState) -> list[Document]:
    return [
        Document(page_content=d["text"], metadata={**d["metadata"], "ref": d["ref"]}, id=d["ref"])
        for d in state.documents.documents
    ]


def _splitter(suffix: str, size: int, overlap: int, kind: str) -> RecursiveCharacterTextSplitter:
    if kind == "recursive_language" and suffix in _LANGUAGES:
        return RecursiveCharacterTextSplitter.from_language(
            _LANGUAGES[suffix], chunk_size=size, chunk_overlap=overlap, add_start_index=True
        )
    return RecursiveCharacterTextSplitter(
        chunk_size=size, chunk_overlap=overlap, add_start_index=True
    )


def _split_sources(state: IndexState, size: int, overlap: int, kind: str) -> list[Document]:
    """Split artifact documents; every child records its parent artifact and offset."""
    result: list[Document] = []
    for parent in _documents(state):
        suffix = parent.metadata.get("suffix") or ""
        if kind == "markdown_header" and suffix == ".md":
            pieces = MarkdownHeaderTextSplitter(
                [("#", "h1"), ("##", "h2"), ("###", "h3")], strip_headers=False
            ).split_text(parent.page_content)
            children = []
            for piece in pieces:
                for sub in _splitter(suffix, size, overlap, "recursive").split_documents([piece]):
                    children.append(sub)
            hint_known = False
        else:
            children = _splitter(suffix, size, overlap, kind).split_documents([parent])
            hint_known = True
        for index, child in enumerate(children):
            start = child.metadata.get("start_index") if hint_known else None
            ref = f"{parent.id}:{start if start is not None else 'n' + str(index)}"
            meta = {k: v for k, v in parent.metadata.items() if k not in {"start_index", "ref"}}
            meta.update({"ref": ref, "parent_ref": parent.id, "start_hint": start})
            for key in ("h1", "h2", "h3"):
                if key in child.metadata:
                    meta[key] = child.metadata[key]
            result.append(Document(page_content=child.page_content, metadata=meta, id=ref))
    return result


def _vector_store(state: IndexState, documents: list[Document]) -> InMemoryVectorStore:
    store = InMemoryVectorStore(BuildLensEmbeddings(state.ledger))
    for start in range(0, len(documents), 256):
        batch = documents[start : start + 256]
        store.add_documents(batch, ids=[d.id for d in batch])
    return store


TEXTS: dict[str, str] = {}  # text sha256 -> text for the current request (one at a time)


def remember(lab_item: dict[str, Any], text: str) -> dict[str, Any]:
    TEXTS[lab_item["text_sha256"]] = text
    return lab_item


# ---------------------------------------------------------------------------- build


def build(session: WorkerSession, state: IndexState, directory: str) -> dict[str, Any]:
    pipeline = state.config["pipeline"]
    params = state.config["params"]
    native = state.native
    if not native:
        state.sources = {d["ref"]: d["text"] for d in state.documents.documents}
    with session.tracer.step("split", "prepare documents", "langchain.build") as attrs:
        if pipeline in {"langchain.native_retriever", "langchain.agent"}:
            documents: list[Document] = []
        elif pipeline in {"langchain.vector", "langchain.ensemble"}:
            documents = (
                _documents(state)
                if native
                else _split_sources(state, 1024, 0, "recursive_language")
            )
        elif pipeline == "langchain.ingest":
            documents = _split_sources(
                state, params["chunk_size"], params["chunk_overlap"], params["splitter"]
            )
        elif pipeline in {"langchain.parent_document", "langchain.pg_custom"}:
            documents = []  # handled below
        elif pipeline == "langchain.auto_merge":
            documents = _hierarchy(state, params["chunk_sizes"])
        elif pipeline == "langchain.sentence_window":
            sentences = _sentences(state)
            documents = [
                d for d in sentences if len(d.page_content) <= params["max_sentence_chars"]
            ]
            state.manifest["dropped_oversized_sentences"] = len(sentences) - len(documents)
        else:
            raise LabError("WORKER_PIPELINE_UNSUPPORTED", pipeline)
        attrs["documents"] = len(documents)
    if pipeline == "langchain.parent_document":
        return _build_parent_document(session, state, params, directory)
    if pipeline == "langchain.pg_custom":
        return _build_pg_custom(session, state, _documents(state))
    if documents:
        with session.tracer.step(
            "embed", "InMemoryVectorStore.add_documents", "langchain.vectorstore"
        ) as attrs:
            state.store = _vector_store(state, documents)
            attrs["indexed"] = len(documents)
        state.nodes = {str(d.id): d for d in documents}
        with session.tracer.step(
            "store", "InMemoryVectorStore.dump", "langchain.vectorstore"
        ) as attrs:
            path = os.path.join(directory, "langchain_vectorstore.json")
            state.store.dump(path)
            attrs["bytes"] = os.path.getsize(path)
        state.manifest["vector_digests"] = _dumped_digests(path)
    if pipeline == "langchain.ensemble":
        state.extra["bm25"] = BM25Retriever(documents=documents, k=params["k"])
    state.manifest.update(
        {"nodes": len(documents), "anchor_methods": _anchor_census(state, documents)}
    )
    return state.manifest


def _build_pg_custom(
    session: WorkerSession, state: IndexState, documents: list[Document]
) -> dict[str, Any]:
    from sqlalchemy import select

    from buildlens.infrastructure.lab.workers.langchain.pg_store import BuildLensLabPgVectorStore

    dimensions = len(next(iter(state.documents.reuse.values()), [0.0] * 384))
    store = BuildLensLabPgVectorStore(
        BuildLensEmbeddings(state.ledger), f"lc_custom_{state.index_id[:16]}", dimensions
    )
    with session.tracer.step(
        "store", "BuildLensLabPgVectorStore.add_documents", "langchain.vectorstore.pgvector"
    ) as attrs:
        store.delete()
        store.add_documents(documents, ids=[str(d.id) for d in documents])
        attrs["rows"] = store.count()
    with store._engine.connect() as connection:  # read back what PostgreSQL actually stored
        rows = connection.execute(select(store._table.c.id, store._table.c.embedding))
        state.manifest["vector_digests"] = {
            r.id: float32_digest([float(v) for v in r.embedding]) for r in rows
        }
    state.extra["pg_store"] = store
    state.nodes = {str(d.id): d for d in documents}
    state.manifest.update(
        {
            "nodes": len(documents),
            "table": f"lab.lc_custom_{state.index_id[:16]}",
            "anchor_methods": {"native": len(documents)},
        }
    )
    return state.manifest


def _dumped_digests(path: str) -> dict[str, str]:
    """Vector digests read back from LangChain's own public dump (not private state)."""
    with open(path, encoding="utf-8") as stream:
        raw = json.load(stream)
    store = raw.get("store", raw)
    digests = {}
    for key, value in store.items():
        vector = value.get("vector") if isinstance(value, dict) else None
        if vector is not None:
            digests[str(key)] = float32_digest(vector)
    return digests


def _anchor_census(state: IndexState, documents: list[Document]) -> dict[str, int]:
    census: dict[str, int] = {}
    spans: dict[str, list[list[Any]]] = {}
    for document in documents:
        lab = _item(state, document, 0, None, "none")
        method = lab["detail"]["anchor_method"]
        census[method] = census.get(method, 0) + 1
        span = lab.get("span") or {}
        if lab.get("locator") and sum(len(v) for v in spans.values()) < 20_000:
            spans.setdefault(str(lab["locator"]), []).append(
                [span.get("start_offset"), span.get("end_offset"), method, lab["ref"]]
            )
    state.manifest["node_spans"] = spans
    return census


def _build_parent_document(
    session: WorkerSession, state: IndexState, params: dict[str, Any], directory: str
) -> dict[str, Any]:
    parents: list[Document] = []
    if params["parent_chunk_size"] > 0:
        parents = _split_sources(state, params["parent_chunk_size"], 0, "recursive_language")
    else:
        for document in _documents(state):
            document.metadata["start_hint"] = 0
            parents.append(document)
    store = InMemoryVectorStore(BuildLensEmbeddings(state.ledger))
    retriever = ParentDocumentRetriever(
        vectorstore=store,
        docstore=InMemoryStore(),
        child_splitter=RecursiveCharacterTextSplitter(
            chunk_size=params["child_chunk_size"], chunk_overlap=0, add_start_index=True
        ),
        search_kwargs={"k": max(params["k"] * 4, 16)},
    )
    with session.tracer.step(
        "embed", "ParentDocumentRetriever.add_documents", "langchain.parent_document"
    ) as attrs:
        retriever.add_documents(parents, ids=[str(p.id) for p in parents])
        attrs["parents"] = len(parents)
    state.store = store
    state.nodes = {str(p.id): p for p in parents}
    state.extra["retriever"] = retriever
    path = os.path.join(directory, "langchain_child_vectorstore.json")
    store.dump(path)
    state.manifest.update(
        {
            "nodes": len(parents),
            "children": len(_dumped_digests(path)),
            "vector_digests": _dumped_digests(path),
            "anchor_methods": _anchor_census(state, parents),
        }
    )
    return state.manifest


def _hierarchy(state: IndexState, sizes: list[int]) -> list[Document]:
    """Hand-built hierarchy: level-0 chunks split into smaller levels; leaves indexed."""
    leaves: list[Document] = []
    parents: dict[str, Document] = {}
    children_of: dict[str, list[str]] = {}
    for artifact in _documents(state):
        level: list[tuple[Document, int, str | None]] = [(artifact, 0, None)]
        for depth, size in enumerate(sizes):
            next_level: list[tuple[Document, int, str | None]] = []
            for node, base, parent_ref in level:
                pieces = _splitter(
                    artifact.metadata.get("suffix") or "", size, 0, "recursive_language"
                ).split_text(node.page_content)
                cursor = 0
                for piece in pieces:
                    offset = node.page_content.find(piece, cursor)
                    if offset < 0:
                        continue
                    cursor = offset + 1
                    start = base + offset
                    ref = f"{artifact.id}:L{depth}:{start}"
                    meta = {
                        **{k: v for k, v in artifact.metadata.items() if k != "ref"},
                        "ref": ref,
                        "parent_ref": parent_ref,
                        "start_hint": start,
                        "level": depth,
                        "artifact_ref": artifact.id,
                    }
                    document = Document(page_content=piece, metadata=meta, id=ref)
                    if parent_ref:
                        children_of.setdefault(parent_ref, []).append(ref)
                    next_level.append((document, start, ref))
            if depth < len(sizes) - 1:
                for document, _, made in next_level:
                    if made is not None:
                        parents[made] = document
            else:
                leaves.extend(document for document, _, _ in next_level)
            level = next_level
    state.extra["parents"] = parents
    state.extra["children_of"] = children_of
    return leaves


_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n\s*\n|\n(?=\s*(?:def |class |#|- |\* |\d+\. ))")


def _sentences(state: IndexState) -> list[Document]:
    result: list[Document] = []
    ordered: dict[str, list[str]] = {}
    for artifact in _documents(state):
        text = artifact.page_content
        start = 0
        index = 0
        for match in list(_SENTENCE.finditer(text)) + [None]:
            end = match.start() if match else len(text)
            piece = text[start:end]
            if piece.strip():
                ref = f"{artifact.id}:S{index}"
                meta = {
                    **{k: v for k, v in artifact.metadata.items() if k != "ref"},
                    "ref": ref,
                    "start_hint": start,
                    "artifact_ref": artifact.id,
                    "position": index,
                }
                result.append(Document(page_content=piece, metadata=meta, id=ref))
                ordered.setdefault(str(artifact.id), []).append(ref)
                index += 1
            start = match.end() if match else len(text)
    state.extra["ordered"] = ordered
    return result


# ---------------------------------------------------------------------------- query


def _item(
    state: IndexState,
    document: Document,
    rank: int,
    score: float | None,
    score_kind: str,
    detail: dict[str, Any] | None = None,
    span: tuple[int, int] | None = None,
) -> dict[str, Any]:
    meta = document.metadata
    source = None
    if not state.native:
        source = state.sources.get(
            str(meta.get("artifact_ref") or meta.get("parent_ref") or meta.get("artifact_id") or "")
        )
        if source is None:
            source = state.sources.get(str(meta.get("artifact_id")))
    return remember(
        item(
            ref=str(document.id or meta.get("ref")),
            rank=rank,
            text=document.page_content,
            metadata=meta,
            source_text=source,
            start_hint=meta.get("start_hint"),
            score=score,
            score_kind=score_kind,
            detail=detail,
            span_override=span,
        ),
        document.page_content,
    )


def _items(
    state: IndexState, documents: list[Document], score_key: str | None, kind: str
) -> list[dict[str, Any]]:
    return [
        _item(state, d, rank, d.metadata.get(score_key) if score_key else None, kind)
        for rank, d in enumerate(documents, 1)
    ]


def query(
    session: WorkerSession, state: IndexState, question: str, config: dict[str, Any]
) -> dict[str, Any]:
    pipeline = config["pipeline"]
    params = config["params"]
    trace = TraceCallback(session.tracer)
    run_config: RunnableConfig = {"callbacks": [trace], "run_name": pipeline}
    filters = config.get("corpus", {}).get("filters") or {}
    if pipeline == "langchain.native_retriever":
        return _query_native(session, state, question, params, run_config, filters)
    if pipeline == "langchain.agent":
        from buildlens.infrastructure.lab.workers.langchain.agent import run_agent

        return run_agent(session, question, params, run_config)
    if pipeline == "langchain.pg_custom":
        store = state.extra.get("pg_store")
        if store is None:
            raise LabError("LAB_INDEX_NOT_LOADED")
        with session.tracer.step(
            "retrieve",
            "BuildLensLabPgVectorStore.similarity_search_with_score",
            "langchain.vectorstore.pgvector",
        ) as attrs:
            hits = store.similarity_search_with_score(question, k=params["k"])
            attrs["_refs"] = [str(d.id) for d, _ in hits]
        documents = [
            Document(page_content=d.page_content, metadata={**d.metadata, "similarity": s}, id=d.id)
            for d, s in hits
        ]
        ranked = _items(state, documents, "similarity", "cosine")
        return _finish(
            session,
            question,
            ranked,
            ranked,
            ranked,
            params,
            run_config,
            {"store": "lab pgvector (custom)"},
        )
    if state.store is None and pipeline != "langchain.parent_document":
        raise LabError("LAB_INDEX_NOT_LOADED")
    if pipeline in {"langchain.vector", "langchain.ingest"}:
        return _query_vector(session, state, question, params, run_config)
    if pipeline == "langchain.ensemble":
        return _query_ensemble(session, state, question, params, run_config)
    if pipeline == "langchain.parent_document":
        return _query_parent(session, state, question, params, run_config)
    if pipeline == "langchain.auto_merge":
        return _query_auto_merge(session, state, question, params, run_config)
    if pipeline == "langchain.sentence_window":
        return _query_window(session, state, question, params, run_config)
    raise LabError("WORKER_PIPELINE_UNSUPPORTED", pipeline)


def _scores(state: IndexState, question: str, k: int) -> dict[str, float]:
    assert state.store is not None
    vector = state.store.embeddings.embed_query(question)  # cached by the adapter
    hits = state.store.similarity_search_with_score_by_vector(vector, k=k)
    return {str(d.id): float(s) for d, s in hits}


def _with_scores(documents: list[Document], scores: dict[str, float]) -> list[Document]:
    return [
        Document(
            page_content=d.page_content,
            metadata={**d.metadata, "similarity": scores.get(str(d.id))},
            id=d.id,
        )
        for d in documents
    ]


def _query_vector(
    session: WorkerSession,
    state: IndexState,
    question: str,
    params: dict[str, Any],
    run_config: RunnableConfig,
) -> dict[str, Any]:
    assert state.store is not None
    k = params["k"]
    search_type = params.get("search_type", "similarity")
    kwargs: dict[str, Any] = {"k": k}
    if search_type == "mmr":
        kwargs.update(fetch_k=max(params["fetch_k"], k), lambda_mult=params["lambda_mult"])
    elif search_type == "similarity_score_threshold":
        kwargs["score_threshold"] = params["score_threshold"]
    retriever = state.store.as_retriever(search_type=search_type, search_kwargs=kwargs)
    scores = _scores(state, question, max(k, kwargs.get("fetch_k", k)))
    documents = _with_scores(retriever.invoke(question, config=run_config), scores)
    retrieved = _items(state, documents, "similarity", "cosine")
    ranked = retrieved
    if params.get("rerank"):
        compressor = CrossEncoderCompressor(session=session, top_n=k)
        pipeline = ContextualCompressionRetriever(
            base_compressor=compressor, base_retriever=retriever
        )
        with session.tracer.step(
            "rerank", "ContextualCompressionRetriever", "langchain.compression"
        ) as attrs:
            reranked = pipeline.invoke(question, config=run_config)
            attrs["_refs"] = [str(d.id) for d in reranked]
        ranked = _items(state, list(reranked), "rerank_score", "rerank_logit")
    return _finish(
        session,
        question,
        retrieved,
        ranked,
        ranked,
        params,
        run_config,
        {"search_type": search_type},
    )


def _query_ensemble(
    session: WorkerSession,
    state: IndexState,
    question: str,
    params: dict[str, Any],
    run_config: RunnableConfig,
) -> dict[str, Any]:
    assert state.store is not None
    k = params["k"]
    vector = state.store.as_retriever(search_kwargs={"k": k})
    bm25 = state.extra["bm25"]
    weight = params["vector_weight"]
    ensemble = EnsembleRetriever(
        retrievers=[vector, bm25],
        weights=[weight, 1.0 - weight],
        c=params["c"],
        id_key="ref" if params["dedupe_by"] == "chunk_id" else None,
    )
    with session.tracer.step(
        "fuse", "EnsembleRetriever (weighted RRF)", "langchain.ensemble"
    ) as attrs:
        fused = ensemble.invoke(question, config=run_config)[:k]
        vector_docs = vector.invoke(question)
        bm25_docs = bm25.invoke(question)
        attrs.update(
            {
                "vector_candidates": len(vector_docs),
                "bm25_candidates": len(bm25_docs),
                "fused": len(fused),
                "dedupe_by": params["dedupe_by"],
                "_refs": [str(d.id) for d in fused],
            }
        )
    vector_rank = {str(d.id): r for r, d in enumerate(vector_docs, 1)}
    bm25_rank = {str(d.id): r for r, d in enumerate(bm25_docs, 1)}
    ranked = []
    for rank, d in enumerate(fused, 1):
        ref = str(d.id)
        ranked.append(
            _item(
                state,
                d,
                rank,
                None,
                "weighted_rrf",
                {"vector_rank": vector_rank.get(ref), "bm25_rank": bm25_rank.get(ref)},
            )
        )
    return _finish(
        session,
        question,
        ranked,
        ranked,
        ranked,
        params,
        run_config,
        {"fusion": "EnsembleRetriever"},
    )


def _query_parent(
    session: WorkerSession,
    state: IndexState,
    question: str,
    params: dict[str, Any],
    run_config: RunnableConfig,
) -> dict[str, Any]:
    retriever = state.extra["retriever"]
    assert state.store is not None
    child_hits = state.store.similarity_search_with_score(question, k=max(params["k"] * 4, 16))
    with session.tracer.step(
        "expand", "child hits -> parent documents", "langchain.parent_document"
    ) as attrs:
        parents = retriever.invoke(question, config=run_config)[: params["k"]]
        attrs.update(
            {
                "child_hits": len(child_hits),
                "parents": len(parents),
                "_refs": [str(p.metadata.get("ref") or p.id) for p in parents],
            }
        )
    ranked = []
    for rank, parent in enumerate(parents, 1):
        ref = str(parent.metadata.get("ref") or parent.id)
        original = state.nodes.get(ref, parent)
        best = max((s for c, s in child_hits if c.metadata.get("doc_id") == ref), default=None)
        ranked.append(
            _item(
                state,
                original,
                rank,
                best,
                "max_child_cosine",
                {"children_hit": sum(1 for c, _ in child_hits if c.metadata.get("doc_id") == ref)},
            )
        )
    return _finish(
        session, question, ranked, ranked, ranked, params, run_config, {"returned": "parents"}
    )


def _query_auto_merge(
    session: WorkerSession,
    state: IndexState,
    question: str,
    params: dict[str, Any],
    run_config: RunnableConfig,
) -> dict[str, Any]:
    assert state.store is not None
    k = params["k"]
    scores = _scores(state, question, k)
    leaves = _with_scores(
        state.store.as_retriever(search_kwargs={"k": k}).invoke(question, config=run_config), scores
    )
    retrieved = _items(state, leaves, "similarity", "cosine")

    def merge(documents: list[Document]) -> list[Document]:
        current = list(documents)
        parents = state.extra["parents"]
        children_of = state.extra["children_of"]
        changed = True
        merges = 0
        while changed:
            changed = False
            by_parent: dict[str, list[Document]] = {}
            for d in current:
                parent = d.metadata.get("parent_ref")
                if parent:
                    by_parent.setdefault(parent, []).append(d)
            for parent, hits in by_parent.items():
                total = len(children_of.get(parent, []))
                if total and len(hits) / total > params["merge_ratio"] and parent in parents:
                    best = max((h.metadata.get("similarity") or 0.0) for h in hits)
                    replacement = parents[parent]
                    replacement = Document(
                        page_content=replacement.page_content,
                        metadata={
                            **replacement.metadata,
                            "similarity": best,
                            "merged_children": len(hits),
                        },
                        id=replacement.id,
                    )
                    first = min(current.index(h) for h in hits)
                    current = [d for d in current if d not in hits]
                    current.insert(min(first, len(current)), replacement)
                    merges += 1
                    changed = True
                    break
        state.extra["last_merges"] = merges
        return current

    with session.tracer.step(
        "merge", "LCEL auto-merge (RunnableLambda)", "langchain.runnable_lambda"
    ) as attrs:
        merged = RunnableLambda(merge).invoke(leaves, config=run_config)
        attrs.update({"merges": state.extra["last_merges"], "_refs": [str(d.id) for d in merged]})
    ranked = _items(state, merged, "similarity", "cosine")
    return _finish(
        session,
        question,
        retrieved,
        ranked,
        ranked,
        params,
        run_config,
        {"merges": state.extra["last_merges"]},
    )


def _query_window(
    session: WorkerSession,
    state: IndexState,
    question: str,
    params: dict[str, Any],
    run_config: RunnableConfig,
) -> dict[str, Any]:
    assert state.store is not None
    k = params["k"]
    scores = _scores(state, question, k)
    hits = _with_scores(
        state.store.as_retriever(search_kwargs={"k": k}).invoke(question, config=run_config), scores
    )
    retrieved = _items(state, hits, "similarity", "cosine")
    ordered = state.extra["ordered"]
    window = params["window_size"]
    expanded = []
    with session.tracer.step(
        "expand", "LCEL sentence window", "langchain.runnable_lambda"
    ) as attrs:
        for rank, hit in enumerate(hits, 1):
            artifact = str(hit.metadata["artifact_ref"])
            sequence = ordered[artifact]
            position = hit.metadata["position"]
            refs = sequence[max(0, position - window) : position + window + 1]
            members = [state.nodes[r] for r in refs]
            start = members[0].metadata["start_hint"]
            last = members[-1]
            end = last.metadata["start_hint"] + len(last.page_content)
            text = state.sources[artifact][start:end]
            composite = Document(
                page_content=text,
                metadata={**hit.metadata, "ref": f"{hit.id}:W{window}"},
                id=f"{hit.id}:W{window}",
            )
            expanded.append(
                _item(
                    state,
                    composite,
                    rank,
                    hit.metadata.get("similarity"),
                    "cosine",
                    {"window_members": len(members)},
                    span=(start, end),
                )
            )
        attrs["_refs"] = [e["ref"] for e in expanded]
    return _finish(
        session, question, retrieved, retrieved, expanded, params, run_config, {"window": window}
    )


def _query_native(
    session: WorkerSession,
    state: IndexState,
    question: str,
    params: dict[str, Any],
    run_config: RunnableConfig,
    filters: dict[str, Any],
) -> dict[str, Any]:
    retriever = NativeBridgeRetriever(session=session, mode=params["mode"], filters=filters)
    chain = RunnableParallel(question=RunnablePassthrough(), docs=retriever)
    if params.get("rerank"):
        compressor = CrossEncoderCompressor(session=session, top_n=32)
        chain = chain | RunnablePassthrough.assign(
            ranked=RunnableLambda(
                lambda x: list(compressor.compress_documents(x["docs"][:32], x["question"]))
            ).with_config(run_name="cross_encoder_rerank")
        )
    with session.tracer.step(
        "retrieve", "LCEL RunnableParallel(native retriever)", "langchain.lcel"
    ) as attrs:
        output = chain.invoke(question, config=run_config)
        attrs["graph_mermaid"] = chain.get_graph().draw_mermaid()[:6000]
    docs = output["docs"]
    retrieved = [
        remember(
            item(
                ref=d.metadata["chunk_id"],
                rank=r,
                text=d.page_content,
                metadata=d.metadata,
                source_text=None,
                start_hint=None,
                score=d.metadata.get("score"),
                score_kind=str(d.metadata.get("score_kind")),
            ),
            d.page_content,
        )
        for r, d in enumerate(docs, 1)
    ]
    ranked = retrieved
    if params.get("rerank"):
        ranked = [
            remember(
                item(
                    ref=d.metadata["chunk_id"],
                    rank=r,
                    text=d.page_content,
                    metadata=d.metadata,
                    source_text=None,
                    start_hint=None,
                    score=d.metadata.get("rerank_score"),
                    score_kind="rerank_logit",
                ),
                d.page_content,
            )
            for r, d in enumerate(output["ranked"], 1)
        ]
    return _finish(
        session, question, retrieved, ranked, ranked, params, run_config, {"mode": params["mode"]}
    )


def _finish(
    session: WorkerSession,
    question: str,
    retrieved: list[dict[str, Any]],
    ranked: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    params: dict[str, Any],
    run_config: RunnableConfig,
    info: dict[str, Any],
) -> dict[str, Any]:
    packed, packing = pack_context([LabItem.from_dict(c) for c in candidates])
    result: dict[str, Any] = {
        "retrieved": retrieved,
        "ranked": ranked,
        "candidates": candidates,
        "packed_refs": [p.ref for p in packed],
        "packing": packing,
        "info": info,
        "answer": None,
    }
    if params.get("synthesis", False) and packed:
        texts = {c["ref"]: c for c in candidates}
        result["answer"] = synthesize(session, question, [texts[p.ref] for p in packed], run_config)
    return result


def synthesize(
    session: WorkerSession, question: str, packed: list[dict[str, Any]], run_config: RunnableConfig
) -> dict[str, Any]:
    """Prompt -> offline model -> LangChain structured output -> BuildLens validation."""
    evidence = [
        (f"E{i}", TEXTS.get(p["text_sha256"], ""), {"locator": p["locator"]})
        for i, p in enumerate(packed, 1)
    ]
    prompt = ChatPromptTemplate.from_messages(
        [("system", SYSTEM_PROMPT), ("human", "Question: {question}\n\nEvidence:\n{evidence}")]
    )
    model = OfflineChatModel(question=question)
    chain = prompt | model.with_structured_output(CITED_ANSWER_SCHEMA, include_raw=True)
    with session.tracer.step("prompt", "ChatPromptTemplate", "langchain.prompt") as attrs:
        rendered = prompt.invoke({"question": question, "evidence": evidence_block(evidence)})
        attrs["prompt_chars"] = sum(len(str(m.content)) for m in rendered.to_messages())
    output = chain.invoke(
        {"question": question, "evidence": evidence_block(evidence)}, config=run_config
    )
    parsed = output.get("parsed")
    with session.tracer.step(
        "validate", "BuildLens citation validation", "buildlens.validate"
    ) as attrs:
        validation = validate_citations(parsed or {}, [e[0] for e in evidence])
        attrs.update(validation)
    return {
        "model": "buildlens-offline-extractive (mechanics only)",
        "framework_parse_ok": parsed is not None and output.get("parsing_error") is None,
        "parsed": parsed,
        "buildlens_validation": validation,
        "prompt_chars": attrs.get("prompt_chars"),
        "evidence_ids": [e[0] for e in evidence],
    }
