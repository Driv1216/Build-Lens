"""Semantic catalog and controlled generation commands (same services as the API).

``generate`` executes only an existing persisted plan, and only with its exact digest and an
explicit ``--confirm``; it can never send anything a plan did not authorise.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import typer

from buildlens.application.semantic_generation import PlanSelection, SemanticGenerationService
from buildlens.core.config import get_settings
from buildlens.infrastructure.runtime import (
    LocalRuntime,
    semantic_catalog_service,
    semantic_generation_service,
)

app = typer.Typer(help="Deterministic semantic catalog and plan-authorised generation.")


def _emit(value: Any) -> None:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    typer.echo(json.dumps(value, indent=2, sort_keys=True, default=str))


@contextmanager
def _runtime() -> Iterator[LocalRuntime]:
    runtime = LocalRuntime(get_settings())
    try:
        yield runtime
    finally:
        asyncio.run(runtime.close())


def _generation(call: Callable[[SemanticGenerationService], Any]) -> None:
    with _runtime() as runtime:
        _emit(call(semantic_generation_service(runtime)))


@app.command("catalog")
def catalog(project: UUID = typer.Option(...), snapshot: UUID = typer.Option(...)) -> None:
    """Build (or reuse) the deterministic catalog for a snapshot."""
    with _runtime() as runtime:
        _emit(semantic_catalog_service(runtime).build(project, snapshot))


@app.command("status")
def status() -> None:
    """Show semantic generation configuration (no provider is contacted)."""
    _generation(lambda service: service.configuration())


@app.command("plan")
def plan(
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    entity: list[str] = typer.Option([], "--entity"),
    locator_prefix: str | None = typer.Option(None, "--locator-prefix"),
    kind: list[str] = typer.Option([], "--kind"),
    stale_only: bool = typer.Option(False, "--stale-only"),
    mode: str = typer.Option("generate", "--mode"),
    accept_profile: list[str] = typer.Option([], "--accept-profile"),
) -> None:
    """Persist a plan (sends nothing). Use --accept-profile any for reuse_only across profiles."""
    if mode not in {"generate", "reuse_only"}:
        raise typer.BadParameter("mode must be generate or reuse_only")
    profiles: tuple[str, ...] | Literal["any"] | None = (
        "any" if accept_profile == ["any"] else tuple(accept_profile) or None
    )
    selection = PlanSelection(
        tuple(entity),
        locator_prefix,
        tuple(kind),
        stale_only,
        "reuse_only" if mode == "reuse_only" else "generate",
        profiles,
    )
    _generation(lambda service: service.plan(project, snapshot, selection))


@app.command("generate")
def generate(
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    plan_id: UUID = typer.Option(..., "--plan-id"),
    plan_digest: str = typer.Option(..., "--plan-digest"),
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    """Execute one persisted plan, bounded by its caps and deadline."""
    if not confirm:
        raise typer.BadParameter("Pass --confirm after reviewing the plan.")

    def run(service: SemanticGenerationService) -> Any:
        value = service.execute(project, snapshot, plan_id, plan_digest, True)
        selected, attempts = service.run(project, snapshot, value.id)
        return {"run": asdict(selected), "attempts": [asdict(item) for item in attempts]}

    _generation(run)


@app.command("metrics")
def metrics(project: UUID = typer.Option(...), snapshot: UUID = typer.Option(...)) -> None:
    """Grounding metrics and hard gates H1/H2 (not quality)."""
    _generation(lambda service: service.metrics(project, snapshot))


@app.command("recover")
def recover(project: UUID = typer.Option(...), snapshot: UUID = typer.Option(...)) -> None:
    """Abandon expired-lease runs; outcome-unknown attempts are recorded, never retried."""
    _generation(lambda service: {"abandoned": service.recover(project, snapshot)})


# ---------------------------------------------------------------- Milestone D: human review


def _write(path: Path, value: Any, text: bool = False) -> None:
    from buildlens.infrastructure.evaluation.local import atomic_write

    atomic_write(path, value, text=text)
    typer.echo(f"wrote {path}")


def _read(path: Path) -> Any:
    from buildlens.infrastructure.evaluation.local import read_json

    return read_json(path)


@app.command("preregister")
def preregister(out: Path = typer.Option(..., "--out")) -> None:
    """Write a draft pre-registration; criteria must be agreed and approved before review."""
    from buildlens.application.semantic_review import preregistration_template

    _write(out, preregistration_template())


@app.command("sample-freeze")
def sample_freeze(
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    preregistration: Path = typer.Option(..., "--preregistration"),
    source: Path = typer.Option(..., "--source", help="JSON describing the evaluated source."),
    out: Path = typer.Option(..., "--out"),
) -> None:
    """Freeze the evaluation sample and disjoint pilot before any generation (write-once)."""
    with _runtime() as runtime:
        sample = semantic_generation_service(runtime).freeze_sample(
            project, snapshot, _read(preregistration), _read(source)
        )
    _write(out, sample)
    _emit({key: sample[key] for key in ("sample_digest", "population")})


@app.command("review-packet")
def review_packet(
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    preregistration: Path = typer.Option(..., "--preregistration"),
    sample: Path = typer.Option(..., "--sample"),
    run: UUID = typer.Option(..., "--run"),
    out: Path = typer.Option(..., "--out"),
) -> None:
    """Create a digest-bound packet for every frozen-sample entity (no replacement)."""
    from buildlens.application.semantic_review import render_markdown, review_template

    with _runtime() as runtime:
        packet = semantic_generation_service(runtime).review_packet(
            project, snapshot, _read(preregistration), _read(sample), run
        )
    _write(out / "packet.json", packet)
    _write(out / "REVIEW.md", render_markdown(packet), text=True)
    _write(out / "reviews.template.json", review_template(packet))


@app.command("review-score")
def review_score(
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    packet: Path = typer.Option(..., "--packet"),
    reviews: Path = typer.Option(..., "--reviews"),
    preregistration: Path = typer.Option(..., "--preregistration"),
    sample: Path = typer.Option(..., "--sample"),
    out: Path = typer.Option(..., "--out"),
) -> None:
    """Score reviews: operational outcomes, quality and hard gates as separate sections.

    Hard gates are measured on the frozen sample's own snapshot; any other is refused."""
    from buildlens.application.semantic_review import score

    frozen = _read(sample)
    if frozen.get("snapshot_id") != str(snapshot):
        raise typer.BadParameter("--snapshot must be the frozen sample's snapshot")
    with _runtime() as runtime:
        metrics = semantic_generation_service(runtime).metrics(project, snapshot)
    result = score(_read(packet), _read(reviews), _read(preregistration), frozen, metrics)
    _write(out, result)
    _emit(result)


@app.command("recognizer-packet")
def recognizer_packet(
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    out: Path = typer.Option(..., "--out"),
) -> None:
    """Recognizer matches and candidate misses for human-approved ground truth."""
    from buildlens.application.recognizers import RECOGNIZERS
    from buildlens.application.semantic_review import OUT_OF_SCOPE

    with _runtime() as runtime:
        packet = semantic_generation_service(runtime).recognizer_packet(project, snapshot)
    _write(out / "recognizer-packet.json", packet)
    _write(
        out / "recognizer-reviews.template.json",
        {
            "packet_digest": packet["packet_digest"],
            "matches": [
                {"entity_key": m["entity_key"], "is_correct": None} for m in packet["matches"]
            ],
            "candidates": [
                {"entity_key": c["entity_key"], "is_missed_match": None, "recognizer": None}
                for c in packet["candidates"]
            ],
            "instructions": "A missed match must name the recognizer that should have matched it "
            f"({', '.join(RECOGNIZERS)}) or '{OUT_OF_SCOPE}'; unattributed misses are refused.",
        },
    )


@app.command("recognizer-score")
def recognizer_score(
    packet: Path = typer.Option(..., "--packet"), reviews: Path = typer.Option(..., "--reviews")
) -> None:
    """Precision (and recall over reviewed candidates) per recognizer."""
    from buildlens.application.semantic_review import recognizer_score as score

    _emit(score(_read(packet), _read(reviews)))
