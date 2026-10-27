"""N0: native BuildLens Stage 9 context as a laboratory pipeline (no framework, no worker).

Uses the unchanged ContextService and the existing passive observation hooks, so the lab
sees exactly the Stage 8 retrieval list, the Stage 11 pre-seed ranking and the packed
context that production computes.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from buildlens.application.context import ContextService
from buildlens.application.evaluation import Collector
from buildlens.application.observation import observing
from buildlens.domain.context import ContextResult, RankingObservation, RankingPolicy
from buildlens.domain.evaluation import CorpusEvidence
from buildlens.domain.lab import LabItem, LabStep
from buildlens.domain.retrieval import RetrievalFilters, RetrievalResult


def _item(
    chunk: Any,
    rank: int,
    corpus: CorpusEvidence | None,
    score: float | None,
    kind: str,
    detail: dict[str, Any] | None = None,
) -> LabItem:
    fingerprint = None
    if corpus is not None:
        artifact = next((a for a in corpus.artifacts if a.id == chunk.artifact_id), None)
        fingerprint = (
            artifact.content_fingerprint.digest
            if artifact and artifact.content_fingerprint
            else None
        )
    return LabItem(
        ref=str(chunk.id),
        rank=rank,
        text_sha256=chunk.content_hash,
        text_bytes=len(chunk.content.encode()),
        locator=chunk.locator,
        artifact_fingerprint=fingerprint,
        start_offset=chunk.span.start_offset,
        end_offset=chunk.span.end_offset,
        anchored=True,
        chunk_id=str(chunk.id),
        score=score,
        score_kind=kind,
        detail={"anchor_method": "native", **(detail or {})},
    )


class NativePipeline:
    def __init__(self, context: ContextService) -> None:
        self.context = context

    def run(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        question: str,
        params: dict[str, Any],
        filters: dict[str, str | None],
        corpus: CorpusEvidence | None,
    ) -> dict[str, Any]:
        collector = Collector()
        policy: RankingPolicy = "local_cross_encoder" if params.get("rerank") else "retrieval_order"
        started = time.perf_counter()
        with observing(collector):
            result = self.context.build(
                project_id,
                snapshot_id,
                question,
                params.get("mode", "hybrid"),
                policy,
                RetrievalFilters(
                    filters.get("locator_prefix"), filters.get("language"), filters.get("kind")
                ),
            )
        elapsed = (time.perf_counter() - started) * 1000
        retrieval = collector.values.get("retrieval")
        ranking = collector.values.get("ranking")
        retrieved: list[LabItem] = []
        if isinstance(retrieval, RetrievalResult):
            retrieved = [
                _item(
                    h.chunk,
                    h.rank,
                    corpus,
                    h.score,
                    h.score_kind,
                    {
                        "lexical_rank": h.lexical.rank if h.lexical else None,
                        "semantic_rank": h.semantic.rank if h.semantic else None,
                        "exact_rank": h.exact_rank,
                    },
                )
                for h in retrieval.hits
            ]
        chunks = {c.chunk.id: c.chunk for c in result.items}
        if isinstance(retrieval, RetrievalResult):
            chunks.update({h.chunk.id: h.chunk for h in retrieval.hits})
        ranked: list[LabItem] = []
        if isinstance(ranking, RankingObservation):
            selected = sorted(
                (c for c in ranking.candidates if c.selection_order is not None),
                key=lambda c: c.selection_order or 0,
            )
            ranked = [
                _item(
                    chunks[c.chunk_id],
                    rank,
                    corpus,
                    c.rerank_score,
                    "rerank_logit" if params.get("rerank") else "retrieval_order",
                )
                for rank, c in enumerate(selected, 1)
                if c.chunk_id in chunks
            ]
        context = [
            _item(
                i.chunk,
                rank,
                corpus,
                i.rerank_score,
                "context",
                {"seed": i.selected_as_seed, "expanded_from": [str(x) for x in i.expanded_from]},
            )
            for rank, i in enumerate(result.items, 1)
        ]
        steps = _steps(result, retrieval, elapsed)
        return {
            "retrieved": retrieved,
            "ranked": ranked or retrieved,
            "context": context,
            "steps": steps,
            "info": {
                "ranking_policy": policy,
                "reranker_state": result.reranker_state,
                "budget_used": result.budget.used,
                "budget_limit": result.budget.limit,
                "decisions": len(result.decisions),
            },
            "timings": {"pipeline_ms": round(elapsed, 3)},
            "texts": {c.content_hash: c.content for c in chunks.values()},
        }


def _steps(result: ContextResult, retrieval: object, elapsed: float) -> list[dict[str, Any]]:
    steps: list[LabStep] = []
    if isinstance(retrieval, RetrievalResult):
        steps.append(
            LabStep(
                1,
                "retrieve",
                f"Stage 8 {retrieval.executed_mode}",
                "buildlens.retrieval",
                0.0,
                {
                    "exact": retrieval.exact.candidate_count,
                    "lexical": retrieval.lexical.candidate_count,
                    "semantic": retrieval.semantic.candidate_count,
                    "returned": retrieval.returned_count,
                },
            )
        )
    steps.append(
        LabStep(
            len(steps) + 1,
            "rerank" if result.reranker_state == "completed" else "other",
            f"ranking ({result.ranking_policy})",
            "buildlens.context.rank",
            0.0,
            {"reranker_state": result.reranker_state},
        )
    )
    expanded = sum(1 for i in result.items if i.expanded_from)
    steps.append(
        LabStep(
            len(steps) + 1,
            "expand",
            "one-hop adjacent parts",
            "buildlens.context.expand",
            0.0,
            {"expanded_items": expanded},
        )
    )
    excluded = [d for d in result.decisions if d.disposition == "excluded"]
    steps.append(
        LabStep(
            len(steps) + 1,
            "pack",
            "whole-item byte packing",
            "buildlens.context.pack",
            0.0,
            {
                "items": len(result.items),
                "used": result.budget.used,
                "limit": result.budget.limit,
                "excluded": len(excluded),
            },
        )
    )
    steps.append(
        LabStep(
            len(steps) + 1, "other", "ContextService.build total", "buildlens.context", elapsed, {}
        )
    )
    return [s.as_dict() for s in steps]
