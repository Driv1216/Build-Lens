"""Stage 12 laboratory HTTP routes (local only; existing host/origin/JSON guards apply).

Index builds and probes are synchronous and deadline-bounded (like embedding preparation).
Long benchmarks run from the CLI; their retained results are served read-only here.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from buildlens.application.lab import LabService
from buildlens.domain.lab import LabError, parse_config
from buildlens.infrastructure.lab.service import build_lab_service, catalog
from buildlens.infrastructure.lab.store import LabStore

router = APIRouter(prefix="/api/v1/projects/{project_id}/snapshots/{snapshot_id}/lab", tags=["lab"])
runtime_router = APIRouter(prefix="/api/v1/lab", tags=["lab"])
MAX_TEXT_RETURN = 8 * 1024
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,160}$")
RUN_FILES = {
    "manifest.json",
    "scores.json",
    "summary.md",
    "parity.json",
    "loader-diff.json",
    "engineering.json",
    "ld2.json",
    "stores.json",
}


def get_lab_service(request: Request) -> LabService:
    return build_lab_service(request.app.state)


def _store(service: LabService) -> LabStore:
    store = service.store
    assert isinstance(store, LabStore)
    return store


class ConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: dict[str, Any]


class ProbeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    configs: list[dict[str, Any]] = Field(min_length=1, max_length=3)
    question: str
    mode: Literal["sequential"] = "sequential"

    @field_validator("question")
    @classmethod
    def bounded(cls, value: str) -> str:
        if not value.strip() or len(value.encode()) > 4096:
            raise ValueError("question must be non-blank and at most 4096 UTF-8 bytes")
        return value


class LoaderDiffBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seed: int = Field(12, ge=0, le=1_000_000)


def _parse(raw: dict[str, Any]) -> Any:
    raw = dict(raw)
    raw.pop("digest", None)
    return parse_config(raw)


def _trim(result: dict[str, Any]) -> dict[str, Any]:
    """Return item texts only for delivered context and the top ranked items (bounded)."""
    keep = {i["text_sha256"] for i in result.get("context", [])}
    keep |= {i["text_sha256"] for i in result.get("ranked", [])[:10]}
    texts = {k: v[:MAX_TEXT_RETURN] for k, v in (result.get("texts") or {}).items() if k in keep}
    return {**result, "texts": texts}


@runtime_router.get("/status")
def status(request: Request, service: LabService = Depends(get_lab_service)) -> dict[str, Any]:
    settings = request.app.state.settings
    return catalog(
        _store(service),
        lab_db_configured=settings.lab_database_url is not None,
        generation_enabled=settings.lab_generation_enabled,
        embedding=request.app.state.embedding_runtime,
        reranker=request.app.state.reranker_runtime,
    )


@runtime_router.get("/experiments")
def experiments(service: LabService = Depends(get_lab_service)) -> dict[str, Any]:
    return {"experiments": _store(service).experiments()}


@runtime_router.post("/experiments")
def save_experiment(
    body: ConfigBody, service: LabService = Depends(get_lab_service)
) -> dict[str, Any]:
    config = _parse(body.config)
    _store(service).save_experiment(config)
    return {**config.as_dict(), "digest": config.digest}


@runtime_router.post("/validate")
def validate(body: ConfigBody) -> dict[str, Any]:
    config = _parse(body.config)
    return {
        **config.as_dict(),
        "digest": config.digest,
        "ingestion_identity": config.ingestion_identity(),
    }


@runtime_router.get("/indexes")
def indexes(service: LabService = Depends(get_lab_service)) -> dict[str, Any]:
    return {"indexes": _store(service).indexes()}


@runtime_router.get("/indexes/{index_id}")
def index(index_id: str, service: LabService = Depends(get_lab_service)) -> dict[str, Any]:
    manifest = _store(service).read_index(index_id, "index.json")
    return {k: v for k, v in manifest.items() if k != "steps"} | {
        "steps": manifest.get("steps", [])[:300]
    }


@runtime_router.get("/indexes/{index_id}/boundaries")
def boundaries(
    index_id: str, locator: str, service: LabService = Depends(get_lab_service)
) -> dict[str, Any]:
    store = _store(service)
    spans = store.read_index(index_id, "node-spans.json")
    sources = {}
    try:
        sources = store.read_index(index_id, "sources.json")
    except LabError:
        pass
    manifest = store.read_index(index_id, "index.json")
    by_locator = spans.get("by_locator", {})
    text = None
    artifact = spans.get("artifacts", {}).get(locator)
    if artifact is not None:
        text = sources.get(artifact)
    return {
        "index_id": index_id,
        "pipeline": manifest.get("pipeline"),
        "locator": locator,
        "framework_spans": by_locator.get(locator, []),
        "native_spans": spans.get("native", {}).get(locator, []),
        "text": text[:200_000] if text else None,
    }


@router.post("/indexes")
async def build(
    project_id: UUID,
    snapshot_id: UUID,
    body: ConfigBody,
    service: LabService = Depends(get_lab_service),
) -> dict[str, Any]:
    config = _parse(body.config)
    result = await run_in_threadpool(service.build, config, project_id, snapshot_id)
    return {k: v for k, v in result.items() if k != "steps"} | {
        "steps": result.get("steps", [])[:300]
    }


@router.post("/probe")
async def probe(
    project_id: UUID,
    snapshot_id: UUID,
    body: ProbeBody,
    response: Response,
    service: LabService = Depends(get_lab_service),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    configs = [_parse(c) for c in body.configs]
    results: list[dict[str, Any]] = []
    for config in configs:
        try:
            result = await run_in_threadpool(
                service.query, config, project_id, snapshot_id, body.question
            )
            results.append({"ok": True, "result": _trim(result)})
        except LabError as error:
            results.append(
                {
                    "ok": False,
                    "pipeline": config.pipeline,
                    "error": {"code": error.code, "message": error.message},
                }
            )
    ranked = [
        [i.get("chunk_id") or i["ref"] for i in r["result"]["ranked"]] if r["ok"] else []
        for r in results
    ]
    overlap = {}
    for a in range(len(ranked)):
        for b in range(a + 1, len(ranked)):
            overlap[f"{a}-{b}"] = {
                str(k): len(set(ranked[a][:k]) & set(ranked[b][:k])) for k in (1, 5, 10, 32)
            }
    return {"question": body.question, "results": results, "overlap": overlap}


@runtime_router.get("/runs")
def runs(service: LabService = Depends(get_lab_service)) -> dict[str, Any]:
    store = _store(service)
    listing = []
    for path in sorted(store.area("runs").iterdir(), reverse=True):
        if path.is_dir():
            files = sorted(p.name for p in path.iterdir() if p.is_file() and p.name in RUN_FILES)
            listing.append({"run_id": path.name, "files": files})
    return {"runs": listing}


@runtime_router.get("/runs/{run_id}/files/{name}")
def run_file(run_id: str, name: str, service: LabService = Depends(get_lab_service)) -> Any:
    if name not in RUN_FILES:
        raise LabError("LAB_RUN_FILE_NOT_ALLOWED")
    store = _store(service)
    path = store.inside(store.run_dir(run_id) / name, "runs")
    if not path.is_file():
        raise LabError("LAB_RUN_NOT_FOUND")
    if name.endswith(".md"):
        return {"markdown": path.read_text()[:500_000]}
    return store.read_run(run_id, name)


@runtime_router.get("/runs/{run_id}/cases")
def run_cases(run_id: str, service: LabService = Depends(get_lab_service)) -> dict[str, Any]:
    store = _store(service)
    directory = store.inside(store.run_dir(run_id) / "cases", "runs")
    if not directory.is_dir():
        raise LabError("LAB_RUN_NOT_FOUND")
    return {"cases": sorted(p.stem for p in directory.glob("*.json"))}


@runtime_router.get("/runs/{run_id}/cases/{name}")
def run_case(run_id: str, name: str, service: LabService = Depends(get_lab_service)) -> Any:
    if not _SAFE.match(name):
        raise LabError("LAB_RUN_FILE_NOT_ALLOWED")
    return _store(service).read_run(run_id, f"cases/{name}.json")


@runtime_router.post("/loader-diff")
async def loader_diff(
    body: LoaderDiffBody, request: Request, service: LabService = Depends(get_lab_service)
) -> dict[str, Any]:
    from buildlens.infrastructure.lab.loader_lab import loader_diff as run
    from buildlens.infrastructure.lab.synthetic import create_sandbox

    store = _store(service)
    sandbox = create_sandbox(store.sandbox_base, body.seed)
    report: dict[str, Any] = await run_in_threadpool(
        run, request.app.state.lab_workers, sandbox, str(store.sandbox_base)
    )
    return report
