"""CLI adapter for Stage 16, using the same service and request contracts as HTTP."""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import typer
from pydantic import ValidationError

from buildlens.application.impact import (
    AnalysisRequest,
    ImpactError,
    ImpactService,
    PrepareRequest,
    ProposalRequest,
    SnapshotSelection,
)
from buildlens.core.config import get_settings
from buildlens.infrastructure.runtime import LocalRuntime, impact_service

app = typer.Typer(help="Inspect, compare and retain evidence-backed impact findings.")


def _run(call: Callable[[ImpactService], Any]) -> None:
    runtime = LocalRuntime(get_settings())
    try:
        result = call(impact_service(runtime))
        typer.echo(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
    except ImpactError as error:
        typer.echo(error.code, err=True)
        raise typer.Exit(1) from None
    except (ValidationError, OSError, ValueError):
        typer.echo("IMPACT_INVALID_REQUEST", err=True)
        raise typer.Exit(2) from None
    finally:
        asyncio.run(runtime.close())


@app.command()
def prepare(
    project: UUID,
    snapshot: UUID,
    locator: list[str] = typer.Option(default=[]),
    seal_flow: bool = False,
) -> None:
    """Prepare named artifacts and optionally seal an existing flow model."""
    _run(
        lambda service: service.prepare(
            project,
            PrepareRequest(
                selection=SnapshotSelection(snapshot_id=snapshot),
                locators=locator,
                seal_flow=seal_flow,
            ),
        )
    )


@app.command()
def interpret(project: UUID, snapshot: UUID, proposal_file: Path) -> None:
    """Propose structured edits from a local text file."""

    def action(service: ImpactService) -> Any:
        with proposal_file.open(encoding="utf-8") as handle:
            return service.interpret(
                project,
                ProposalRequest(
                    selection=SnapshotSelection(snapshot_id=snapshot),
                    text=handle.read(2049),
                ),
            )

    _run(action)


@app.command()
def analyze(project: UUID, request: Path) -> None:
    """Analyze the mode and authority described by a JSON request file."""

    def action(service: ImpactService) -> Any:
        with request.open("rb") as handle:
            data = handle.read(12 * 1024 * 1024 + 1)
        if len(data) > 12 * 1024 * 1024:
            raise ImpactError("IMPACT_REQUEST_LIMIT", 413)
        return service.analyze(project, AnalysisRequest.model_validate_json(data))

    _run(action)


@app.command()
def show(project: UUID, result: str) -> None:
    """Show a saved report by its result digest."""
    _run(lambda service: service.show(project, result))


@app.command()
def verify(project: UUID, result: str) -> None:
    """Recheck a saved report and its retained evidence."""
    _run(lambda service: service.show(project, result, verify=True))
