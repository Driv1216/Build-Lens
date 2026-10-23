"""`buildlens lab` — Stage 12 framework laboratory commands (explicit, local, offline)."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import secrets
import statistics
import subprocess
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import typer

from buildlens.application.lab_evaluation import LabBenchmarkRunner
from buildlens.cli.evaluation import implementation_identity
from buildlens.core.config import get_settings
from buildlens.domain.lab import PIPELINES, LabError, parse_config
from buildlens.infrastructure.evaluation.local import (
    atomic_write,
    boundary_path,
    load_dataset,
    read_json,
)
from buildlens.infrastructure.lab.runs import LabRunWriter, report
from buildlens.infrastructure.lab.service import ISOLATED_ENVS, build_lab_service, catalog
from buildlens.infrastructure.lab.store import LabStore
from buildlens.infrastructure.runtime import LocalRuntime

app = typer.Typer(help="Stage 12 LangChain / LlamaIndex laboratory (offline by default).")
env_app = typer.Typer(help="Isolated framework environments.")
db_app = typer.Typer(help="Separate least-privilege laboratory database.")
app.add_typer(env_app, name="env")
app.add_typer(db_app, name="db")
BACKEND = Path(__file__).resolve().parents[3]
REPO = BACKEND.parent


def _emit(value: Any) -> None:
    typer.echo(json.dumps(value, indent=2, sort_keys=True, default=str))


def _config(experiment: str) -> Any:
    path = Path(experiment)
    if path.suffix == ".json" and path.is_file():
        raw = read_json(path)
        raw.pop("digest", None)
        return parse_config(raw)
    runtime = LocalRuntime(get_settings())
    try:
        return build_lab_service(runtime).store.experiment(experiment)  # type: ignore[attr-defined]
    finally:
        asyncio.run(runtime.close())


def _fail(error: LabError) -> None:
    typer.echo(
        f"error: {error.code} {error.message if error.message != error.code else ''}", err=True
    )
    raise typer.Exit(code=1)


@app.command("status")
def status() -> None:
    """Installed frameworks, isolated environments, models and pipeline availability."""
    settings = get_settings()
    runtime = LocalRuntime(settings)
    try:
        service = build_lab_service(runtime)
        _emit(
            catalog(
                service.store,  # type: ignore[arg-type]
                lab_db_configured=settings.lab_database_url is not None,
                generation_enabled=settings.lab_generation_enabled,
                embedding=runtime.embedding_runtime,
                reranker=runtime.reranker_runtime,
            )
        )
    finally:
        asyncio.run(runtime.close())


@app.command("save")
def save(config_file: Path) -> None:
    """Validate a configuration and store it under .buildlens-eval/lab/experiments/."""
    raw = read_json(config_file)
    raw.pop("digest", None)
    try:
        config = parse_config(raw)
    except LabError as error:
        _fail(error)
    runtime = LocalRuntime(get_settings())
    try:
        path = build_lab_service(runtime).store.save_experiment(config)  # type: ignore[attr-defined]
        typer.echo(f"{config.name} {config.digest} -> {path}")
    finally:
        asyncio.run(runtime.close())


@app.command("build")
def build(
    experiment: str, project: UUID = typer.Option(...), snapshot: UUID = typer.Option(...)
) -> None:
    config = _config(experiment)
    runtime = LocalRuntime(get_settings())
    try:
        result = build_lab_service(runtime).build(config, project, snapshot)
        _emit({k: v for k, v in result.items() if k not in {"steps", "config"}})
    except LabError as error:
        _fail(error)
    finally:
        asyncio.run(runtime.close())


@app.command("probe")
def probe(
    experiment: list[str] = typer.Option(..., "--experiment"),
    question: str = typer.Option(...),
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
) -> None:
    configs = [_config(e) for e in experiment]
    runtime = LocalRuntime(get_settings())
    try:
        service = build_lab_service(runtime)
        for config in configs:
            result = service.query(config, project, snapshot, question)
            _emit(
                {
                    "pipeline": config.name,
                    "top": [
                        (i["rank"], i.get("locator"), i.get("span"), i.get("score"))
                        for i in result["ranked"][:8]
                    ],
                    "context_refs": [i["ref"] for i in result["context"]],
                    "timings": result["timings"],
                    "verification": result["verification"],
                    "embedding": result.get("embedding"),
                }
            )
    except LabError as error:
        _fail(error)
    finally:
        asyncio.run(runtime.close())


@app.command("bench")
def bench(
    dataset: Path = typer.Option(...),
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    experiment: list[str] = typer.Option(..., "--experiment"),
    run_id: str = typer.Option(..., "--run-id"),
    baseline: str | None = typer.Option(None),
    case: list[str] | None = typer.Option(None, "--case"),
    repetitions: int = typer.Option(1, min=1, max=10),
) -> None:
    """Context-only benchmark of saved experiments against a reviewed Stage 11 dataset."""
    configs = [_config(e) for e in experiment]
    runtime = LocalRuntime(get_settings())
    try:
        service = build_lab_service(runtime)
        definition = load_dataset(boundary_path(dataset, REPO, "datasets"))
        identity = implementation_identity()
        identity["runtime"] = {
            "python": platform.python_version(),
            "platform": platform.system(),
            "machine": platform.machine(),
        }
        identity["embedding_contract"] = service.corpus_session(
            project, snapshot
        ).corpus.embedding.contract.digest
        directory = service.store.run_dir(run_id)  # type: ignore[attr-defined]
        runner = LabBenchmarkRunner(
            service, LabRunWriter(directory), progress=lambda m: typer.echo(m, err=True)
        )
        manifest = runner.run(
            definition,
            configs,
            project,
            snapshot,
            implementation=identity,
            selected_cases=case,
            repetitions=repetitions,
        )
        report(directory, baseline=baseline)
        typer.echo(
            f"run {run_id}: {manifest['status']} {manifest['counts']} -> {directory / 'summary.md'}"
        )
        if manifest["counts"]["error"] or manifest["status"] != "completed":
            raise typer.Exit(code=1)
    except LabError as error:
        _fail(error)
    finally:
        asyncio.run(runtime.close())


@app.command("report")
def report_command(run: Path, baseline: str | None = typer.Option(None)) -> None:
    """Recompute scores from retained per-case records (no database, no workers)."""
    try:
        scores = report(run, baseline=baseline)
    except LabError as error:
        _fail(error)
    typer.echo(f"{run / 'summary.md'} scores_digest={scores['scores_digest']}")


@app.command("parity")
def parity(
    stage11_run: Path = typer.Option(..., "--stage11-run"),
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    output: Path = typer.Option(...),
) -> None:
    """Gate: lab-native must reproduce the retained Stage 11 retrieval and ranking orders."""
    variants = {
        "vector": ("semantic", False),
        "hybrid": ("hybrid", False),
        "hybrid_reranked": ("hybrid", True),
    }
    manifest = read_json(stage11_run / "manifest.json")
    runtime = LocalRuntime(get_settings())
    rows: list[dict[str, Any]] = []
    try:
        service = build_lab_service(runtime)
        session = service.verify_corpus(project, snapshot)
        dataset = read_json(stage11_run / "dataset.json")
        questions = {c["id"]: c["question"] for c in dataset["cases"]}
        for path in sorted((stage11_run / "cases").glob("*.json")):
            case_id, variant = path.stem.rsplit(".", 1)
            record = read_json(path)
            mode, rerank = variants[variant]
            config = parse_config(
                {
                    "name": f"native-{variant}",
                    "pipeline": "native.context",
                    "params": {"mode": mode, "rerank": rerank},
                }
            )
            result = service.query(config, project, snapshot, questions[case_id])
            expected_retrieval = [
                r["chunk_id"] for r in record["observations"].get("retrieval") or []
            ]
            expected_ranking = [r["chunk_id"] for r in record["observations"].get("ranking") or []]
            expected_context = [r["chunk_id"] for r in record["observations"].get("context") or []]
            got_retrieval = [i["chunk_id"] for i in result["retrieved"]]
            got_ranking = [i["chunk_id"] for i in result["ranked"]]
            got_context = [i["chunk_id"] for i in result["context"]]
            rows.append(
                {
                    "case": case_id,
                    "variant": variant,
                    "retrieval_equal": got_retrieval == expected_retrieval,
                    "ranking_equal": got_ranking == expected_ranking,
                    "context_equal": got_context == expected_context,
                    "lengths": [
                        len(expected_retrieval),
                        len(got_retrieval),
                        len(expected_ranking),
                        len(got_ranking),
                    ],
                }
            )
            typer.echo(
                f"{case_id}.{variant}: "
                + " ".join(
                    str(rows[-1][k]) for k in ("retrieval_equal", "ranking_equal", "context_equal")
                ),
                err=True,
            )
        result_doc = {
            "version": "stage12.parity.v1",
            "stage11_run_id": manifest.get("id"),
            "corpus_digest": session.digest,
            "stage11_corpus_digest": manifest.get("corpus_digest"),
            "pairs": len(rows),
            "retrieval_equal": sum(r["retrieval_equal"] for r in rows),
            "ranking_equal": sum(r["ranking_equal"] for r in rows),
            "context_equal": sum(r["context_equal"] for r in rows),
            "rows": rows,
            "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        atomic_write(output, result_doc, replace=True)
        typer.echo(json.dumps({k: v for k, v in result_doc.items() if k != "rows"}))
        if result_doc["ranking_equal"] != len(rows) or result_doc["retrieval_equal"] != len(rows):
            raise typer.Exit(code=1)
    except LabError as error:
        _fail(error)
    finally:
        asyncio.run(runtime.close())


@app.command("loader-diff")
def loader_diff_command(output: Path = typer.Option(...), seed: int = typer.Option(12)) -> None:
    """LD-1: framework file loaders vs BuildLens boundary on a generated sandbox."""
    from buildlens.infrastructure.lab.loader_lab import loader_diff
    from buildlens.infrastructure.lab.synthetic import create_sandbox

    runtime = LocalRuntime(get_settings())
    try:
        service = build_lab_service(runtime)
        store = service.store
        assert isinstance(store, LabStore)
        sandbox = create_sandbox(store.sandbox_base, seed)
        result = loader_diff(runtime.lab_workers, sandbox, str(store.sandbox_base))
        atomic_write(output, result, replace=True)
        _emit(result["summary"])
    finally:
        asyncio.run(runtime.close())


# ------------------------------------------------------------------ engineering metrics

SOURCE_GROUPS = {
    "native (lab adapter)": ["infrastructure/lab/native.py"],
    "langchain worker": ["infrastructure/lab/workers/langchain"],
    "llamaindex worker": ["infrastructure/lab/workers/llamaindex"],
    "shared worker runtime": ["infrastructure/lab/workers/common"],
    "parent orchestration": [
        "application/lab.py",
        "infrastructure/lab/bridge.py",
        "infrastructure/lab/corpus.py",
        "infrastructure/lab/supervisor.py",
        "infrastructure/lab/service.py",
    ],
    "isolated langchain-postgres worker": ["../../lab_envs/langchain_postgres/src"],
}
IMPORTS = {
    "langchain-core": "langchain_core.runnables",
    "langchain (create_agent)": "langchain.agents",
    "langchain-classic retrievers": "langchain_classic.retrievers",
    "llama-index-core": "llama_index.core",
    "llama-index bm25": "llama_index.retrievers.bm25",
}


def _sloc(path: Path) -> int:
    files = [path] if path.is_file() else sorted(path.rglob("*.py"))
    total = 0
    for file in files:
        for line in file.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                total += 1
    return total


def _closure(lock: dict[str, Any], roots: list[str]) -> set[str]:
    packages = {p["name"]: p for p in lock["package"]}
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        name = stack.pop()
        if name in seen or name not in packages:
            continue
        seen.add(name)
        for dep in packages[name].get("dependencies", []):
            stack.append(dep["name"])
    return seen


@app.command("engineering")
def engineering(
    output: Path = typer.Option(...), repeats: int = typer.Option(3, min=1, max=10)
) -> None:
    """Measured engineering metrics: SLOC, dependency closure, cold import time, spawn time."""
    source = BACKEND / "src" / "buildlens"
    sloc = {
        name: sum(_sloc((source / p).resolve()) for p in paths if (source / p).resolve().exists())
        for name, paths in SOURCE_GROUPS.items()
    }
    lock = tomllib.loads((BACKEND / "uv.lock").read_text())
    project = next(p for p in lock["package"] if p["name"] == "buildlens")
    extras = project.get("optional-dependencies", {})
    base = _closure(lock, [d["name"] for d in project.get("dependencies", [])])
    closure = {}
    for extra in (
        "lab-langchain",
        "lab-llamaindex",
        "lab-providers",
        "lab-pgvector",
        "lab-code",
        "embeddings",
        "generation",
    ):
        packages = _closure(lock, [d["name"] for d in extras.get(extra, [])])
        closure[extra] = {"packages": len(packages), "beyond_base": len(packages - base)}
    imports: dict[str, Any] = {}
    for label, module in IMPORTS.items():
        samples = []
        for _ in range(repeats):
            started = time.perf_counter()
            done = subprocess.run(
                [sys.executable, "-s", "-c", f"import {module}"],
                capture_output=True,
                timeout=120,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            samples.append((time.perf_counter() - started) * 1000)
            if done.returncode != 0:
                samples = []
                break
        imports[label] = (
            {
                "median_ms": round(statistics.median(samples), 1),
                "samples_ms": [round(s, 1) for s in samples],
            }
            if samples
            else {"error": "import failed"}
        )
    baseline = []
    for _ in range(repeats):
        started = time.perf_counter()
        subprocess.run([sys.executable, "-s", "-c", "pass"], capture_output=True, timeout=60)
        baseline.append((time.perf_counter() - started) * 1000)
    result = {
        "version": "stage12.engineering.v1",
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "sloc_non_blank_non_comment": sloc,
        "dependency_closure": {"base_packages": len(base), "extras": closure},
        "cold_import_ms": imports,
        "interpreter_startup_ms": round(statistics.median(baseline), 1),
        "note": "Cold import times include interpreter start-up (see interpreter_startup_ms).",
    }
    atomic_write(output, result, replace=True)
    _emit(result)


# ------------------------------------------------------------------ isolated envs and lab DB


@env_app.command("sync")
def env_sync(name: str) -> None:
    """Explicitly create an isolated framework environment from its frozen lock."""
    if name != "langchain-postgres":
        typer.echo("unknown environment", err=True)
        raise typer.Exit(code=2)
    directory = ISOLATED_ENVS / "langchain_postgres"
    command = ["uv", "sync", "--frozen", "--project", str(directory)]
    typer.echo("running: " + " ".join(command), err=True)
    raise typer.Exit(
        code=subprocess.run(
            command, env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(directory / ".venv")}
        ).returncode
    )


@db_app.command("provision")
def provision(
    admin_url: str = typer.Option(
        ..., envvar="BUILDLENS_LAB_ADMIN_URL", help="Superuser URL of the SEPARATE lab server"
    ),
    database: str = typer.Option("buildlens_lab"),
    role: str = typer.Option("buildlens_lab_app"),
    credentials: Path = typer.Option(..., help="Where to write the 0600 env file with the lab URL"),
) -> None:
    """Create a separate lab database and a least-privilege role (never the production DB)."""
    from buildlens.infrastructure.lab.database import provision_lab_database

    settings = get_settings()
    url = provision_lab_database(
        admin_url, database, role, secrets.token_urlsafe(24), production_url=settings.database_url
    )
    credentials.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(credentials, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(f"BUILDLENS_LAB_DATABASE_URL={url}\n")
    typer.echo(
        f"lab database {database} with role {role}; credentials written to {credentials} (0600)"
    )


def pipelines_table() -> list[dict[str, Any]]:
    return [{"id": s.id, "family": s.family, "outcome": s.outcome} for s in PIPELINES.values()]
