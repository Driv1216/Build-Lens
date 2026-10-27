"""AG-1: bounded, read-only `create_agent` over BuildLens tools.

Constraints enforced here (and validated before the agent is constructed):
* tools: only buildlens_search / buildlens_read_chunk, executed by the parent bridge;
* middleware: an explicit allow-list (call limits, tracing, evidence budget). Shell,
  filesystem, fallback, retry and human-in-the-loop middlewares are rejected;
* no checkpointer, no store, recursion_limit and parent deadline.
"""

from __future__ import annotations

import json
import time
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
)
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from buildlens.domain.lab import LabError, LabItem, pack_context
from buildlens.infrastructure.lab.workers.common.items import (
    CITED_ANSWER_SCHEMA,
    item,
    validate_citations,
)
from buildlens.infrastructure.lab.workers.common.runtime import WorkerSession
from buildlens.infrastructure.lab.workers.langchain.adapters import OfflineChatModel

SYSTEM = (
    "You investigate a code repository using read-only BuildLens tools. Search first, read "
    "the most relevant chunks, then answer citing the evidence identifiers returned by "
    "buildlens_read_chunk. Never invent identifiers."
)


class SearchArgs(BaseModel):
    query: str = Field(description="Natural-language or identifier query", max_length=1000)


class ReadArgs(BaseModel):
    chunk_id: str = Field(description="A chunk_id returned by buildlens_search", max_length=64)


class TraceMiddleware(AgentMiddleware):
    """Records each model and tool hook into the lab trace."""

    def __init__(self, session: WorkerSession) -> None:
        super().__init__()
        self.session = session
        self.model_calls = 0
        self.tool_calls = 0

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        self.model_calls += 1
        started = time.perf_counter()
        response = handler(request)
        self.session.tracer.add(
            "agent",
            f"model call {self.model_calls}",
            "middleware.wrap_model_call",
            (time.perf_counter() - started) * 1000,
            {"messages": len(request.messages)},
        )
        return response

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        self.tool_calls += 1
        started = time.perf_counter()
        result = handler(request)
        call = request.tool_call
        self.session.tracer.add(
            "tool",
            str(call.get("name")),
            "middleware.wrap_tool_call",
            (time.perf_counter() - started) * 1000,
            {"args": call.get("args")},
        )
        return result


class EvidenceBudgetMiddleware(AgentMiddleware):
    """Stops tool execution once delivered evidence exceeds the configured byte budget."""

    def __init__(self, budget: int, ledger: dict[str, Any]) -> None:
        super().__init__()
        self.budget = budget
        self.ledger = ledger

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        if self.ledger["bytes"] >= self.budget:
            self.ledger["budget_stops"] += 1
            return ToolMessage(
                content=json.dumps({"error": "EVIDENCE_BUDGET_EXHAUSTED"}),
                name=request.tool_call.get("name"),
                tool_call_id=request.tool_call["id"],
            )
        return handler(request)


ALLOWED_MIDDLEWARE = (
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
    TraceMiddleware,
    EvidenceBudgetMiddleware,
)


def validate_middleware(middleware: list[Any]) -> None:
    for entry in middleware:
        if type(entry) not in ALLOWED_MIDDLEWARE:
            raise LabError("LAB_MIDDLEWARE_NOT_ALLOWED", type(entry).__name__)


def run_agent(
    session: WorkerSession, question: str, params: dict[str, Any], run_config: RunnableConfig
) -> dict[str, Any]:
    seen: list[dict[str, Any]] = []  # first-retrieval order of native chunks
    read: dict[str, dict[str, Any]] = {}
    ledger: dict[str, Any] = {"bytes": 0, "budget_stops": 0, "errors": []}

    def remember(chunk_id: str, source: str, rank: int | None) -> None:
        if all(entry["chunk_id"] != chunk_id for entry in seen):
            seen.append({"chunk_id": chunk_id, "source": source, "tool_rank": rank})

    def search(query: str) -> str:
        try:
            result = session.call(
                "tool_call",
                {
                    "name": "buildlens_search",
                    "args": {"query": query, "top_k": params["tool_top_k"]},
                },
            )
        except LabError as error:
            ledger["errors"].append(error.code)
            return json.dumps({"error": error.code})
        for hit in result["hits"]:
            remember(hit["chunk_id"], "search", hit["rank"])
        payload = json.dumps(result, ensure_ascii=False)
        ledger["bytes"] += len(payload.encode())
        return payload

    def read_chunk(chunk_id: str) -> str:
        try:
            result = session.call(
                "tool_call", {"name": "buildlens_read_chunk", "args": {"chunk_id": chunk_id}}
            )
        except LabError as error:
            ledger["errors"].append(error.code)
            return json.dumps({"error": error.code})
        evidence_id = f"E{len(read) + 1}"
        read[result["chunk_id"]] = {**result, "evidence_id": evidence_id}
        remember(result["chunk_id"], "read", None)
        payload = json.dumps({"evidence_id": evidence_id, **result}, ensure_ascii=False)
        ledger["bytes"] += len(payload.encode())
        return f"[{evidence_id}] " + payload

    tools = [
        StructuredTool.from_function(
            search,
            name="buildlens_search",
            description="Search repository evidence (read-only).",
            args_schema=SearchArgs,
        ),
        StructuredTool.from_function(
            read_chunk,
            name="buildlens_read_chunk",
            description="Read one chunk's exact source (read-only).",
            args_schema=ReadArgs,
        ),
    ]
    trace = TraceMiddleware(session)
    middleware = [
        ModelCallLimitMiddleware(run_limit=params["max_model_calls"], exit_behavior="end"),
        ToolCallLimitMiddleware(run_limit=params["max_tool_calls"], exit_behavior="end"),
        trace,
        EvidenceBudgetMiddleware(params["evidence_budget_bytes"], ledger),
    ]
    validate_middleware(middleware)
    model = OfflineChatModel(question=question)
    agent = create_agent(
        model=model,
        tools=tools,
        system_prompt=SYSTEM,
        middleware=middleware,
        response_format=ToolStrategy(CITED_ANSWER_SCHEMA),
        checkpointer=None,
        store=None,
    )
    config: RunnableConfig = {**run_config, "recursion_limit": 4 * params["max_model_calls"] + 4}
    with session.tracer.step("agent", "create_agent.invoke", "langchain.agent") as attrs:
        state = agent.invoke({"messages": [HumanMessage(content=question)]}, config=config)
        attrs.update(
            {
                "model_calls": trace.model_calls,
                "tool_calls": trace.tool_calls,
                "evidence_bytes": ledger["bytes"],
            }
        )
    structured = state.get("structured_response")
    answer_ids = [r["evidence_id"] for r in read.values()]
    validation = validate_citations(structured or {}, answer_ids)
    retrieved = [
        item(
            ref=entry["chunk_id"],
            rank=rank,
            text="",
            metadata={"chunk_id": entry["chunk_id"]},
            source_text=None,
            start_hint=None,
            score=None,
            score_kind="agent_first_retrieval",
            detail={
                "reference_only": True,
                "source": entry["source"],
                "tool_rank": entry["tool_rank"],
            },
        )
        for rank, entry in enumerate(seen, 1)
    ]
    # Delivered context: chunks the agent actually read, then other surfaced chunks.
    order = [e["chunk_id"] for e in seen if e["chunk_id"] in read] + [
        e["chunk_id"] for e in seen if e["chunk_id"] not in read
    ]
    by_id = {r["ref"]: r for r in retrieved}
    candidates = [dict(by_id[c], rank=i) for i, c in enumerate(order, 1)]
    for candidate in candidates:
        content = read.get(candidate["ref"], {}).get("content")
        if content is not None:
            candidate["text_bytes"] = len(content.encode())
    packed, packing = pack_context([LabItem.from_dict(c) for c in candidates])
    messages = state.get("messages", [])
    return {
        "retrieved": retrieved,
        "ranked": retrieved,
        "candidates": candidates,
        "packed_refs": [p.ref for p in packed],
        "packing": packing,
        "info": {
            "model_calls": trace.model_calls,
            "tool_calls": trace.tool_calls,
            "evidence_bytes": ledger["bytes"],
            "budget_stops": ledger["budget_stops"],
            "tool_errors": ledger["errors"],
            "messages": len(messages),
            "limits": {
                k: params[k] for k in ("max_model_calls", "max_tool_calls", "evidence_budget_bytes")
            },
            "transcript": [
                {
                    "type": type(m).__name__,
                    "name": getattr(m, "name", None),
                    "tool_calls": [c["name"] for c in getattr(m, "tool_calls", []) or []],
                    "chars": len(str(m.content)),
                }
                for m in messages
            ][:60],
        },
        "answer": {
            "model": "buildlens-offline-extractive (mechanics only)",
            "parsed": structured,
            "buildlens_validation": validation,
            "evidence_ids": answer_ids,
        },
    }
