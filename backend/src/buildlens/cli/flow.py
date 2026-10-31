"""Stage 15 flow evidence commands (same service and authority rules as the HTTP API)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from typing import Any
from uuid import UUID

import typer

from buildlens.application.flow_evidence import FlowEvidenceService
from buildlens.core.config import get_settings
from buildlens.infrastructure.runtime import LocalRuntime, flow_evidence_service

app = typer.Typer(help="Flow tracing evidence: per-scope CFGs, callsites and bindings.")
evidence_app = typer.Typer(help="The flow evidence run (the only Stage 15 source read).")
app.add_typer(evidence_app, name="evidence")


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


def _service(call: Callable[[FlowEvidenceService], Any]) -> None:
    with _runtime() as runtime:
        _emit(call(flow_evidence_service(runtime)))


@evidence_app.command("run")
def run(project: UUID = typer.Option(...), snapshot: UUID = typer.Option(...)) -> None:
    """Extract (or reuse) flow evidence for the snapshot's active structural analysis."""
    _service(lambda service: service.extract(project, snapshot))


@evidence_app.command("status")
def status(project: UUID = typer.Option(...), snapshot: UUID = typer.Option(...)) -> None:
    """Latest attempt, the active run and its freshness."""
    _service(lambda service: service.status(project, snapshot))


@evidence_app.command("coverage")
def coverage(
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    state: str | None = typer.Option(None),
    limit: int = typer.Option(100),
    offset: int = typer.Option(0),
    run: UUID | None = typer.Option(None, "--run"),
) -> None:
    """Per-artifact flow coverage (extracted, partial, failed, not_current, ...)."""

    def call(service: FlowEvidenceService) -> Any:
        value, reasons, rows, total = service.coverage(project, snapshot, run, state, limit, offset)
        return {"run_id": value.id, "freshness_reasons": reasons, "total": total, "items": rows}

    _service(call)


@evidence_app.command("facts")
def facts(
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    run: UUID | None = typer.Option(None, "--run"),
) -> None:
    """Framework versions read from lockfiles and manifests (as data)."""

    def call(service: FlowEvidenceService) -> Any:
        value, reasons, items = service.framework_facts(project, snapshot, run)
        return {"run_id": value.id, "freshness_reasons": reasons, "items": items}

    _service(call)
