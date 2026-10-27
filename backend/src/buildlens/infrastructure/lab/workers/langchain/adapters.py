"""LangChain extension points implemented over the BuildLens capability bridge."""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from typing import Any
from uuid import UUID

import bm25s
from langchain_core.callbacks import BaseCallbackHandler, CallbackManagerForRetrieverRun
from langchain_core.documents import BaseDocumentCompressor, Document
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.retrievers import BaseRetriever
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict, PrivateAttr

from buildlens.infrastructure.lab.workers.common.items import offline_answer
from buildlens.infrastructure.lab.workers.common.runtime import EmbeddingLedger, Tracer

_TOKEN = re.compile(r"[A-Za-z][a-z]+|[A-Z]+(?![a-z])|\d+")


def tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text)] or ["<empty>"]


class BuildLensEmbeddings(Embeddings):
    """LangChain `Embeddings` whose documents are resolved through the embedding ledger.

    `embed_documents` receives the exact text LangChain will index (page_content). The
    ledger reuses verified stored vectors by sha256(text) or computes through the bridge.
    """

    def __init__(self, ledger: EmbeddingLedger) -> None:
        self.ledger = ledger
        self._queries: dict[str, list[float]] = {}

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        # LangChain embeds page_content verbatim: record that observation for every text.
        for text in texts:
            self.ledger.note("", text, text, "identity")
        return self.ledger.documents(list(texts))

    def embed_query(self, text: str) -> list[float]:
        if text not in self._queries:
            self._queries[text] = self.ledger.query(text)
        return self._queries[text]


class NativeBridgeRetriever(BaseRetriever):
    """LC-A: BuildLens Stage 8 ranking exposed as a LangChain retriever."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    session: Any
    mode: str = "hybrid"
    filters: dict[str, Any] = {}

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        result = self.session.call(
            "native_candidates", {"query": query, "mode": self.mode, "filters": self.filters}
        )
        documents = []
        for hit in result["hits"]:
            metadata = {k: v for k, v in hit.items() if k != "content"}
            documents.append(
                Document(page_content=hit["content"], metadata=metadata, id=hit["chunk_id"])
            )
        return documents


class BM25Retriever(BaseRetriever):
    """Okapi BM25 (bm25s) over the same identifier-aware tokens as the hash embedder."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    documents: list[Document]
    k: int = 32
    _index: Any = PrivateAttr(default=None)

    def model_post_init(self, __context: Any) -> None:
        self._index = bm25s.BM25(k1=1.5, b=0.75)
        self._index.index([tokens(d.page_content) for d in self.documents], show_progress=False)

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        k = min(self.k, len(self.documents))
        if k == 0:
            return []
        ids, scores = self._index.retrieve([tokens(query)], k=k, show_progress=False)
        result = []
        for index, score in zip(ids[0], scores[0], strict=True):
            if float(score) <= 0:
                continue
            doc = self.documents[int(index)]
            result.append(
                Document(
                    page_content=doc.page_content,
                    metadata={**doc.metadata, "bm25_score": float(score)},
                    id=doc.id,
                )
            )
        return result


class CrossEncoderCompressor(BaseDocumentCompressor):
    """Reranks documents with the BuildLens cross-encoder via the bridge."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    session: Any
    top_n: int = 32

    def compress_documents(
        self, documents: Sequence[Document], query: str, callbacks: Any = None
    ) -> Sequence[Document]:
        refs = [str(d.id or d.metadata.get("ref") or i) for i, d in enumerate(documents)]
        items = [
            {"ref": ref, "text": d.page_content} for ref, d in zip(refs, documents, strict=True)
        ]
        scores: dict[str, float | None] = {}
        for start in range(0, len(items), 32):
            result = self.session.call(
                "rerank", {"query": query, "items": items[start : start + 32]}
            )
            scores.update({row["ref"]: row["score"] for row in result["scores"]})
        ranked = sorted(
            zip(refs, documents, strict=True),
            key=lambda pair: (scores.get(pair[0]) is None, -(scores.get(pair[0]) or 0.0)),
        )
        return [
            Document(
                page_content=d.page_content,
                metadata={**d.metadata, "rerank_score": scores.get(r)},
                id=d.id,
            )
            for r, d in ranked[: self.top_n]
        ]


class OfflineChatModel(BaseChatModel):
    """Deterministic offline chat model. Supports bind_tools, so LangChain's own
    `with_structured_output` (function calling) and `create_agent` loops run for real.
    Its answers are extractive placeholders: mechanics only, never quality evidence."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    question: str = ""
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "buildlens-offline-extractive"

    def bind_tools(
        self, tools: Sequence[Any], *, tool_choice: str | None = None, **kwargs: Any
    ) -> Any:
        formatted = [convert_to_openai_tool(t) for t in tools]
        return self.bind(tools=formatted, tool_choice=tool_choice, **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls += 1
        tools = {t["function"]["name"]: t for t in kwargs.get("tools") or []}
        prompt = "\n".join(str(m.content) for m in messages)
        message = self._decide(messages, tools, prompt)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _decide(self, messages: list[BaseMessage], tools: dict[str, Any], prompt: str) -> AIMessage:
        tool_results = [m for m in messages if isinstance(m, ToolMessage)]
        if "buildlens_search" in tools:
            searched = [m for m in tool_results if m.name == "buildlens_search"]
            read = [m for m in tool_results if m.name == "buildlens_read_chunk"]
            if not searched:
                return _tool_call("buildlens_search", {"query": self.question or prompt[-400:]}, 1)
            hits = re.findall(r'"chunk_id":\s*"([0-9a-f-]{36})"', str(searched[-1].content))
            already = {
                re.search(r'"chunk_id":\s*"([0-9a-f-]{36})"', str(m.content)).group(1)  # type: ignore[union-attr]
                for m in read
                if re.search(r'"chunk_id":\s*"([0-9a-f-]{36})"', str(m.content))
            }
            pending = [h for h in hits if h not in already]
            if pending and len(read) < 2:
                return _tool_call(
                    "buildlens_read_chunk", {"chunk_id": pending[0]}, len(tool_results) + 1
                )
        structured = next(
            (
                n
                for n in tools
                if n not in {"buildlens_search", "buildlens_read_chunk", "buildlens_context"}
            ),
            None,
        )
        answer = offline_answer(prompt)
        if structured is not None:
            return _tool_call(structured, answer, len(tool_results) + 100)
        return AIMessage(content=str(answer))


def _tool_call(name: str, args: dict[str, Any], index: int) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": f"call_{index}", "type": "tool_call"}],
    )


class TraceCallback(BaseCallbackHandler):
    """Maps LangChain callback events onto LabSteps (run-scoped via RunnableConfig)."""

    def __init__(self, tracer: Tracer) -> None:
        self.tracer = tracer
        self._started: dict[UUID, tuple[float, str, str]] = {}

    def _start(self, run_id: UUID, kind: str, name: str) -> None:
        self._started[run_id] = (time.perf_counter(), kind, name)

    def _end(
        self, run_id: UUID, event: str, attributes: dict[str, Any], refs: list[str] | None = None
    ) -> None:
        started = self._started.pop(run_id, None)
        if started is None:
            return
        began, kind, name = started
        self.tracer.add(kind, name, event, (time.perf_counter() - began) * 1000, attributes, refs)

    def on_retriever_start(
        self, serialized: Any, query: str, *, run_id: UUID, **kwargs: Any
    ) -> None:
        name = (serialized or {}).get("name") or kwargs.get("name") or "retriever"
        self._start(run_id, "retrieve", str(name))

    def on_retriever_end(
        self, documents: Sequence[Document], *, run_id: UUID, **kwargs: Any
    ) -> None:
        refs = [str(d.id or d.metadata.get("chunk_id") or "") for d in documents]
        self._end(run_id, "on_retriever_end", {"documents": len(documents)}, refs)

    def on_chat_model_start(
        self, serialized: Any, messages: list[list[BaseMessage]], *, run_id: UUID, **kwargs: Any
    ) -> None:
        self._start(run_id, "generate", "chat_model")
        chars = sum(len(str(m.content)) for batch in messages for m in batch)
        began = self._started[run_id][0]
        self._started[run_id] = (
            began,
            "generate",
            f"chat_model ({chars} chars)",
        )

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        generation = (
            response.generations[0][0] if response.generations and response.generations[0] else None
        )
        message = getattr(generation, "message", None)
        calls = [c["name"] for c in getattr(message, "tool_calls", []) or []]
        self._end(run_id, "on_llm_end", {"tool_calls": calls})

    def on_tool_start(
        self, serialized: Any, input_str: str, *, run_id: UUID, **kwargs: Any
    ) -> None:
        self._start(run_id, "tool", str((serialized or {}).get("name") or "tool"))

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._end(run_id, "on_tool_end", {"output_chars": len(str(output))})

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._end(run_id, "on_tool_error", {"error": type(error).__name__})
