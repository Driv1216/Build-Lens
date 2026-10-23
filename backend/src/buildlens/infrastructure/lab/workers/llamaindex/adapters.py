"""LlamaIndex extension points implemented over the BuildLens capability bridge."""

from __future__ import annotations

import json
import time
from typing import Any

from llama_index.core.base.llms.types import CompletionResponse, CompletionResponseGen, LLMMetadata
from llama_index.core.bridge.pydantic import PrivateAttr
from llama_index.core.embeddings import BaseEmbedding
from llama_index.core.instrumentation.event_handlers import BaseEventHandler
from llama_index.core.instrumentation.events import BaseEvent
from llama_index.core.llms import CustomLLM
from llama_index.core.postprocessor.types import BaseNodePostprocessor
from llama_index.core.retrievers import BaseRetriever
from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode
from pydantic import BaseModel

from buildlens.domain.lab import LabError
from buildlens.infrastructure.lab.workers.common.items import offline_answer
from buildlens.infrastructure.lab.workers.common.runtime import EmbeddingLedger, WorkerSession


class Statement(BaseModel):
    text: str
    support: str
    evidence_ids: list[str]


class CitedAnswer(BaseModel):
    """A grounded answer whose statements cite evidence identifiers."""

    status: str
    statements: list[Statement]


class BuildLensLlamaEmbedding(BaseEmbedding):
    """Every text LlamaIndex embeds (node.get_content(EMBED)) is resolved by the ledger."""

    _ledger: EmbeddingLedger = PrivateAttr()
    _queries: dict[str, list[float]] = PrivateAttr(default_factory=dict)

    def __init__(self, ledger: EmbeddingLedger, **kwargs: Any) -> None:
        super().__init__(model_name="buildlens-bridge", embed_batch_size=64, **kwargs)
        self._ledger = ledger
        self._queries = {}

    @classmethod
    def class_name(cls) -> str:
        return "BuildLensLlamaEmbedding"

    def _get_query_embedding(self, query: str) -> list[float]:
        if query not in self._queries:
            self._queries[query] = self._ledger.query(query)
        return self._queries[query]

    def _get_text_embedding(self, text: str) -> list[float]:
        return self._ledger.documents([text])[0]

    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        return self._ledger.documents(list(texts))

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._get_query_embedding(query)

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return self._get_text_embedding(text)


class ForbiddenEmbedding(BaseEmbedding):
    """Installed as Settings.embed_model so a silent global default can never be used."""

    @classmethod
    def class_name(cls) -> str:
        return "ForbiddenEmbedding"

    def _get_query_embedding(self, query: str) -> list[float]:
        raise LabError("LLAMAINDEX_GLOBAL_EMBED_MODEL_USED")

    def _get_text_embedding(self, text: str) -> list[float]:
        raise LabError("LLAMAINDEX_GLOBAL_EMBED_MODEL_USED")

    async def _aget_query_embedding(self, query: str) -> list[float]:
        raise LabError("LLAMAINDEX_GLOBAL_EMBED_MODEL_USED")


class OfflineLLM(CustomLLM):
    """Deterministic offline LLM: JSON for structured prompts, short text otherwise.
    Mechanics only (call counts, prompt sizes, parsing); never quality evidence."""

    calls: int = 0
    prompt_chars: int = 0
    forbidden: bool = False

    @property
    def metadata(self) -> LLMMetadata:
        return LLMMetadata(
            context_window=32_768, num_output=2_048, model_name="buildlens-offline-extractive"
        )

    def complete(self, prompt: str, formatted: bool = False, **kwargs: Any) -> CompletionResponse:
        if self.forbidden:
            raise LabError("LLAMAINDEX_GLOBAL_LLM_USED")
        self.calls += 1
        self.prompt_chars += len(prompt)
        if "CitedAnswer" in prompt or '"evidence_ids"' in prompt or "json" in prompt.lower():
            return CompletionResponse(text=json.dumps(offline_answer(prompt)))
        passages = prompt.count("\n\n")
        return CompletionResponse(
            text=f"Offline extractive response over {passages} prompt blocks."
        )

    def stream_complete(
        self, prompt: str, formatted: bool = False, **kwargs: Any
    ) -> CompletionResponseGen:
        response = self.complete(prompt, formatted, **kwargs)

        def generator() -> Any:
            yield response

        return generator()


class NativeBridgeRetriever(BaseRetriever):
    """LI-A: BuildLens Stage 8 ranking as a LlamaIndex retriever."""

    def __init__(self, session: WorkerSession, mode: str, filters: dict[str, Any]) -> None:
        super().__init__()
        self._session = session
        self._mode = mode
        self._filters = filters

    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        result = self._session.call(
            "native_candidates",
            {"query": query_bundle.query_str, "mode": self._mode, "filters": self._filters},
        )
        nodes = []
        for hit in result["hits"]:
            metadata = {k: v for k, v in hit.items() if k != "content"}
            node = TextNode(id_=hit["chunk_id"], text=hit["content"], metadata=metadata)
            nodes.append(NodeWithScore(node=node, score=hit["score"]))
        return nodes


class BridgeRerank(BaseNodePostprocessor):
    """Cross-encoder reranking through the bridge; native chunks use their real ids."""

    top_n: int = 32
    native: bool = False
    _session: Any = PrivateAttr()

    def __init__(self, session: WorkerSession, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._session = session

    @classmethod
    def class_name(cls) -> str:
        return "BuildLensBridgeRerank"

    def _postprocess_nodes(
        self, nodes: list[NodeWithScore], query_bundle: QueryBundle | None = None
    ) -> list[NodeWithScore]:
        if query_bundle is None or not nodes:
            return nodes
        admitted = nodes[: self.top_n]
        if self.native:
            result = self._session.call(
                "rerank_chunks",
                {"query": query_bundle.query_str, "chunk_ids": [n.node.node_id for n in admitted]},
            )
            scores = {row["chunk_id"]: row["score"] for row in result["scores"]}
        else:
            scores = {}
            for start in range(0, len(admitted), 32):
                batch = admitted[start : start + 32]
                result = self._session.call(
                    "rerank",
                    {
                        "query": query_bundle.query_str,
                        "items": [
                            {"ref": n.node.node_id, "text": n.node.get_content()} for n in batch
                        ],
                    },
                )
                scores.update({row["ref"]: row["score"] for row in result["scores"]})
        ranked = sorted(
            admitted,
            key=lambda n: (
                scores.get(n.node.node_id) is None,
                -(scores.get(n.node.node_id) or 0.0),
            ),
        )
        return [NodeWithScore(node=n.node, score=scores.get(n.node.node_id)) for n in ranked]


class TraceEvents(BaseEventHandler):
    """Root-dispatcher instrumentation handler. Global by design in LlamaIndex; safe here
    because the worker process serves exactly one request at a time."""

    _session: Any = PrivateAttr(default=None)
    _open: dict[str, tuple[float, str]] = PrivateAttr(default_factory=dict)

    @classmethod
    def class_name(cls) -> str:
        return "BuildLensTraceEvents"

    def bind(self, session: WorkerSession) -> None:
        self._session = session

    def handle(self, event: BaseEvent, **kwargs: Any) -> None:
        if self._session is None:
            return
        name = type(event).__name__
        key = str(getattr(event, "span_id", "") or "")
        base = name.replace("StartEvent", "").replace("EndEvent", "")
        if name.endswith("StartEvent"):
            self._open[key + base] = (time.perf_counter(), base)
            return
        started = self._open.pop(key + base, None)
        kind = _KINDS.get(base, "other")
        attributes: dict[str, Any] = {}
        nodes = getattr(event, "nodes", None)
        if isinstance(nodes, list):
            attributes["nodes"] = len(nodes)
        chunks = getattr(event, "chunks", None)
        if isinstance(chunks, list):
            attributes["texts"] = len(chunks)
        duration = (time.perf_counter() - started[0]) * 1000 if started else 0.0
        if name.endswith("EndEvent") or started is None:
            self._session.tracer.add(kind, base, f"llama_index.{name}", duration, attributes)


_KINDS = {
    "Retrieval": "retrieve",
    "Embedding": "embed",
    "ReRank": "rerank",
    "Synthesize": "generate",
    "GetResponse": "generate",
    "LLMCompletion": "generate",
    "LLMChat": "generate",
    "LLMStructuredPredict": "parse",
    "Query": "other",
}
