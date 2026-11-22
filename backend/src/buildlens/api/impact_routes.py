"""HTTP adapter for the local Stage 16 workbench."""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from threading import Event
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from buildlens.application.impact import (
    INTERACTIVE_SECONDS,
    PREPARATION_SECONDS,
    AnalysisRequest,
    Deadline,
    ImpactError,
    PrepareRequest,
    ProposalRequest,
    SnapshotSelection,
)
from buildlens.infrastructure.runtime import impact_service

router = APIRouter(prefix="/api/v1/projects/{project_id}/impact", tags=["impact"])


def _http(error: ImpactError) -> HTTPException:
    return HTTPException(
        error.status,
        {
            "code": error.code,
            "message": _MESSAGES.get(
                error.code,
                "The impact operation could not be completed.",
            ),
        },
    )


_MESSAGES = {
    "IMPACT_BUSY": "An impact operation is already using this slot.",
    "IMPACT_FLOW_AUTHORITY_UNAVAILABLE": "A coherent flow model is unavailable for this snapshot.",
    "IMPACT_PREPARATION_REQUIRED": "Prepare the selected source artifact first.",
    "IMPACT_HYPOTHESIS_UNSUPPORTED": "The hypothesis cannot be applied to the prepared facts.",
    "IMPACT_REPORT_NOT_FOUND": "The saved impact report is unavailable.",
}


async def _run(
    request: Request,
    seconds: float,
    call: Callable[..., dict[str, Any]],
    model: type[BaseModel] | None = None,
    max_bytes: int = 8192,
) -> dict[str, Any]:
    canceled = Event()
    deadline = Deadline(seconds, canceled)
    payload = None

    async def watch() -> None:
        while not canceled.is_set():
            if await request.is_disconnected():
                canceled.set()
                return
            await asyncio.sleep(0.1)

    try:
        if model is not None:
            data = bytearray()
            try:
                async with asyncio.timeout(deadline.remaining()):
                    async for part in request.stream():
                        if len(data) + len(part) > max_bytes:
                            raise ImpactError("IMPACT_REQUEST_LIMIT", 413)
                        data.extend(part)
                payload = model.model_validate_json(data)
            except ValidationError:
                raise ImpactError("IMPACT_INVALID_REQUEST", 422) from None
            except TimeoutError:
                raise ImpactError("IMPACT_DEADLINE", 408) from None
        task = asyncio.create_task(watch())
        try:
            if model is not None:
                return cast(dict[str, Any], await run_in_threadpool(call, payload, deadline))
            return cast(dict[str, Any], await run_in_threadpool(call, deadline))
        finally:
            canceled.set()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    except ImpactError as error:
        raise _http(error) from None


@router.post("/preparations")
async def prepare(request: Request, project_id: UUID) -> dict[str, Any]:
    service = impact_service(request.app.state)
    return await _run(
        request,
        PREPARATION_SECONDS,
        lambda payload, deadline: service.prepare(project_id, payload, deadline),
        PrepareRequest,
        96 * 1024,
    )


@router.get("/preparations/{snapshot_id}")
def coverage(
    request: Request,
    project_id: UUID,
    snapshot_id: UUID,
    locator: str | None = None,
    flow_run_id: UUID | None = None,
    after_snapshot_id: UUID | None = None,
) -> dict[str, Any]:
    try:
        return impact_service(request.app.state).coverage(
            project_id,
            SnapshotSelection(snapshot_id=snapshot_id, flow_run_id=flow_run_id),
            locator,
            SnapshotSelection(snapshot_id=after_snapshot_id) if after_snapshot_id else None,
        )
    except ImpactError as error:
        raise _http(error) from None


@router.post("/proposals/interpret")
async def interpret(request: Request, project_id: UUID) -> dict[str, Any]:
    service = impact_service(request.app.state)
    return await _run(
        request,
        INTERACTIVE_SECONDS,
        lambda payload, deadline: service.interpret(project_id, payload, deadline),
        ProposalRequest,
        16 * 1024,
    )


@router.post("/analyses")
async def analyze(request: Request, project_id: UUID) -> dict[str, Any]:
    service = impact_service(request.app.state)
    return await _run(
        request,
        INTERACTIVE_SECONDS,
        lambda payload, deadline: service.analyze(project_id, payload, deadline),
        AnalysisRequest,
        12 * 1024 * 1024,
    )


@router.get("/analyses")
def history(request: Request, project_id: UUID) -> dict[str, Any]:
    try:
        return {"items": impact_service(request.app.state).history(project_id)}
    except ImpactError as error:
        raise _http(error) from None


@router.get("/analyses/{result_id}")
async def show(
    request: Request,
    project_id: UUID,
    result_id: str = Path(pattern="^[a-f0-9]{64}$"),
    verify: bool = False,
) -> dict[str, Any]:
    service = impact_service(request.app.state)
    return await _run(
        request,
        INTERACTIVE_SECONDS,
        lambda deadline: service.show(project_id, result_id, verify=verify, deadline=deadline),
    )
