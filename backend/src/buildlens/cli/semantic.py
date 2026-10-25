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


@app.command("recover")
def recover(project: UUID = typer.Option(...), snapshot: UUID = typer.Option(...)) -> None:
    """Abandon expired-lease runs; outcome-unknown attempts are recorded, never retried."""
    _generation(lambda service: {"abandoned": service.recover(project, snapshot)})
