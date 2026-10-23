"""Explicit native evaluation commands; no automatic onboarding or model acquisition."""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import typer

from buildlens.application.evaluation import EvaluationRunner, error_code, preflight
from buildlens.application.evaluation_binding import check_corpus, definition_digest
from buildlens.core.config import get_settings
from buildlens.domain.evaluation import EvaluationError, digest, primitive
from buildlens.infrastructure.evaluation.corpus import SqlAlchemyEvaluationCorpus
from buildlens.infrastructure.evaluation.local import (
    LocalRunStore,
    atomic_write,
    boundary_path,
    load_dataset,
    run_root,
)
from buildlens.infrastructure.evaluation.report import compare, report
from buildlens.infrastructure.runtime import LocalRuntime, ask_service, context_service

app = typer.Typer(help="Evaluate frozen production RAG evidence using local versioned definitions.")


def implementation_identity() -> dict[str, Any]:
    backend = Path(__file__).resolve().parents[3]
    root = backend.parent
    files = sorted((backend / "src" / "buildlens").rglob("*.py"))
    hashes = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files
    }

    def git(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), *args], capture_output=True, timeout=5, check=False
            )
            return result.stdout.decode().strip() if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    status = git("status", "--porcelain")
    lock = backend / "uv.lock"
    return {
        "version": "stage11.implementation.v1",
        "source_digest": digest(hashes),
        "source_file_digests": hashes,
        "lockfile_digest": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "git_commit": git("rev-parse", "HEAD"),
        "git_dirty": bool(status) if status is not None else None,
    }


async def _validate(
    dataset: Path, project: UUID, snapshot: UUID, corpus_manifest: Path | None
) -> dict[str, Any]:
    runtime = LocalRuntime(get_settings())
    try:
        evidence = SqlAlchemyEvaluationCorpus(runtime.session_factory).load(project, snapshot)
        root = Path(evidence.snapshot.canonical_root or "")
        definition = load_dataset(boundary_path(dataset, root, "datasets"))
        if corpus_manifest is not None:
            # Explicit inspection/export only. Does not prepare evidence or edit the dataset.
            check_corpus(
                replace(
                    definition,
                    corpus_manifest=evidence.manifest,
                    corpus_digest=digest(evidence.manifest),
                ),
                evidence,
            )
            atomic_write(boundary_path(corpus_manifest, root, "datasets"), evidence.manifest)
        if corpus_manifest is not None and definition.corpus_manifest is None:
            return {
                "definitions": "passed",
                "ground_truth": definition.review_status,
                "corpus": "exported; not pinned",
                "corpus_digest": digest(evidence.manifest),
                "invalid_cases": {},
                "manifest_path": str(corpus_manifest),
            }
        bindings, invalid = preflight(definition, evidence)
        return {
            "definitions": "passed",
            "ground_truth": definition.review_status,
            "definition_digest": definition_digest(definition),
            "judgments_digest": definition.judgments_digest,
            "corpus": "passed",
            "bound_cases": len(bindings),
            "invalid_cases": invalid,
            "embedding_model": primitive(runtime.embedding_runtime.provider.status()),
            "reranker_model": primitive(runtime.reranker_runtime.provider.status()),
            "answer_configuration": ask_service(runtime).status(),
        }
    finally:
        await runtime.close()


@app.command("validate")
def validate(
    dataset: Path = typer.Option(..., "--dataset"),
    project: UUID = typer.Option(..., "--project"),
    snapshot: UUID = typer.Option(..., "--snapshot"),
    corpus_manifest: Path | None = typer.Option(
        None,
        "--corpus-manifest",
        help=(
            "Export inspected membership under datasets explicitly; "
            "does not prepare or pin evidence automatically."
        ),
    ),
) -> None:
    """Check approval, exact evidence binding, frozen membership and prerequisites."""
    try:
        value = asyncio.run(_validate(dataset, project, snapshot, corpus_manifest))
        typer.echo(json.dumps(value, indent=2))
        if value["invalid_cases"] or value["corpus"] != "passed":
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except (EvaluationError, OSError) as error:
        typer.echo(f"evaluation validation: {error_code(error)}")
        raise typer.Exit(code=1) from None
    except Exception as error:
        typer.echo(f"evaluation validation: {error_code(error)}")
        raise typer.Exit(code=1) from None


async def _run(
    dataset: Path,
    project: UUID,
    snapshot: UUID,
    destination: Path,
    answers: bool,
    cases: list[str] | None,
) -> None:
    runtime = LocalRuntime(get_settings())
    try:
        corpus = SqlAlchemyEvaluationCorpus(runtime.session_factory)
        snapshot_record = corpus.processing.get_snapshot(project, snapshot)
        if snapshot_record is None or not snapshot_record.canonical_root:
            raise EvaluationError("SNAPSHOT_NOT_FOUND")
        root = Path(snapshot_record.canonical_root)
        definition = load_dataset(boundary_path(dataset, root, "datasets"))
        destination = boundary_path(destination, root, "runs")
        context = context_service(runtime)
        _, reranker_contract = context.status()
        identity = implementation_identity()
        identity["runtime"] = {
            "python": platform.python_version(),
            "platform": platform.system(),
            "machine": platform.machine(),
        }
        identity["configuration"] = {
            "context_max_bytes": runtime.settings.context_max_bytes,
            "context_max_items": runtime.settings.context_max_items,
            "context_max_source_bytes": runtime.settings.context_max_source_bytes,
            "context_max_response_bytes": runtime.settings.context_max_response_bytes,
            "context_request_timeout_seconds": runtime.settings.context_request_timeout_seconds,
            "reranker_contract": primitive(reranker_contract),
            "generation_requested_policy": primitive(ask_service(runtime)._policy)
            if answers
            else None,
            "provider_contract": primitive(runtime.generation_provider.contract)
            if answers
            else None,
        }
        runner = EvaluationRunner(
            corpus,
            context,
            ask_service(runtime),
            LocalRunStore(destination),
            progress=typer.echo,
        )
        manifest = await runner.run(
            definition,
            project,
            snapshot,
            answers=answers,
            implementation=identity,
            selected_cases=tuple(cases) if cases else None,
        )
        report(destination)
        typer.echo(f"run {manifest['id']}: {manifest['status']}; {destination / 'summary.md'}")
        if (
            manifest["counts"].get("error")
            or manifest["counts"].get("invalid")
            or not manifest["comparison_valid"]
        ):
            raise typer.Exit(code=1)
    finally:
        await runtime.close()


@app.command("run")
def run(
    dataset: Path = typer.Option(..., "--dataset"),
    project: UUID = typer.Option(..., "--project"),
    snapshot: UUID = typer.Option(..., "--snapshot"),
    destination: Path = typer.Option(..., "--destination"),
    answers: bool = typer.Option(
        False, "--answers", help="Explicitly invoke configured production generation provider."
    ),
    case: list[str] | None = typer.Option(
        None, "--case", help="Select stable case IDs; repeat this option."
    ),
) -> None:
    """Run vector, hybrid and hybrid plus reranking sequentially; context only by default."""
    try:
        asyncio.run(_run(dataset, project, snapshot, destination, answers, case))
    except (typer.Exit, KeyboardInterrupt):
        raise
    except Exception as error:
        typer.echo(f"evaluation run: {error_code(error)}")
        raise typer.Exit(code=1) from None


@app.command("report")
def rescore(
    run: Path = typer.Option(..., "--run"),
    dataset: Path | None = typer.Option(
        None, "--dataset", help="Rescore compatible observations against a new judgment revision."
    ),
    reviews: Path | None = typer.Option(None, "--reviews"),
    verification: Path | None = typer.Option(
        None, "--verification", help="Attach a digested independent verification record under runs."
    ),
) -> None:
    """Deterministically rescore saved observations; optionally validate human reviews."""
    try:
        root = run_root(run)
        run = boundary_path(run, root, "runs")
        revised = load_dataset(boundary_path(dataset, root, "datasets")) if dataset else None
        if reviews:
            reviews = boundary_path(reviews, root, "reviews")
        report(run, revised=revised, reviews=reviews, verification=verification)
        typer.echo(str(run / "summary.md"))
    except Exception as error:
        typer.echo(f"evaluation report: {error_code(error)}")
        raise typer.Exit(code=1) from None


@app.command("compare")
def paired(
    left: Path = typer.Option(..., "--left"),
    right: Path = typer.Option(..., "--right"),
    output: Path | None = typer.Option(None, "--output"),
) -> None:
    """Compare matching corpus, judgments, cases, filters and metric definitions."""
    try:
        root = run_root(left)
        left = boundary_path(left, root, "runs")
        right = boundary_path(right, run_root(right), "runs")
        markdown = compare(left, right)
        if output:
            atomic_write(boundary_path(output, root, "runs"), markdown, text=True)
        typer.echo(markdown)
    except Exception as error:
        typer.echo(f"evaluation compare: {error_code(error)}")
        raise typer.Exit(code=1) from None
