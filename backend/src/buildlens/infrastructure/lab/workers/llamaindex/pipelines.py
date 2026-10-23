"""LlamaIndex pipeline construction and querying inside the worker process."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any

from llama_index.core import (
    Document,
    PromptTemplate,
    StorageContext,
    VectorStoreIndex,
    get_response_synthesizer,
    load_index_from_storage,
)
from llama_index.core.ingestion import IngestionCache, IngestionPipeline
from llama_index.core.node_parser import (
    CodeSplitter,
    HierarchicalNodeParser,
    MarkdownNodeParser,
    SentenceSplitter,
    SentenceWindowNodeParser,
    TokenTextSplitter,
    get_leaf_nodes,
)
from llama_index.core.postprocessor import (
    LongContextReorder,
    MetadataReplacementPostProcessor,
    PrevNextNodePostprocessor,
    SimilarityPostprocessor,
)
from llama_index.core.query_engine import RetrieverQueryEngine
from llama_index.core.retrievers import (
    AutoMergingRetriever,
    QueryFusionRetriever,
    RecursiveRetriever,
)
from llama_index.core.schema import (
    BaseNode,
    IndexNode,
    MetadataMode,
    NodeRelationship,
    NodeWithScore,
    QueryBundle,
    RelatedNodeInfo,
    TextNode,
)
from llama_index.core.storage.docstore import SimpleDocumentStore
from llama_index.retrievers.bm25 import BM25Retriever

from buildlens.domain.lab import LabError, LabItem, pack_context
from buildlens.infrastructure.lab.workers.common.items import (
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
from buildlens.infrastructure.lab.workers.llamaindex.adapters import (
    BridgeRerank,
    BuildLensLlamaEmbedding,
    CitedAnswer,
    NativeBridgeRetriever,
    OfflineLLM,
)

TEXTS: dict[str, str] = {}
_CODE_LANGUAGES = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".go": "go",
    ".rs": "rust",
}
STRUCTURED_PROMPT = PromptTemplate(
    "Answer only from the numbered evidence and cite identifiers like [E1].\n"
    "Return a CitedAnswer as JSON.\nQuestion: {question}\n\nEvidence:\n{evidence}\n"
)


@dataclass(slots=True)
class IndexState:
    index_id: str
    config: dict[str, Any]
    documents: DocumentSet
    ledger: EmbeddingLedger
    native: bool
    directory: str
    embed_model: BuildLensLlamaEmbedding
    index: VectorStoreIndex | None = None
    nodes: dict[str, BaseNode] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)


def _exclude_all(metadata: dict[str, Any]) -> list[str]:
    return sorted(metadata)


def _text_nodes(state: IndexState, metadata_in_embedding: bool) -> list[TextNode]:
    """BuildLens chunks as TextNodes with document-order PREVIOUS/NEXT relationships."""
    nodes: list[TextNode] = []
    by_locator: dict[str, list[TextNode]] = {}
    for raw in state.documents.documents:
        meta = dict(raw["metadata"])
        node = TextNode(
            id_=raw["ref"],
            text=raw["text"],
            metadata=meta,
            excluded_embed_metadata_keys=[] if metadata_in_embedding else _exclude_all(meta),
            excluded_llm_metadata_keys=_exclude_all(meta),
        )
        node.relationships[NodeRelationship.SOURCE] = RelatedNodeInfo(
            node_id=str(meta.get("locator"))
        )
        nodes.append(node)
        by_locator.setdefault(str(meta.get("locator")), []).append(node)
    for siblings in by_locator.values():
        siblings.sort(key=lambda n: (n.metadata.get("ordinal", 0), n.node_id))
        for left, right in zip(siblings, siblings[1:], strict=False):
            left.relationships[NodeRelationship.NEXT] = RelatedNodeInfo(node_id=right.node_id)
            right.relationships[NodeRelationship.PREVIOUS] = RelatedNodeInfo(node_id=left.node_id)
    return nodes


def _documents(state: IndexState, metadata_in_embedding: bool = False) -> list[Document]:
    result = []
    for raw in state.documents.documents:
        meta = dict(raw["metadata"])
        result.append(
            Document(
                id_=raw["ref"],
                text=raw["text"],
                metadata=meta,
                excluded_embed_metadata_keys=[] if metadata_in_embedding else _exclude_all(meta),
                excluded_llm_metadata_keys=_exclude_all(meta),
            )
        )
    return result


def _note_transformations(state: IndexState, nodes: list[BaseNode]) -> None:
    for node in nodes:
        raw = node.get_content(metadata_mode=MetadataMode.NONE)
        embedded = node.get_content(metadata_mode=MetadataMode.EMBED)
        kind = (
            "identity"
            if raw == embedded
            else "metadata_prefixed"
            if embedded.endswith(raw)
            else "rewritten"
        )
        state.ledger.note(node.node_id, raw, embedded, kind)


def _vector_index(
    state: IndexState, nodes: list[BaseNode], storage: StorageContext | None = None
) -> VectorStoreIndex:
    _note_transformations(state, nodes)
    return VectorStoreIndex(
        nodes,
        embed_model=state.embed_model,
        storage_context=storage or StorageContext.from_defaults(),
        show_progress=False,
    )


def _persist(state: IndexState, index: VectorStoreIndex, name: str) -> None:
    directory = os.path.join(state.directory, name)
    index.storage_context.persist(persist_dir=directory)
    vector_store = index.storage_context.vector_store
    embeddings = getattr(getattr(vector_store, "data", None), "embedding_dict", {}) or {}
    state.manifest["vector_digests"] = {k: float32_digest(v) for k, v in embeddings.items()}
    state.manifest["persisted"] = sorted(os.listdir(directory))


def _parser(params: dict[str, Any], suffix: str) -> Any:
    size, overlap = params["chunk_size"], params["chunk_overlap"]
    kind = params["parser"]
    if kind == "markdown" and suffix == ".md":
        return MarkdownNodeParser()
    if kind == "token":
        return TokenTextSplitter(chunk_size=size, chunk_overlap=overlap)
    if kind == "code" and suffix in _CODE_LANGUAGES:
        return CodeSplitter(language=_CODE_LANGUAGES[suffix], max_chars=size * 4)
    return SentenceSplitter(chunk_size=size, chunk_overlap=overlap)


# ---------------------------------------------------------------------------- build


def build(session: WorkerSession, state: IndexState) -> dict[str, Any]:
    pipeline = state.config["pipeline"]
    params = state.config["params"]
    if not state.native:
        state.sources = {d["ref"]: d["text"] for d in state.documents.documents}
    if pipeline in {"llamaindex.native_retriever", "llamaindex.workflow"}:
        state.manifest["nodes"] = 0
        return state.manifest
    if pipeline in {"llamaindex.vector", "llamaindex.fusion"}:
        with session.tracer.step(
            "split", "TextNodes from BuildLens chunks", "llama_index.schema"
        ) as attrs:
            if state.native:
                nodes: list[BaseNode] = list(
                    _text_nodes(state, params.get("metadata_in_embedding", False))
                )
            else:
                nodes = SentenceSplitter(chunk_size=256, chunk_overlap=0).get_nodes_from_documents(
                    _documents(state)
                )
            attrs["nodes"] = len(nodes)
        with session.tracer.step("embed", "VectorStoreIndex(nodes)", "llama_index.index") as attrs:
            state.index = _vector_index(state, nodes)
            attrs["_refs"] = []
        state.nodes = {n.node_id: n for n in nodes}
        state.extra["bm25_nodes"] = nodes
        _persist(state, state.index, "storage")
    elif pipeline == "llamaindex.pgvector":
        _build_pgvector(session, state)
    elif pipeline == "llamaindex.ingest":
        _build_ingest(session, state, params)
    elif pipeline == "llamaindex.parent_child":
        _build_parent_child(session, state, params)
    elif pipeline == "llamaindex.auto_merging":
        _build_auto_merging(session, state, params)
    elif pipeline == "llamaindex.sentence_window":
        _build_window(session, state, params)
    else:
        raise LabError("WORKER_PIPELINE_UNSUPPORTED", pipeline)
    state.manifest["nodes"] = len(state.nodes)
    state.manifest["anchor_methods"] = _census(state)
    return state.manifest


def _build_pgvector(session: WorkerSession, state: IndexState) -> None:
    from llama_index.vector_stores.postgres import PGVectorStore
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    url = make_url(os.environ.get("BUILDLENS_LAB_PG_URL") or "")
    if not url.host:
        raise LabError("LAB_DATABASE_NOT_CONFIGURED")
    table = f"li_{state.index_id[:16]}"
    store = PGVectorStore.from_params(
        connection_string=url.set(drivername="postgresql+psycopg2").render_as_string(
            hide_password=False
        ),
        async_connection_string=url.set(drivername="postgresql+asyncpg").render_as_string(
            hide_password=False
        ),
        table_name=table,
        schema_name="lab",
        embed_dim=len(next(iter(state.documents.reuse.values()), [0.0] * 384)),
        perform_setup=True,
    )
    nodes = _text_nodes(state, False)
    with session.tracer.step(
        "store", "PGVectorStore + VectorStoreIndex", "llama_index.vector_stores.postgres"
    ) as attrs:
        engine = create_engine(url)
        with engine.begin() as connection:
            connection.execute(text(f'DROP TABLE IF EXISTS lab."data_{table}"'))
        state.index = _vector_index(
            state, list(nodes), StorageContext.from_defaults(vector_store=store)
        )
        with engine.connect() as connection:
            rows = connection.execute(
                text(f'SELECT node_id, embedding::text FROM lab."data_{table}"')
            )
            state.manifest["vector_digests"] = {
                r[0]: float32_digest([float(v) for v in r[1].strip("[]").split(",")]) for r in rows
            }
        engine.dispose()
        attrs["rows"] = len(state.manifest["vector_digests"])
    state.nodes = {n.node_id: n for n in nodes}
    state.manifest["table"] = f'lab."data_{table}"'


def _build_ingest(session: WorkerSession, state: IndexState, params: dict[str, Any]) -> None:
    documents = _documents(state, params["metadata_in_embedding"])
    docstore = SimpleDocumentStore()
    cache = IngestionCache()
    all_nodes: list[BaseNode] = []
    groups: dict[str, list[Document]] = {}
    for document in documents:
        groups.setdefault(str(document.metadata.get("suffix") or ""), []).append(document)
    with session.tracer.step("split", "IngestionPipeline.run", "llama_index.ingestion") as attrs:
        for suffix, group in sorted(groups.items()):
            parser = _parser(params, suffix)
            pipeline = IngestionPipeline(transformations=[parser], docstore=docstore, cache=cache)
            nodes = pipeline.run(documents=group)
            all_nodes.extend(nodes)
            attrs[f"parser{suffix or '<none>'}"] = type(parser).__name__
        attrs["nodes"] = len(all_nodes)
    with session.tracer.step(
        "other", "IngestionPipeline re-run (docstore dedupe)", "llama_index.ingestion"
    ) as attrs:
        rerun = 0
        for suffix, group in sorted(groups.items()):
            pipeline = IngestionPipeline(
                transformations=[_parser(params, suffix)], docstore=docstore, cache=cache
            )
            rerun += len(pipeline.run(documents=group))
        attrs["second_run_new_nodes"] = rerun
        state.manifest["ingestion_second_run_new_nodes"] = rerun
    with session.tracer.step("embed", "VectorStoreIndex(nodes)", "llama_index.index"):
        state.index = _vector_index(state, all_nodes)
    state.nodes = {n.node_id: n for n in all_nodes}
    _persist(state, state.index, "storage")


def _build_parent_child(session: WorkerSession, state: IndexState, params: dict[str, Any]) -> None:
    documents = _documents(state)
    parents = SentenceSplitter(
        chunk_size=params["parent_chunk_size"], chunk_overlap=0
    ).get_nodes_from_documents(documents)
    children: list[BaseNode] = []
    child_splitter = SentenceSplitter(chunk_size=params["child_chunk_size"], chunk_overlap=0)
    for parent in parents:
        parent.excluded_embed_metadata_keys = _exclude_all(parent.metadata)
        meta = {**parent.metadata, "parent_start": parent.start_char_idx}
        # LlamaIndex subtracts the metadata string from chunk_size; exclude it from both views.
        pieces = child_splitter.get_nodes_from_documents(
            [
                Document(
                    text=parent.get_content(),
                    metadata=meta,
                    excluded_embed_metadata_keys=_exclude_all(meta),
                    excluded_llm_metadata_keys=_exclude_all(meta),
                )
            ]
        )
        for piece in pieces:
            child = IndexNode.from_text_node(piece, parent.node_id)
            child.excluded_embed_metadata_keys = _exclude_all(child.metadata)
            child.excluded_llm_metadata_keys = _exclude_all(child.metadata)
            children.append(child)
    with session.tracer.step(
        "embed", "VectorStoreIndex(IndexNode children)", "llama_index.index"
    ) as attrs:
        state.index = _vector_index(state, children)
        attrs.update({"parents": len(parents), "children": len(children)})
    state.nodes = {p.node_id: p for p in parents}
    state.extra["children"] = len(children)
    state.manifest["children"] = len(children)
    _persist(state, state.index, "storage")


def _build_auto_merging(session: WorkerSession, state: IndexState, params: dict[str, Any]) -> None:
    documents = _documents(state)
    parser = HierarchicalNodeParser.from_defaults(
        chunk_sizes=list(params["chunk_sizes"]), chunk_overlap=0
    )
    with session.tracer.step("split", "HierarchicalNodeParser", "llama_index.node_parser") as attrs:
        nodes = parser.get_nodes_from_documents(documents)
        leaves = get_leaf_nodes(nodes)
        attrs.update({"nodes": len(nodes), "leaves": len(leaves)})
    for node in nodes:
        node.excluded_embed_metadata_keys = _exclude_all(node.metadata)
    docstore = SimpleDocumentStore()
    docstore.add_documents(nodes)
    storage = StorageContext.from_defaults(docstore=docstore)
    with session.tracer.step("embed", "VectorStoreIndex(leaf nodes)", "llama_index.index"):
        state.index = _vector_index(state, leaves, storage)
    state.nodes = {n.node_id: n for n in nodes}
    state.extra["storage"] = storage
    state.manifest["leaves"] = len(leaves)
    _persist(state, state.index, "storage")


def _build_window(session: WorkerSession, state: IndexState, params: dict[str, Any]) -> None:
    parser = SentenceWindowNodeParser.from_defaults(window_size=params["window_size"])
    with session.tracer.step(
        "split", "SentenceWindowNodeParser", "llama_index.node_parser"
    ) as attrs:
        parsed = parser.get_nodes_from_documents(_documents(state))
        limit = params["max_sentence_chars"]
        nodes = [n for n in parsed if len(n.get_content(metadata_mode=MetadataMode.NONE)) <= limit]
        attrs.update({"sentences": len(parsed), "dropped_oversized": len(parsed) - len(nodes)})
        state.manifest["dropped_oversized_sentences"] = len(parsed) - len(nodes)
    for node in nodes:
        # Embed the sentence itself only; the window is delivered later by the postprocessor.
        node.excluded_embed_metadata_keys = sorted(node.metadata)
        node.excluded_llm_metadata_keys = sorted(k for k in node.metadata if k != "window")
    with session.tracer.step("embed", "VectorStoreIndex(sentences)", "llama_index.index"):
        state.index = _vector_index(state, nodes)
    state.nodes = {n.node_id: n for n in nodes}
    _persist(state, state.index, "storage")


# ---------------------------------------------------------------------------- items


def _absolute_start(state: IndexState, node: BaseNode) -> int | None:
    """Follow PARENT relationships: LlamaIndex offsets are relative to the split parent."""
    if node.start_char_idx is None:
        return None
    start = int(node.start_char_idx)
    current = node
    for _ in range(8):
        parent_info = current.relationships.get(NodeRelationship.PARENT)
        if parent_info is None:
            return start
        parent = state.nodes.get(parent_info.node_id)
        if parent is None or parent.start_char_idx is None:
            return start
        start += int(parent.start_char_idx)
        current = parent
    return start


def _window_span(state: IndexState, node: BaseNode, size: int) -> tuple[int, int] | None:
    """Span of the sentence window, from the neighbouring sentence nodes' own offsets.

    LlamaIndex joins window sentences with spaces, so the window text is not an exact source
    slice; the span is a verified union of anchored sentence nodes instead (composite).
    """
    original = state.nodes.get(node.node_id)
    if original is None or original.start_char_idx is None:
        return None
    first = last = original
    for _ in range(size):
        info = first.relationships.get(NodeRelationship.PREVIOUS)
        if info is None or info.node_id not in state.nodes:
            break
        first = state.nodes[info.node_id]
    for _ in range(size):
        info = last.relationships.get(NodeRelationship.NEXT)
        if info is None or info.node_id not in state.nodes:
            break
        last = state.nodes[info.node_id]
    if first.start_char_idx is None or last.end_char_idx is None:
        return None
    return first.start_char_idx, last.end_char_idx


def _source_for(state: IndexState, node: BaseNode) -> str | None:
    if state.native:
        return None
    ref = node.metadata.get("artifact_id") or node.ref_doc_id
    return state.sources.get(str(ref)) if ref is not None else None


def _item(
    state: IndexState,
    node: BaseNode,
    rank: int,
    score: float | None,
    kind: str,
    detail: dict[str, Any] | None = None,
    text: str | None = None,
    span: tuple[int, int] | None = None,
) -> dict[str, Any]:
    content = text if text is not None else node.get_content(metadata_mode=MetadataMode.NONE)
    source = _source_for(state, node)
    hint = node.metadata.get("parent_start") if isinstance(node, IndexNode) else None
    if hint is None:
        hint = _absolute_start(state, node)
    result = item(
        ref=node.node_id,
        rank=rank,
        text=content,
        metadata={**node.metadata, "chunk_id": node.metadata.get("chunk_id")}
        if state.native
        else node.metadata,
        source_text=source,
        start_hint=hint,
        score=score,
        score_kind=kind,
        detail=detail,
        span_override=span,
    )
    TEXTS[result["text_sha256"]] = content
    return result


def _census(state: IndexState) -> dict[str, int]:
    census: dict[str, int] = {}
    spans: dict[str, list[list[Any]]] = {}
    for node in list(state.nodes.values())[:20000]:
        lab = _item(state, node, 0, None, "none")
        method = lab["detail"]["anchor_method"]
        census[method] = census.get(method, 0) + 1
        span = lab.get("span") or {}
        if lab.get("locator"):
            spans.setdefault(str(lab["locator"]), []).append(
                [span.get("start_offset"), span.get("end_offset"), method, lab["ref"]]
            )
    state.manifest["node_spans"] = spans
    TEXTS.clear()
    return census


def _items(state: IndexState, nodes: list[NodeWithScore], kind: str) -> list[dict[str, Any]]:
    return [_item(state, n.node, rank, n.score, kind) for rank, n in enumerate(nodes, 1)]


# ---------------------------------------------------------------------------- query


def query(
    session: WorkerSession, state: IndexState, question: str, config: dict[str, Any]
) -> dict[str, Any]:
    pipeline = config["pipeline"]
    params = config["params"]
    filters = config.get("corpus", {}).get("filters") or {}
    llm = OfflineLLM()
    bundle = QueryBundle(question)
    if pipeline == "llamaindex.native_retriever":
        return _query_native(session, state, question, params, filters, llm)
    if pipeline == "llamaindex.workflow":
        from buildlens.infrastructure.lab.workers.llamaindex.workflow import run_workflow

        return run_workflow(session, question, params, filters)
    if state.index is None:
        raise LabError("LAB_INDEX_NOT_LOADED")
    k = params["k"]
    if pipeline == "llamaindex.pgvector":
        nodes = state.index.as_retriever(similarity_top_k=k).retrieve(bundle)
        ranked = _items(state, nodes, "cosine")
        return _finish(
            session,
            state,
            question,
            ranked,
            ranked,
            ranked,
            nodes,
            params,
            llm,
            {"store": "lab pgvector (llama-index-vector-stores-postgres)"},
        )
    if pipeline == "llamaindex.vector":
        retriever: Any = (
            BM25Retriever.from_defaults(nodes=state.extra["bm25_nodes"], similarity_top_k=k)
            if params["retriever"] == "bm25"
            else state.index.as_retriever(similarity_top_k=k)
        )
        retrieved_nodes = retriever.retrieve(bundle)
        retrieved = _items(
            state, retrieved_nodes, "bm25" if params["retriever"] == "bm25" else "cosine"
        )
        processed = list(retrieved_nodes)
        decisions: dict[str, Any] = {}
        if params["similarity_cutoff"] > -1:
            before = len(processed)
            processed = SimilarityPostprocessor(
                similarity_cutoff=params["similarity_cutoff"]
            ).postprocess_nodes(processed, bundle)
            decisions["similarity_cutoff_dropped"] = before - len(processed)
        if params["prev_next"] > 0:
            present = {n.node.node_id for n in processed}
            processed = PrevNextNodePostprocessor(
                docstore=state.index.docstore, num_nodes=params["prev_next"], mode="both"
            ).postprocess_nodes(processed, bundle)
            decisions["prev_next_added"] = len({n.node.node_id for n in processed} - present)
        if params["long_context_reorder"]:
            processed = LongContextReorder().postprocess_nodes(processed, bundle)
            decisions["long_context_reordered"] = True
        with session.tracer.step(
            "postprocess", "node postprocessors", "llama_index.postprocessor"
        ) as attrs:
            attrs.update(decisions)
            attrs["_refs"] = [n.node.node_id for n in processed]
        candidates = _items(state, processed, "postprocessed")
        return _finish(
            session,
            state,
            question,
            retrieved,
            retrieved,
            candidates,
            processed,
            params,
            llm,
            decisions,
        )
    if pipeline == "llamaindex.fusion":
        vector = state.index.as_retriever(similarity_top_k=k)
        bm25 = BM25Retriever.from_defaults(nodes=state.extra["bm25_nodes"], similarity_top_k=k)
        fusion = QueryFusionRetriever(
            [vector, bm25],
            llm=llm,
            mode=params["fusion_mode"],
            similarity_top_k=k,
            num_queries=1,
            use_async=False,
        )
        with session.tracer.step(
            "fuse", f"QueryFusionRetriever({params['fusion_mode']})", "llama_index.fusion"
        ) as attrs:
            fused = fusion.retrieve(bundle)
            vector_rank = {n.node.node_id: r for r, n in enumerate(vector.retrieve(bundle), 1)}
            bm25_rank = {n.node.node_id: r for r, n in enumerate(bm25.retrieve(bundle), 1)}
            attrs.update(
                {
                    "fused": len(fused),
                    "llm_calls": llm.calls,
                    "_refs": [n.node.node_id for n in fused],
                }
            )
        ranked = [
            _item(
                state,
                n.node,
                rank,
                n.score,
                f"fusion:{params['fusion_mode']}",
                {
                    "vector_rank": vector_rank.get(n.node.node_id),
                    "bm25_rank": bm25_rank.get(n.node.node_id),
                },
            )
            for rank, n in enumerate(fused, 1)
        ]
        return _finish(
            session,
            state,
            question,
            ranked,
            ranked,
            ranked,
            fused,
            params,
            llm,
            {"fusion_mode": params["fusion_mode"]},
        )
    if pipeline == "llamaindex.ingest":
        retriever = state.index.as_retriever(similarity_top_k=k)
        nodes = retriever.retrieve(bundle)
        retrieved = _items(state, nodes, "cosine")
        ranked_nodes = (
            BridgeRerank(session, top_n=k).postprocess_nodes(nodes, bundle)
            if params["rerank"]
            else nodes
        )
        ranked = _items(state, ranked_nodes, "rerank_logit" if params["rerank"] else "cosine")
        return _finish(
            session,
            state,
            question,
            retrieved,
            ranked,
            ranked,
            ranked_nodes,
            params,
            llm,
            {"rerank": params["rerank"]},
        )
    if pipeline == "llamaindex.parent_child":
        children = state.index.as_retriever(similarity_top_k=params["k"] * 4)
        recursive = RecursiveRetriever(
            "vector", retriever_dict={"vector": children}, node_dict=dict(state.nodes)
        )
        with session.tracer.step(
            "expand", "RecursiveRetriever (IndexNode -> parent)", "llama_index.recursive"
        ) as attrs:
            parents = recursive.retrieve(bundle)
            unique: list[NodeWithScore] = []
            seen: set[str] = set()
            for p in parents:
                if p.node.node_id not in seen:
                    seen.add(p.node.node_id)
                    unique.append(p)
            unique = unique[: params["k"]]
            attrs.update(
                {
                    "returned": len(parents),
                    "unique_parents": len(unique),
                    "_refs": [p.node.node_id for p in unique],
                }
            )
        ranked = _items(state, unique, "max_child_cosine")
        return _finish(
            session,
            state,
            question,
            ranked,
            ranked,
            ranked,
            unique,
            params,
            llm,
            {"returned": "parents"},
        )
    if pipeline == "llamaindex.auto_merging":
        base = state.index.as_retriever(similarity_top_k=k)
        leaves = base.retrieve(bundle)
        retrieved = _items(state, leaves, "cosine")
        merger = AutoMergingRetriever(
            base, state.extra["storage"], simple_ratio_thresh=params["simple_ratio_thresh"]
        )
        with session.tracer.step(
            "merge", "AutoMergingRetriever", "llama_index.auto_merging"
        ) as attrs:
            merged = merger.retrieve(bundle)
            attrs.update(
                {
                    "leaves": len(leaves),
                    "after_merge": len(merged),
                    "_refs": [n.node.node_id for n in merged],
                }
            )
        ranked = _items(state, merged, "cosine")
        return _finish(
            session,
            state,
            question,
            retrieved,
            ranked,
            ranked,
            merged,
            params,
            llm,
            {"merged_from": len(leaves), "merged_to": len(merged)},
        )
    if pipeline == "llamaindex.sentence_window":
        sentences = state.index.as_retriever(similarity_top_k=k).retrieve(bundle)
        retrieved = _items(state, sentences, "cosine")
        replaced = MetadataReplacementPostProcessor(target_metadata_key="window").postprocess_nodes(
            [NodeWithScore(node=n.node.model_copy(), score=n.score) for n in sentences], bundle
        )
        candidates = []
        for rank, n in enumerate(replaced, 1):
            window = n.node.get_content(metadata_mode=MetadataMode.NONE)
            span = _window_span(state, n.node, params["window_size"])
            candidates.append(
                _item(
                    state,
                    n.node,
                    rank,
                    n.score,
                    "cosine",
                    {"window_chars": len(window)},
                    text=window,
                    span=span,
                )
            )
        with session.tracer.step(
            "postprocess", "MetadataReplacementPostProcessor(window)", "llama_index.postprocessor"
        ) as attrs:
            attrs["_refs"] = [c["ref"] for c in candidates]
        return _finish(
            session,
            state,
            question,
            retrieved,
            retrieved,
            candidates,
            replaced,
            params,
            llm,
            {"window_size": params["window_size"]},
        )
    raise LabError("WORKER_PIPELINE_UNSUPPORTED", pipeline)


def _query_native(
    session: WorkerSession,
    state: IndexState,
    question: str,
    params: dict[str, Any],
    filters: dict[str, Any],
    llm: OfflineLLM,
) -> dict[str, Any]:
    retriever = NativeBridgeRetriever(session, params["mode"], filters)
    postprocessors = [BridgeRerank(session, top_n=32, native=True)] if params["rerank"] else []
    engine = RetrieverQueryEngine.from_args(
        retriever,
        llm=llm,
        node_postprocessors=postprocessors,
        response_mode=params["response_mode"],
    )
    nodes = retriever.retrieve(QueryBundle(question))
    retrieved = _items(state, nodes, "native")
    with session.tracer.step(
        "generate", f"RetrieverQueryEngine({params['response_mode']})", "llama_index.query_engine"
    ) as attrs:
        response = engine.query(question)
        attrs.update({"llm_calls": llm.calls, "prompt_chars": llm.prompt_chars})
    source_nodes = list(response.source_nodes)
    ranked = _items(state, source_nodes, "rerank_logit" if params["rerank"] else "native")
    result = _finish(
        session,
        state,
        question,
        retrieved,
        ranked,
        ranked,
        source_nodes,
        {**params, "synthesis": False},
        llm,
        {"mode": params["mode"]},
    )
    result["answer"] = {
        "model": "buildlens-offline-extractive (mechanics only)",
        "response_mode": params["response_mode"],
        "llm_calls": llm.calls,
        "prompt_chars": llm.prompt_chars,
        "response_text": str(response)[:500],
    }
    return result


def _finish(
    session: WorkerSession,
    state: IndexState,
    question: str,
    retrieved: list[dict[str, Any]],
    ranked: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    candidate_nodes: list[NodeWithScore],
    params: dict[str, Any],
    llm: OfflineLLM,
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
    mode = params.get("response_mode")
    if mode and mode != "no_text" and packed:
        by_ref = {c["ref"]: c for c in candidates}
        evidence = [
            (f"E{i}", TEXTS.get(by_ref[p.ref]["text_sha256"], ""), {"locator": p.locator})
            for i, p in enumerate(packed, 1)
        ]
        synthesizer = get_response_synthesizer(llm=llm, response_mode=mode)
        packed_nodes = [n for n in candidate_nodes if n.node.node_id in {p.ref for p in packed}]
        with session.tracer.step(
            "generate", f"ResponseSynthesizer({mode})", "llama_index.synthesizer"
        ) as attrs:
            response = synthesizer.synthesize(question, nodes=packed_nodes)
            attrs.update({"llm_calls": llm.calls, "prompt_chars": llm.prompt_chars})
        with session.tracer.step(
            "parse", "LLM.structured_predict(CitedAnswer)", "llama_index.structured"
        ) as attrs:
            calls_before = llm.calls
            parsed = llm.structured_predict(
                CitedAnswer, STRUCTURED_PROMPT, question=question, evidence=evidence_block(evidence)
            )
            attrs["llm_calls"] = llm.calls - calls_before
        answer = parsed.model_dump()
        result["answer"] = {
            "model": "buildlens-offline-extractive (mechanics only)",
            "response_mode": mode,
            "synthesis_llm_calls": calls_before,
            "prompt_chars": llm.prompt_chars,
            "response_text": str(response)[:500],
            "parsed": answer,
            "framework_parse_ok": True,
            "buildlens_validation": validate_citations(answer, [e[0] for e in evidence]),
            "evidence_ids": [e[0] for e in evidence],
        }
    return result


def reload_index(state: IndexState) -> VectorStoreIndex:
    """Demonstrates StorageContext persistence: reload the persisted index from disk."""
    storage = StorageContext.from_defaults(persist_dir=os.path.join(state.directory, "storage"))
    index = load_index_from_storage(storage, embed_model=state.embed_model)
    assert isinstance(index, VectorStoreIndex)
    return index


def run_async(coroutine: Any) -> Any:
    return asyncio.run(coroutine)
