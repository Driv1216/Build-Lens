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
from buildlens.domain.flow_trace import MAX_DEPTH, MAX_HOPS, TraceQuery
from buildlens.infrastructure.runtime import (
    LocalRuntime,
    flow_evidence_service,
    flow_explanation_service,
    flow_service,
    flow_trace_service,
)

app = typer.Typer(help="Flow tracing: evidence, the flow model and verified FlowTraces.")
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


def _model(call: Callable[[Any], Any]) -> None:
    with _runtime() as runtime:
        _emit(call(flow_service(runtime)))


@app.command("build")
def build(project: UUID = typer.Option(...), snapshot: UUID = typer.Option(...)) -> None:
    """Build (or reuse) the flow model from current evidence, catalog and architecture runs."""
    _model(lambda service: service.build(project, snapshot))


@app.command("status")
def model_status(project: UUID = typer.Option(...), snapshot: UUID = typer.Option(...)) -> None:
    """The flow model's latest attempt, active run and freshness."""
    _model(lambda service: service.status(project, snapshot))


@app.command("node")
def node(
    key: str = typer.Argument(..., help="A flow node key, e.g. scope:app/api.py#m.0"),
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    run: UUID | None = typer.Option(None, "--run"),
) -> None:
    """A verified node with its incoming and outgoing edges."""
    _model(lambda service: service.node(project, snapshot, run, key))


@app.command("integrity")
def integrity(
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    run: UUID | None = typer.Option(None, "--run"),
) -> None:
    """Full inspection: every element, evidence item and cited row, plus the graph digest."""
    _model(lambda service: service.integrity(project, snapshot, run))


def _traces(call: Callable[[Any], Any]) -> None:
    with _runtime() as runtime:
        _emit(call(flow_trace_service(runtime)))


def _trace_query(entry: str, direction: str, max_depth: int, max_hops: int) -> TraceQuery:
    if direction not in {"forward", "backward"}:
        raise typer.BadParameter("direction must be forward or backward")
    return TraceQuery(entry, direction, max_depth, max_hops)  # type: ignore[arg-type]


@app.command("entries")
def entries(
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    query: str | None = typer.Option(None, "--q", help="Search text; candidates only"),
    kind: str | None = typer.Option(None, "--kind"),
    mode: str = typer.Option("lexical", "--mode"),
    run: UUID | None = typer.Option(None, "--run"),
) -> None:
    """Entry points (or ranked candidates for a search; the user chooses)."""
    _traces(lambda service: service.entries(project, snapshot, run, kind, query, mode))


@app.command("trace")
def trace(
    entry: str = typer.Argument(..., help="An entry-point or node key"),
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    direction: str = typer.Option("forward", "--direction"),
    max_depth: int = typer.Option(MAX_DEPTH, "--max-depth", min=1, max=64),
    max_hops: int = typer.Option(MAX_HOPS, "--max-hops", min=1, max=10_000),
    run: UUID | None = typer.Option(None, "--run"),
) -> None:
    """A verified FlowTrace with its four verdicts per step (never an execution claim)."""
    query = _trace_query(entry, direction, max_depth, max_hops)
    _traces(lambda service: service.trace(project, snapshot, run, query))


@app.command("path")
def path(
    source: str = typer.Argument(...),
    target: str = typer.Argument(...),
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    run: UUID | None = typer.Option(None, "--run"),
) -> None:
    """One shortest structural witness between two nodes (L3; other paths may exist)."""
    _traces(lambda service: service.path(project, snapshot, run, source, target))


@app.command("pin")
def pin(
    entry: str = typer.Argument(...),
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    direction: str = typer.Option("forward", "--direction"),
    run: UUID | None = typer.Option(None, "--run"),
) -> None:
    """Compute, verify and pin a trace (D4)."""
    query = _trace_query(entry, direction, MAX_DEPTH, MAX_HOPS)
    _traces(lambda service: service.pin(project, snapshot, run, query))


@app.command("verify")
def verify(
    digest: str = typer.Argument(..., help="A pinned trace digest"),
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
) -> None:
    """Re-verify a pinned trace: current, stale (with reasons) or integrity_failed."""
    _traces(lambda service: service.pinned(project, snapshot, digest))


explain_app = typer.Typer(
    help="Plan-gated explanations of pinned traces (validated; never evidence)."
)
app.add_typer(explain_app, name="explain")


def _explanations(call: Callable[[Any], Any]) -> None:
    with _runtime() as runtime:
        _emit(call(flow_explanation_service(runtime)))


@explain_app.command("plan")
def explain_plan(
    digest: str = typer.Argument(..., help="A pinned trace digest"),
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
) -> None:
    """Plan an explanation: the exact outbound bytes, provider and retention note. Sends nothing."""
    _explanations(lambda service: service.plan(project, snapshot, digest))


@explain_app.command("run")
def explain_run(
    plan: UUID = typer.Argument(...),
    plan_digest: str = typer.Option(..., "--plan-digest"),
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    confirm: bool = typer.Option(False, "--confirm", help="Required: send exactly the plan"),
) -> None:
    """Confirm a plan and send its single request through the outbound gate."""
    if not confirm:
        raise typer.BadParameter("Pass --confirm to send exactly the planned request.")
    _explanations(lambda service: service.execute(project, snapshot, plan, plan_digest, True))


@explain_app.command("show")
def explain_show(
    run: UUID = typer.Argument(...),
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
) -> None:
    """An explanation run with its validated claims and rejection codes."""
    _explanations(lambda service: service.run(project, snapshot, run))
