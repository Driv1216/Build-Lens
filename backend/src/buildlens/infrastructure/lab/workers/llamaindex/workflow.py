"""LI-WF: the native Stage 9 retrieval flow re-expressed as a LlamaIndex Workflow.

Steps mirror ContextService: retrieve (<=100) -> optional cross-encoder rerank of the
first 32 -> select 8 seeds -> one-hop adjacent-part expansion. Every step calls BuildLens
through the bridge, so a parity check against NativePipeline isolates orchestration from
retrieval behaviour. Packing uses the shared lab packer (not the native canonical-JSON
budget), which is reported as a known difference.
"""

from __future__ import annotations

import asyncio
from typing import Any

from llama_index.core.workflow import Context, Event, StartEvent, StopEvent, Workflow, step

from buildlens.domain.lab import LabItem, pack_context
from buildlens.infrastructure.lab.workers.common.items import item
from buildlens.infrastructure.lab.workers.common.runtime import WorkerSession

SEEDS = 8
RERANK_DEPTH = 32


class Retrieved(Event):
    hits: list[dict[str, Any]]


class Ranked(Event):
    ranked: list[dict[str, Any]]


class Seeded(Event):
    seeds: list[dict[str, Any]]
    ranked: list[dict[str, Any]]


class NativeFlow(Workflow):
    def __init__(
        self, session: WorkerSession, params: dict[str, Any], filters: dict[str, Any]
    ) -> None:
        super().__init__(timeout=120, verbose=False)
        self.session = session
        self.params = params
        self.filters = filters

    @step
    async def retrieve(self, ctx: Context, ev: StartEvent) -> Retrieved:
        await ctx.store.set("question", ev.question)
        with self.session.tracer.step("retrieve", "workflow.retrieve", "llama_index.workflow.step"):
            result = self.session.call(
                "native_candidates",
                {"query": ev.question, "mode": self.params["mode"], "filters": self.filters},
            )
        await ctx.store.set("retrieved", result["hits"])
        return Retrieved(hits=result["hits"])

    @step
    async def rerank(self, ctx: Context, ev: Retrieved) -> Ranked:
        if not self.params["rerank"]:
            return Ranked(ranked=ev.hits)
        question = await ctx.store.get("question")
        admitted = ev.hits[:RERANK_DEPTH]
        with self.session.tracer.step("rerank", "workflow.rerank", "llama_index.workflow.step"):
            result = self.session.call(
                "rerank_chunks", {"query": question, "chunk_ids": [h["chunk_id"] for h in admitted]}
            )
        scores = {r["chunk_id"]: r["score"] for r in result["scores"]}
        position = {h["chunk_id"]: i for i, h in enumerate(admitted)}
        # Native ordering: finite score desc, then retrieval rank, stable key, chunk id.
        ranked = sorted(
            admitted,
            key=lambda h: (
                scores.get(h["chunk_id"]) is None,
                -(scores.get(h["chunk_id"]) or 0.0),
                position[h["chunk_id"]],
                h.get("stable_key", ""),
                h["chunk_id"],
            ),
        )
        return Ranked(ranked=[{**h, "rerank_score": scores.get(h["chunk_id"])} for h in ranked])

    @step
    async def seed(self, ctx: Context, ev: Ranked) -> Seeded:
        return Seeded(seeds=ev.ranked[:SEEDS], ranked=ev.ranked)

    @step
    async def expand(self, ctx: Context, ev: Seeded) -> StopEvent:
        with self.session.tracer.step("expand", "workflow.expand", "llama_index.workflow.step"):
            result = self.session.call(
                "neighbours", {"chunk_ids": [s["chunk_id"] for s in ev.seeds]}
            )
        return StopEvent(
            result={
                "retrieved": await ctx.store.get("retrieved"),
                "ranked": ev.ranked,
                "seeds": ev.seeds,
                "neighbours": result["neighbours"],
            }
        )


def run_workflow(
    session: WorkerSession, question: str, params: dict[str, Any], filters: dict[str, Any]
) -> dict[str, Any]:
    flow = NativeFlow(session, params, filters)
    with session.tracer.step("other", "NativeFlow.run", "llama_index.workflow") as attrs:
        output = asyncio.run(_run(flow, question))
        attrs["steps"] = ["retrieve", "rerank", "seed", "expand"]

    def lab(hit: dict[str, Any], rank: int, score_key: str, kind: str) -> dict[str, Any]:
        return item(
            ref=hit["chunk_id"],
            rank=rank,
            text=hit["content"],
            metadata=hit,
            source_text=None,
            start_hint=None,
            score=hit.get(score_key),
            score_kind=kind,
        )

    retrieved = [
        lab(h, r, "score", str(h.get("score_kind"))) for r, h in enumerate(output["retrieved"], 1)
    ]
    score_key = "rerank_score" if params["rerank"] else "score"
    ranked = [
        lab(h, r, score_key, "rerank_logit" if params["rerank"] else str(h.get("score_kind")))
        for r, h in enumerate(output["ranked"], 1)
    ]
    by_id = {h["chunk_id"]: h for h in output["retrieved"]}
    candidates: list[dict[str, Any]] = []
    for seed in output["seeds"]:
        candidates.append(lab(seed, len(candidates) + 1, score_key, "seed"))
    neighbour_ids = [n["chunk_id"] for n in output["neighbours"]]
    neighbours = [
        item(
            ref=cid,
            rank=0,
            text="",
            metadata={"chunk_id": cid},
            source_text=None,
            start_hint=None,
            score=None,
            score_kind="neighbour",
            detail={"reference_only": True},
        )
        if cid not in by_id
        else lab(by_id[cid], 0, "score", "neighbour")
        for cid in neighbour_ids
        if cid not in {c["ref"] for c in candidates}
    ]
    for neighbour in neighbours:
        neighbour["rank"] = len(candidates) + 1
        candidates.append(neighbour)
    packed, packing = pack_context([LabItem.from_dict(c) for c in candidates])
    return {
        "retrieved": retrieved,
        "ranked": ranked,
        "candidates": candidates,
        "packed_refs": [p.ref for p in packed],
        "packing": packing,
        "info": {
            "workflow": "NativeFlow",
            "seeds": [s["chunk_id"] for s in output["seeds"]],
            "neighbours": output["neighbours"],
        },
        "answer": None,
    }


async def _run(flow: NativeFlow, question: str) -> dict[str, Any]:
    handler = flow.run(question=question)
    result = await handler
    assert isinstance(result, dict)
    return result
