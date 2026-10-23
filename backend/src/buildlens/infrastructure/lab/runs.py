"""Atomic, deterministic persistence and reporting for laboratory benchmark runs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from buildlens.application.lab_evaluation import paired, summarize
from buildlens.domain.lab import LabError, lab_digest
from buildlens.infrastructure.evaluation.local import atomic_write, read_json

TIER_NOTES = {
    "approved": "Human-approved R1 judgments; only pipelines returning BuildLens chunks.",
    "span": (
        "Approved required-evidence span coverage; any anchored item counts. "
        "Compare with context KiB."
    ),
    "derived": "derived.r1-span-projection.v1 (tau=0.5). NOT human-approved.",
}
KEY_METRICS = (
    "approved:MRR@32",
    "approved:recall@5",
    "approved:precision@5",
    "approved:recall@32",
    "span:evidence_coverage@5",
    "span:evidence_coverage@32",
    "span:context_coverage",
    "span:context_complete",
    "span:context_kib",
    "span:context_coverage_per_kib",
    "derived:derived_MRR@32",
    "derived:derived_recall@5",
    "derived:derived_precision@5",
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class LabRunWriter:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.manifest: dict[str, Any] = {}

    def start(self, manifest: dict[str, Any]) -> None:
        if self.directory.exists() and any(self.directory.iterdir()):
            raise LabError("LAB_RUN_EXISTS")
        (self.directory / "cases").mkdir(parents=True, exist_ok=True)
        self.manifest = {
            **manifest,
            "run_id": self.directory.name,
            "started_at": _now(),
            "result_digests": {},
        }
        atomic_write(self.directory / "manifest.json", self.manifest, replace=True)

    def case(self, record: dict[str, Any]) -> None:
        name = f"{record['case_id']}.{record['pipeline']}.json"
        atomic_write(self.directory / "cases" / name, record)
        self.manifest["result_digests"][name] = lab_digest(record)

    def finish(self, manifest: dict[str, Any]) -> None:
        digests = self.manifest["result_digests"]
        self.manifest = {
            **self.manifest,
            **manifest,
            "result_digests": digests,
            "finished_at": _now(),
        }
        atomic_write(self.directory / "manifest.json", self.manifest, replace=True)


def load_records(directory: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = read_json(directory / "manifest.json")
    records = []
    for name, expected in sorted(manifest.get("result_digests", {}).items()):
        record = read_json(directory / "cases" / name)
        if lab_digest(record) != expected:
            raise LabError("LAB_RUN_RECORD_TAMPERED", name)
        records.append(record)
    return manifest, records


def report(directory: Path, *, baseline: str | None = None) -> dict[str, Any]:
    """Recompute scores from retained per-case records only (no DB, no workers)."""
    manifest, records = load_records(directory)
    pipelines = sorted(manifest["pipelines"])
    summary = summarize(records, pipelines)
    comparisons = []
    if baseline and baseline in pipelines:
        for candidate in pipelines:
            if candidate == baseline:
                continue
            for metric in (
                "span:context_coverage",
                "span:evidence_coverage@5",
                "approved:MRR@32",
                "derived:derived_recall@5",
            ):
                result = paired(records, baseline, candidate, metric)
                if result.get("n"):
                    comparisons.append(result)
    scores = {
        "version": "stage12.lab-scores.v1",
        "run_id": manifest["run_id"],
        "corpus_digest": manifest["corpus_digest"],
        "dataset": {
            "id": manifest["dataset_id"],
            "version": manifest["dataset_version"],
            "review_status": manifest["dataset_review_status"],
        },
        "tiers": TIER_NOTES,
        "summary": summary,
        "comparisons": comparisons,
        "builds": manifest.get("builds"),
        "counts": manifest.get("counts"),
        "status": manifest.get("status"),
    }
    scores["scores_digest"] = lab_digest(scores)
    atomic_write(directory / "scores.json", scores, replace=True)
    atomic_write(directory / "summary.md", render(manifest, scores), replace=True, text=True)
    return scores


def _cell(entry: dict[str, Any] | None) -> str:
    if not entry:
        return "—"
    return f"{entry['mean']:.4f} (n={entry['n']})"


def render(manifest: dict[str, Any], scores: dict[str, Any]) -> str:
    pipelines = sorted(manifest["pipelines"])
    means = scores["summary"]["means"]
    lines = [
        "# Stage 12 framework laboratory run",
        "",
        f"- Run: `{manifest['run_id']}` — status **{manifest.get('status')}**",
        f"- Dataset: `{manifest['dataset_id']}` version `{manifest['dataset_version']}` "
        f"({manifest['dataset_review_status']})",
        f"- Corpus digest: `{manifest['corpus_digest']}`",
        f"- Counts: {json.dumps(manifest.get('counts'))}",
        "",
        "Tier labels: **approved** = human-approved R1 judgments (BuildLens chunk identity only); "
        "**span** = approved evidence-span coverage (any anchored item; read with context KiB); "
        "**derived** = span projection of R1 judgments, *not human-approved*.",
        "Offline runs use no generative model; answer fields are mechanics only.",
        "",
        "| Metric | " + " | ".join(pipelines) + " |",
        "| --- | " + " | ".join("---" for _ in pipelines) + " |",
    ]
    for metric in KEY_METRICS:
        lines.append(
            f"| {metric} | "
            + " | ".join(_cell(means.get(p, {}).get(metric)) for p in pipelines)
            + " |"
        )
    latency = scores["summary"]["latency"]
    lines.append(
        "| latency p50 ms | " + " | ".join(str(latency[p]["p50_ms"]) for p in pipelines) + " |"
    )
    lines.append(
        "| latency p95 ms | " + " | ".join(str(latency[p]["p95_ms"]) for p in pipelines) + " |"
    )
    builds = manifest.get("builds", {})
    lines += [
        "",
        "## Builds",
        "",
        "| Pipeline | nodes | build ms | embedding | reuse verified |",
        "| --- | --- | --- | --- | --- |",
    ]
    for p in pipelines:
        b = builds.get(p, {})
        embedding = b.get("embedding") or {}
        reuse = (b.get("reuse_verification") or {}).get("verified_genuine_reuse")
        lines.append(
            f"| {p} | {b.get('nodes')} | {b.get('build_ms')} | "
            f"reused={embedding.get('reused')} computed={embedding.get('computed_via_bridge')} "
            f"{embedding.get('transformations') or ''} | {reuse} |"
        )
    if scores["comparisons"]:
        lines += [
            "",
            "## Paired comparisons (descriptive)",
            "",
            "| Candidate | Metric | n | mean Δ | W/L/T | bootstrap 95% |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for c in scores["comparisons"]:
            lines.append(
                f"| {c['candidate']} | {c['metric']} | {c['n']} | {c['mean_delta']:+.4f} | "
                f"{c['wins']}/{c['losses']}/{c['ties']} | {c['bootstrap_95']} |"
            )
    lines.append("")
    return "\n".join(lines)
