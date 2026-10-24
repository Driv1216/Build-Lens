"""Stage 12 benchmark runner and tiered scoring (framework-free).

Tiers are never mixed without labels:
* approved  - R1 human judgments; only for pipelines whose items are BuildLens chunks.
* span      - approved required-evidence span coverage; any anchored item counts. Always
              reported with delivered context bytes, since bigger chunks cover more.
* derived   - `derived.r1-span-projection.v1`: R1 relevant chunk spans projected onto
              framework items by overlap >= tau. NOT human-approved.
* constructed - synthetic dataset spans known by construction. NOT human-approved.
"""

from __future__ import annotations

import random
import statistics
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

from buildlens.application.evaluation import preflight
from buildlens.application.evaluation_binding import chunk_metadata
from buildlens.application.evaluation_metrics import coverage, score_result
from buildlens.domain.evaluation import (
    CUTOFFS,
    CaseBinding,
    CorpusEvidence,
    EvaluationCase,
    EvaluationDataset,
)
from buildlens.domain.lab import PIPELINES, ExperimentConfig, LabError

DERIVED_VERSION = "derived.r1-span-projection.v1"
DERIVED_TAU = 0.5
APPROVED_KEYS = ("recall", "precision", "judgment_coverage")


def rows_for(
    items: list[dict[str, Any]], corpus: CorpusEvidence
) -> tuple[list[dict[str, Any]], int]:
    """Observation rows consumable by Stage 11 metrics; duplicates dropped (first wins)."""
    chunks = {str(c.id): c for c in corpus.chunks}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates = 0
    for item in items:
        chunk_id = item.get("chunk_id")
        key = chunk_id or "lab:" + str(item["ref"])
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        if chunk_id and chunk_id in chunks:
            rows.append(chunk_metadata(chunks[chunk_id], corpus))
            continue
        span = item.get("span") if item.get("anchored") else None
        row = {
            "chunk_id": key,
            "locator": item.get("locator") if span else None,
            "artifact_fingerprint": item.get("artifact_fingerprint") if span else None,
            "span": span or {"start_offset": -1, "end_offset": -1},
            "text_bytes": item.get("text_bytes"),
        }
        if span and item.get("spans"):
            row["spans"] = item["spans"]  # verified composite segments only
        rows.append(row)
    return rows, duplicates


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def segments(entry: dict[str, Any]) -> list[tuple[int, int]]:
    """Credited source segments of a row or item: verified composite segments, else its span."""
    return [(s["start_offset"], s["end_offset"]) for s in entry.get("spans") or [entry["span"]]]


def derived_metrics(
    binding: CaseBinding, corpus: CorpusEvidence, rows: list[dict[str, Any]], answerable: bool
) -> dict[str, Any]:
    chunks = {str(c.id): c for c in corpus.chunks}
    relevant = []
    for chunk_id, is_relevant in binding.judged.items():
        chunk = chunks.get(chunk_id)
        if chunk is None:
            continue
        meta = chunk_metadata(chunk, corpus)
        relevant.append(
            (
                meta["locator"],
                meta["artifact_fingerprint"],
                (chunk.span.start_offset, chunk.span.end_offset),
                is_relevant,
            )
        )
    positives = [r for r in relevant if r[3]]
    if not answerable or not positives:
        return {"version": DERIVED_VERSION, "eligible": False}

    def item_relevant(row: dict[str, Any]) -> bool | None:
        span = row["span"]
        if row.get("locator") is None or span["start_offset"] < 0:
            return None
        pieces = segments(row)
        length = max(1, sum(b - a for a, b in pieces))

        def overlap(target: tuple[int, int]) -> int:
            return sum(_overlap(target, piece) for piece in pieces)

        judged = [
            r
            for r in relevant
            if r[0] == row["locator"] and r[1] == row.get("artifact_fingerprint") and overlap(r[2])
        ]
        if not judged:
            return None  # overlaps no judged chunk: unjudged under the projection
        positive_overlap = sum(overlap(r[2]) for r in judged if r[3])
        covers_one = any(
            r[3] and overlap(r[2]) >= DERIVED_TAU * (r[2][1] - r[2][0]) for r in judged
        )
        return positive_overlap >= DERIVED_TAU * length or covers_one

    result: dict[str, Any] = {"version": DERIVED_VERSION, "tau": DERIVED_TAU, "eligible": True}
    flags = [item_relevant(r) for r in rows]
    first = next((i for i, f in enumerate(flags[:32], 1) if f), None)
    result["rr@32"] = 1 / first if first else 0.0
    for k in CUTOFFS:
        top = rows[:k]
        top_flags = flags[:k]
        covered = 0
        for locator, fingerprint, span, _positive in positives:
            union = sorted(
                piece
                for r in top
                if r.get("locator") == locator and r.get("artifact_fingerprint") == fingerprint
                for piece in segments(r)
            )
            amount = sum(_overlap(span, u) for u in _merge(union))
            if amount >= DERIVED_TAU * (span[1] - span[0]):
                covered += 1
        judged = [f for f in top_flags if f is not None]
        result[str(k)] = {
            "recall": covered / len(positives),
            "precision": sum(1 for f in judged if f) / len(top) if top else None,
            "judged_fraction": len(judged) / len(top) if top else None,
        }
    return result


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def tiers(
    config: ExperimentConfig,
    case: EvaluationCase,
    binding: CaseBinding,
    corpus: CorpusEvidence,
    observations: dict[str, Any],
    context_bytes: int,
) -> dict[str, Any]:
    raw = score_result(case, binding, observations, "success")
    native_identity = PIPELINES[config.pipeline].native_chunk_identity
    approved: dict[str, Any] = {}
    span: dict[str, Any] = {}
    ranking = raw.get("ranking")
    if ranking is not None:
        if native_identity and ranking["rr@32"] is not None:
            approved["MRR@32"] = ranking["rr@32"]
        for k in CUTOFFS:
            values = ranking[str(k)]
            if native_identity:
                for metric in APPROVED_KEYS:
                    if values[metric] is not None:
                        approved[f"{metric}@{k}"] = values[metric]
            evidence = values["required_evidence"]
            if evidence["coverage"] is not None:
                span[f"evidence_coverage@{k}"] = evidence["coverage"]
                span[f"evidence_complete@{k}"] = float(evidence["complete"])
    context = raw.get("context")
    if context and context["coverage"] is not None:
        span["context_coverage"] = context["coverage"]
        span["context_complete"] = float(context["complete"])
        span["context_kib"] = context_bytes / 1024
        span["context_coverage_per_kib"] = context["coverage"] / max(context_bytes / 1024, 1e-9)
    derived = derived_metrics(binding, corpus, observations.get("ranking") or [], case.answerable)
    return {"approved": approved, "span": span, "derived": derived, "raw": raw}


def flatten_derived(derived: dict[str, Any]) -> dict[str, float]:
    if not derived.get("eligible"):
        return {}
    result = {"derived_MRR@32": derived["rr@32"]}
    for k in CUTOFFS:
        for metric in ("recall", "precision"):
            value = derived[str(k)][metric]
            if value is not None:
                result[f"derived_{metric}@{k}"] = value
    return result


Query = Callable[[ExperimentConfig, str], dict[str, Any]]


class LabBenchmarkRunner:
    def __init__(
        self, service: Any, writer: Any, *, progress: Callable[[str], None] | None = None
    ) -> None:
        self.service = service
        self.writer = writer
        self.progress = progress or (lambda _: None)

    def run(
        self,
        dataset: EvaluationDataset,
        configs: list[ExperimentConfig],
        project_id: UUID,
        snapshot_id: UUID,
        *,
        implementation: dict[str, Any],
        selected_cases: list[str] | None = None,
        repetitions: int = 1,
    ) -> dict[str, Any]:
        names = [c.name for c in configs]
        if len(set(names)) != len(names):
            raise LabError("LAB_RUN_CONFIG_NAMES_DUPLICATE")
        session = self.service.verify_corpus(project_id, snapshot_id)
        corpus = session.corpus
        bindings, invalid = preflight(dataset, corpus)
        cases = [c for c in dataset.cases if not selected_cases or c.id in selected_cases]
        manifest: dict[str, Any] = {
            "version": "stage12.lab-run.v1",
            "dataset_id": dataset.id,
            "dataset_version": dataset.version,
            "dataset_review_status": dataset.review_status,
            "corpus_digest": session.digest,
            "pipelines": {
                c.name: {"pipeline": c.pipeline, "digest": c.digest, "config": c.as_dict()}
                for c in configs
            },
            "cases": [c.id for c in cases],
            "invalid_bindings": invalid,
            "implementation": implementation,
            "repetitions": repetitions,
            "status": "running",
            "counts": {"success": 0, "error": 0},
            "builds": {},
        }
        self.writer.start(manifest)
        for config in configs:
            started = time.perf_counter()
            try:
                build = self.service.build(config, project_id, snapshot_id)
                manifest["builds"][config.name] = {
                    "index_id": build.get("index_id"),
                    "nodes": build.get("nodes"),
                    "build_ms": build.get(
                        "build_ms", round((time.perf_counter() - started) * 1000, 1)
                    ),
                    "embedding": build.get("embedding"),
                    "reuse_verification": build.get("reuse_verification"),
                    "gateway": build.get("gateway"),
                    "worker_peak_rss": build.get("worker_peak_rss"),
                    "worker_spawn_ms": build.get("worker_spawn_ms"),
                    "framework_versions": build.get("framework_versions"),
                }
            except LabError as error:
                manifest["builds"][config.name] = {"error": error.code}
                continue
            for case in cases:
                binding = bindings.get(case.id)
                if binding is None:
                    continue
                self.progress(f"{config.name} {case.id}")
                timings: list[float] = []
                try:
                    result: dict[str, Any] = {}
                    for _ in range(repetitions):
                        result = self.service.query(config, project_id, snapshot_id, case.question)
                        timings.append(result["timings"]["total_ms"])
                    retrieval, dup_r = rows_for(result["retrieved"], corpus)
                    ranking, dup_k = rows_for(result["ranked"], corpus)
                    context, dup_c = rows_for(result["context"], corpus)
                    observations = {"retrieval": retrieval, "ranking": ranking, "context": context}
                    context_bytes = sum(int(i.get("text_bytes") or 0) for i in result["context"])
                    gateway = manifest["builds"][config.name].get("gateway") or {}
                    available = gateway.get("available_locators")
                    targets_available = available is None or all(
                        t.locator in set(available) for t in case.targets
                    )
                    scored = tiers(config, case, binding, corpus, observations, context_bytes)
                    record = {
                        "case_id": case.id,
                        "category": case.category,
                        "answerable": case.answerable,
                        "pipeline": config.name,
                        "outcome": "success",
                        "targets_available": targets_available,
                        "observations": {
                            name: [
                                {
                                    k: r.get(k)
                                    for k in ("chunk_id", "locator", "artifact_fingerprint", "span")
                                }
                                for r in rows[:64]
                            ]
                            for name, rows in observations.items()
                        },
                        "duplicates_dropped": {
                            "retrieval": dup_r,
                            "ranking": dup_k,
                            "context": dup_c,
                        },
                        "tiers": {k: v for k, v in scored.items() if k != "raw"},
                        "metrics_raw": scored["raw"],
                        "anchored_fraction": {
                            name: (
                                sum(1 for i in result[name] if i.get("anchored"))
                                / len(result[name])
                                if result[name]
                                else None
                            )
                            for name in ("retrieved", "context")
                        },
                        "timings_ms": timings,
                        "verification": result.get("verification"),
                        "embedding": result.get("embedding"),
                        "bridge": result.get("bridge"),
                        "answer": result.get("answer"),
                        "info": result.get("info"),
                        "steps": result.get("steps", [])[:200],
                        "packing": result.get("packing"),
                    }
                    manifest["counts"]["success"] += 1
                except LabError as error:
                    record = {
                        "case_id": case.id,
                        "pipeline": config.name,
                        "outcome": "error",
                        "error": error.code,
                    }
                    manifest["counts"]["error"] += 1
                self.writer.case(record)
        try:
            self.service.verify_corpus(project_id, snapshot_id)
            manifest["status"] = "completed"
        except LabError:
            manifest["status"] = "authority_changed"
        self.writer.finish(manifest)
        return manifest


# ---------------------------------------------------------------------------- reporting


def matched_cases(records: list[dict[str, Any]], pipelines: list[str]) -> set[str]:
    """Answerable cases every pipeline ran successfully with all target artifacts available."""
    by_case: dict[str, dict[str, dict[str, Any]]] = {}
    for record in records:
        by_case.setdefault(record["case_id"], {})[record["pipeline"]] = record
    return {
        case
        for case, entries in by_case.items()
        if set(entries) >= set(pipelines)
        and all(
            e.get("outcome") == "success"
            and e.get("answerable")
            and e.get("targets_available", True)
            for e in entries.values()
        )
    }


def summarize(records: list[dict[str, Any]], pipelines: list[str]) -> dict[str, Any]:
    matched = matched_cases(records, pipelines)
    full = _summarize(records, pipelines)
    subset = _summarize([r for r in records if r["case_id"] in matched], pipelines)
    return {
        **full,
        "matched": {
            "cases": sorted(matched),
            "means": subset["means"],
            "categories": subset["categories"],
        },
    }


def _summarize(records: list[dict[str, Any]], pipelines: list[str]) -> dict[str, Any]:
    by_pipeline: dict[str, dict[str, list[float]]] = {p: {} for p in pipelines}
    by_category: dict[str, dict[str, dict[str, list[float]]]] = {p: {} for p in pipelines}
    latency: dict[str, list[float]] = {p: [] for p in pipelines}
    for record in records:
        if record.get("outcome") != "success":
            continue
        name = record["pipeline"]
        latency[name].extend(record["timings_ms"])
        if not record["answerable"]:
            continue
        values = {
            **{f"approved:{k}": v for k, v in record["tiers"]["approved"].items()},
            **{f"span:{k}": v for k, v in record["tiers"]["span"].items()},
            **{f"derived:{k}": v for k, v in flatten_derived(record["tiers"]["derived"]).items()},
        }
        for key, value in values.items():
            by_pipeline[name].setdefault(key, []).append(float(value))
            by_category[name].setdefault(record["category"], {}).setdefault(key, []).append(
                float(value)
            )
    means = {
        p: {k: {"mean": round(statistics.fmean(v), 6), "n": len(v)} for k, v in sorted(m.items())}
        for p, m in by_pipeline.items()
    }
    categories = {
        p: {
            c: {k: round(statistics.fmean(v), 6) for k, v in sorted(m.items())}
            for c, m in sorted(cats.items())
        }
        for p, cats in by_category.items()
    }
    lat = {
        p: {
            "n": len(v),
            "p50_ms": round(statistics.median(v), 2) if v else None,
            "p95_ms": round(sorted(v)[max(0, int(round(0.95 * len(v))) - 1)], 2) if v else None,
            "mean_ms": round(statistics.fmean(v), 2) if v else None,
        }
        for p, v in latency.items()
    }
    return {"means": means, "categories": categories, "latency": lat}


def paired(
    records: list[dict[str, Any]], baseline: str, candidate: str, metric: str, seed: int = 12
) -> dict[str, Any]:
    """Per-case paired comparison on one tier-qualified metric (e.g. 'span:context_coverage')."""
    tier, key = metric.split(":", 1)

    def values(name: str) -> dict[str, float]:
        result = {}
        for record in records:
            if (
                record.get("pipeline") != name
                or record.get("outcome") != "success"
                or not record["answerable"]
            ):
                continue
            source = (
                flatten_derived(record["tiers"]["derived"])
                if tier == "derived"
                else record["tiers"][tier]
            )
            if key in source:
                result[record["case_id"]] = float(source[key])
        return result

    left, right = values(baseline), values(candidate)
    cases = sorted(set(left) & set(right))
    deltas = [right[c] - left[c] for c in cases]
    if not deltas:
        return {"metric": metric, "n": 0}
    rng = random.Random(seed)
    boots = sorted(statistics.fmean(rng.choice(deltas) for _ in deltas) for _ in range(2000))
    return {
        "metric": metric,
        "baseline": baseline,
        "candidate": candidate,
        "n": len(deltas),
        "mean_delta": round(statistics.fmean(deltas), 6),
        "wins": sum(1 for d in deltas if d > 1e-12),
        "losses": sum(1 for d in deltas if d < -1e-12),
        "ties": sum(1 for d in deltas if abs(d) <= 1e-12),
        "bootstrap_95": [round(boots[50], 6), round(boots[1949], 6)],
        "note": "Descriptive paired bootstrap over cases; not a significance test.",
    }


def constructed_coverage(
    dataset: dict[str, Any], results: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Tier D: synthetic spans known by construction (unsupported-language experiment)."""
    per_case = {}
    for case in dataset["cases"]:
        result = results.get(case["id"])
        if result is None:
            continue
        target = (case["start_offset"], case["end_offset"])
        entry: dict[str, Any] = {"language": case["language"]}
        for k in (1, 5, 10):
            spans = sorted(
                piece
                for i in result["ranked"][:k]
                if i.get("anchored")
                and i.get("locator") == case["locator"]
                and i.get("artifact_fingerprint") == case["fingerprint"]
                for piece in segments(i)
            )
            covered = sum(_overlap(target, s) for s in _merge(spans)) >= target[1] - target[0]
            entry[f"covered@{k}"] = covered
        entry["context_covered"] = any(
            i.get("anchored")
            and i.get("locator") == case["locator"]
            and any(a <= target[0] and b >= target[1] for a, b in segments(i))
            for i in result["context"]
        )
        entry["context_kib"] = round(
            sum(int(i.get("text_bytes") or 0) for i in result["context"]) / 1024, 3
        )
        per_case[case["id"]] = entry
    return {"tier": "constructed", "status": dataset["status"], "cases": per_case}


def coverage_only(
    case: EvaluationCase, binding: CaseBinding, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    return coverage(case, binding, rows)
