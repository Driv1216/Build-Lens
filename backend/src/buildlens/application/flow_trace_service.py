"""Stage 15 Milestone C: verified ``FlowTrace`` reads, entry discovery and pinning (D4).

Traces are computed on demand from a per-run graph cache. Every response re-verifies the
elements it returns, the cache's digests against the stored ones, the evidence items and the
rows they cite (INV-6), and fails closed. A pinned trace re-verifies on read as ``current``,
``stale`` (with reasons) or ``integrity_failed`` (INV-7). Natural-language search only proposes
entry points with their reasons; the user chooses (plan §3.1).
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Literal, Protocol
from uuid import UUID

from buildlens.application.flow import FlowIntegrityError, FlowNotFoundError, FlowService
from buildlens.application.flow_inputs import ScopeInput
from buildlens.application.flow_trace import TraceEngine, TraceGraph
from buildlens.domain.flow import FlowEdge, FlowNode, FlowRun
from buildlens.domain.flow_trace import (
    FLOW_TRACE_VERSION,
    MAX_DEPTH,
    MAX_HOPS,
    VERIFICATION_BUDGET,
    TraceQuery,
    flow_trace_contract,
    trace_digest,
)
from buildlens.domain.source import digest

_BACK_FOR_ENTRIES = {
    "invokes",
    "awaits",
    "invokes_until_first_await",
    "iterates",
    "resumes",
    "invokes_callback",
    "dispatch_candidate",
    "registers",
    "continues_with",
    "injects",
    "enters",
    "handled_by",
    "calls_endpoint",
    "issues_request",
    "contains_callsite",
}


class FlowTraceNotFoundError(Exception):
    pass


class FlowTraceTooLargeError(Exception):
    pass


class FlowTraceNotPinnableError(Exception):
    pass


class FlowEntrySearchUnavailableError(Exception):
    pass


class TraceStore(Protocol):
    def graph(self, run_id: UUID) -> tuple[dict[str, FlowNode], list[FlowEdge]]: ...
    def scopes(
        self, evidence_run_id: UUID, keys: list[tuple[str, str]]
    ) -> dict[tuple[str, str], ScopeInput]: ...
    def fingerprints(self, evidence_run_id: UUID, locators: set[str]) -> dict[str, str | None]: ...
    def verify(
        self,
        run: FlowRun,
        node_keys: list[str],
        edge_keys: list[str],
        expected: dict[str, str] | None = None,
    ) -> list[str]: ...
    def pin(
        self, run: FlowRun, trace_digest: str, query: dict[str, Any], result: dict[str, Any]
    ) -> None: ...
    def pinned(
        self, project_id: UUID, snapshot_id: UUID, trace_digest: str
    ) -> list[tuple[UUID, dict[str, Any], dict[str, Any]]]: ...


class TraceGraphCache:
    """A small LRU of run graphs, keyed by (run, graph digest); evicted on integrity failure."""

    def __init__(self, size: int = 2) -> None:
        self._size = size
        self._values: OrderedDict[tuple[UUID, str], TraceGraph] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, run: FlowRun, store: TraceStore) -> TraceGraph:
        key = (run.id, run.graph_digest or "")
        with self._lock:
            if key in self._values:
                self._values.move_to_end(key)
                return self._values[key]
        nodes, edges = store.graph(run.id)
        graph = TraceGraph.of(str(run.id), run.graph_digest or "", nodes, edges)
        with self._lock:
            self._values[key] = graph
            while len(self._values) > self._size:
                self._values.popitem(last=False)
        return graph

    def evict(self, run: FlowRun) -> None:
        with self._lock:
            self._values.pop((run.id, run.graph_digest or ""), None)


class FlowTraceService:
    def __init__(
        self,
        flow: FlowService,
        store: TraceStore,
        retrieval: Any | None = None,
        cache: TraceGraphCache | None = None,
    ) -> None:
        self._flow, self._store, self._retrieval = flow, store, retrieval
        self._cache = cache or TraceGraphCache()

    # ----------------------------------------------------------------- traces

    def trace(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID | None, query: TraceQuery
    ) -> dict[str, Any]:
        run, reasons = self._flow.select(project_id, snapshot_id, run_id)
        body = self._compute(run, query)
        return {**body, "freshness_reasons": list(reasons)}

    def _compute(self, run: FlowRun, query: TraceQuery) -> dict[str, Any]:
        graph = self._cache.get(run, self._store)
        if query.entry not in graph.nodes:
            raise FlowNotFoundError("Flow node not found.")
        engine = self._engine(run, graph)
        result = engine.backward(query) if query.direction == "backward" else engine.forward(query)
        node_keys = sorted({h["node"] for h in result["hops"] if h["node"] in graph.nodes})
        edge_keys = sorted({s["edge"] for s in result["steps"]})
        if len(node_keys) + len(edge_keys) > VERIFICATION_BUDGET:
            raise FlowTraceTooLargeError("The trace exceeds the verification budget.")
        expected = {k: graph.nodes[k].element_digest() for k in node_keys}
        by_key = {e.key: e for edges in graph.out.values() for e in edges if e.key in edge_keys}
        expected.update({k: e.element_digest() for k, e in by_key.items()})
        failures = self._store.verify(run, node_keys, edge_keys, expected)
        if failures:
            self._cache.evict(run)
            raise FlowIntegrityError(failures)
        locators = {h["locator"] for h in result["hops"] if h.get("locator")}
        fingerprints = self._store.fingerprints(run.flow_evidence_run_id, locators)
        source = str(digest(sorted([loc, fingerprints.get(loc)] for loc in locators)))
        normalised = query.normalised()
        content = {
            **result,
            "source_digest": source,
            "scope_digests": dict(sorted(engine.used_scopes.items())),
        }
        return {
            **content,
            "run_id": str(run.id),
            "snapshot_id": str(run.project_version_id),
            "graph_digest": run.graph_digest,
            "query": normalised,
            "verification": "verified",
            "trace_digest": trace_digest(run.graph_digest or "", normalised, content),
        }

    def _engine(self, run: FlowRun, graph: TraceGraph) -> TraceEngine:
        evidence_run = run.flow_evidence_run_id
        return TraceEngine(
            graph,
            lambda keys: self._store.scopes(evidence_run, keys),
            run.coverage,
        )

    def path(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        run_id: UUID | None,
        source: str,
        target: str,
        max_depth: int = MAX_DEPTH,
        max_hops: int = MAX_HOPS,
    ) -> dict[str, Any]:
        run, reasons = self._flow.select(project_id, snapshot_id, run_id)
        graph = self._cache.get(run, self._store)
        if source not in graph.nodes or target not in graph.nodes:
            raise FlowNotFoundError("Flow node not found.")
        found = self._engine(run, graph).path(source, target, max_depth, max_hops)
        edge_keys = [s["edge"] for s in found["steps"]]
        node_keys = sorted({source, target})
        failures = self._store.verify(run, node_keys, edge_keys)
        if failures:
            self._cache.evict(run)
            raise FlowIntegrityError(failures)
        return {
            **found,
            "run_id": str(run.id),
            "source": source,
            "target": target,
            "freshness_reasons": list(reasons),
            "verification": "verified",
        }

    # ----------------------------------------------------------------- pinning (D4, INV-7)

    def pin(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID | None, query: TraceQuery
    ) -> dict[str, Any]:
        run, _ = self._flow.select(project_id, snapshot_id, run_id)
        body = self._compute(run, query)
        if body["status"] == "deadline":
            raise FlowTraceNotPinnableError("A trace that hit its deadline is not reproducible.")
        self._store.pin(run, body["trace_digest"], body["query"], body)
        return body

    def pinned(self, project_id: UUID, snapshot_id: UUID, digest_: str) -> dict[str, Any]:
        records = self._store.pinned(project_id, snapshot_id, digest_)
        if not records:
            raise FlowTraceNotFoundError("Pinned trace not found.")
        run_id, query, result = records[-1]
        run = self._flow.run(project_id, snapshot_id, run_id)
        reasons = list(self._flow.freshness(project_id, snapshot_id, run))
        if result.get("contract") != FLOW_TRACE_VERSION:
            # Pinned under an earlier trace contract: recomputation follows different rules,
            # so a digest difference is staleness, not an integrity failure.
            reasons.append("TRACE_CONTRACT_CHANGED")
        status: Literal["current", "stale", "integrity_failed"]
        failures: list[str] = []
        if reasons:
            status = "stale"
        else:
            try:
                again = self._compute(run, _query(query))
                status = "current" if again["trace_digest"] == digest_ else "integrity_failed"
                if status == "integrity_failed":
                    failures = ["TRACE_DIGEST_MISMATCH"]
            except FlowIntegrityError as error:
                status, failures = "integrity_failed", error.failures
        return {
            "status": status,
            "freshness_reasons": list(reasons),
            "failures": failures[:50],
            "trace_digest": digest_,
            "run_id": str(run_id),
            "query": query,
            "trace": result,
        }

    # ----------------------------------------------------------------- entry points

    def entries(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        run_id: UUID | None,
        kind: str | None = None,
        q: str | None = None,
        mode: str = "lexical",
        limit: int = 50,
    ) -> dict[str, Any]:
        run, reasons = self._flow.select(project_id, snapshot_id, run_id)
        graph = self._cache.get(run, self._store)
        base = {
            "run_id": str(run.id),
            "freshness_reasons": list(reasons),
            "contract": flow_trace_contract(),
        }
        if not q:
            values = [_entry_json(n, graph) for n in graph.entries(kind)]
            return {**base, "mode": None, "items": values[:limit], "total": len(values)}
        if self._retrieval is None:
            raise FlowEntrySearchUnavailableError("Retrieval is not configured.")
        result = self._retrieval.search(project_id, snapshot_id, mode, q, top_k=10)
        by_identity: dict[str, list[str]] = {}
        for key, node in graph.nodes.items():
            identity = node.attributes.get("identity")
            if identity and key.startswith("scope:"):
                by_identity.setdefault(str(identity), []).append(key)
        found: dict[str, dict[str, Any]] = {}
        for hit in result.hits:
            identity = hit.chunk.symbol_identity_id
            for scope in by_identity.get(str(identity), []) if identity else []:
                for entry, distance in _entries_reaching(graph, scope, 8):
                    if kind and graph.nodes[entry].attributes.get("kind") != kind:
                        continue
                    reason = {
                        "hit_rank": hit.rank,
                        "chunk_locator": hit.chunk.locator,
                        "matched_scope": scope,
                        "distance": distance,
                    }
                    current = found.get(entry)
                    rank = (hit.rank, distance)
                    if current is None or rank < current["rank"]:
                        found[entry] = {"rank": rank, "reason": reason}
        ordered = sorted(found.items(), key=lambda item: (item[1]["rank"], item[0]))
        items = [
            {**_entry_json(graph.nodes[key], graph), "reason": value["reason"]}
            for key, value in ordered[:limit]
        ]
        return {
            **base,
            "mode": result.executed_mode,
            "requested_mode": mode,
            "items": items,
            "total": len(ordered),
            "selection": "the user chooses an entry point; nothing is selected automatically",
        }


def _entries_reaching(graph: TraceGraph, scope: str, max_depth: int) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    seen, frontier, depth = {scope}, [scope], 0
    while frontier and depth <= max_depth:
        nxt: list[str] = []
        for current in frontier:
            for e in graph.inc.get(current, []):
                if e.relation not in _BACK_FOR_ENTRIES or e.source_key in seen:
                    continue
                seen.add(e.source_key)
                node = graph.nodes.get(e.source_key)
                if node is not None and node.kind == "entry_point":
                    found.append((e.source_key, depth))
                else:
                    nxt.append(e.source_key)
        frontier, depth = sorted(nxt), depth + 1
    return found


def _entry_json(node: FlowNode, graph: TraceGraph) -> dict[str, Any]:
    target = next((e for e in graph.out.get(node.key, []) if e.relation == "enters"), None)
    return {
        "key": node.key,
        "kind": node.attributes.get("kind"),
        "label": node.label,
        "locator": node.locator,
        "target": target.target_key if target else None,
        "tier": target.tier if target else None,
        "conditions": list(target.conditions) if target else [],
    }


def _query(value: dict[str, Any]) -> TraceQuery:
    bounds = value.get("bounds") or {}
    return TraceQuery(
        entry=str(value["entry"]),
        direction=value.get("direction", "forward"),
        max_depth=int(bounds.get("max_depth", MAX_DEPTH)),
        max_hops=int(bounds.get("max_hops", MAX_HOPS)),
        fanout=int(bounds.get("fanout", 8)),
    )
