from pathlib import Path

import typer
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from buildlens import __version__
from buildlens.cli.evaluation import app as evaluation_app
from buildlens.cli.lab import app as lab_app
from buildlens.cli.semantic import app as semantic_app
from buildlens.core.config import get_settings
from buildlens.infrastructure.embeddings.model_cache import download_snapshot
from buildlens.infrastructure.persistence.database import create_database_engine
from buildlens.infrastructure.reranking.model_cache import download_snapshot as download_reranker

app = typer.Typer(add_completion=False, help="BuildLens command-line interface.")
app.add_typer(evaluation_app, name="eval")
app.add_typer(lab_app, name="lab")
app.add_typer(semantic_app, name="semantic")
embeddings_app = typer.Typer(help="Manage the pinned local embedding model.")
app.add_typer(embeddings_app, name="embeddings")
reranking_app = typer.Typer(help="Manage the pinned local cross-encoder reranker.")
app.add_typer(reranking_app, name="reranking")


def version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=version_callback, is_eager=True, help="Show BuildLens version."
    ),
) -> None:
    """Run BuildLens commands."""


@app.command()
def doctor() -> None:
    """Report configuration and PostgreSQL connectivity without inspecting projects."""
    settings = get_settings()
    typer.echo(f"BuildLens {__version__}")
    typer.echo(f"environment: {settings.environment}")
    typer.echo("database URL: configured")
    engine = create_database_engine(settings)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            available = connection.scalar(
                text(
                    "SELECT installed_version IS NOT NULL FROM pg_available_extensions "
                    "WHERE name = 'vector'"
                )
            )
            installed = connection.execute(
                text(
                    "SELECT e.extversion, n.nspname FROM pg_extension e "
                    "JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector'"
                )
            ).first()
            if not available or installed is None:
                typer.echo("pgvector: unavailable or not installed (required: public 0.8.6)")
                raise typer.Exit(code=1)
            if tuple(installed) != ("0.8.6", "public"):
                typer.echo(
                    f"pgvector: incompatible ({installed[1]} {installed[0]}; required public 0.8.6)"
                )
                raise typer.Exit(code=1)
            probe = connection.scalar(text("SELECT 1 - ('[1,0]'::vector <=> '[1,0]'::vector)"))
            if probe is None or abs(float(probe) - 1.0) > 1e-12:
                typer.echo("pgvector: cosine probe failed")
                raise typer.Exit(code=1)
    except SQLAlchemyError as error:
        typer.echo(f"database: unavailable ({error.__class__.__name__})")
        raise typer.Exit(code=1) from error
    finally:
        engine.dispose()
    typer.echo("database: reachable")
    typer.echo("pgvector: public 0.8.6; cosine probe ok")


@app.command()
def diagnostics() -> None:
    """Report retained storage and pending constraint validation without source content."""
    import json

    engine = create_database_engine(get_settings())
    try:
        with engine.connect() as connection:
            counts = {
                table: connection.scalar(text(f'SELECT count(*) FROM "{table}"'))
                for table in (
                    "projects",
                    "project_versions",
                    "artifacts",
                    "structural_analyses",
                    "processing_runs",
                    "artifact_processings",
                    "symbols",
                    "chunks",
                    "embedding_runs",
                    "chunk_embeddings",
                    "artifact_extractions",
                    "semantic_catalog_runs",
                    "semantic_entities",
                    "semantic_facts",
                    "semantic_relations",
                    "semantic_generation_plans",
                    "semantic_generation_runs",
                    "semantic_generation_attempts",
                    "semantic_generation_late_receipts",
                    "semantic_interpretations",
                    "semantic_claims",
                    "semantic_index_entries",
                )
            }
            content_bytes = connection.scalar(
                text("SELECT coalesce(sum(octet_length(content)),0) FROM chunks")
            )
            expression_bytes = connection.scalar(
                text(
                    "SELECT coalesce(sum(pg_column_size(signature)),0) + "
                    "coalesce(sum(pg_column_size(decorators)),0) + "
                    "coalesce(sum(pg_column_size(type_parameters)),0) FROM symbols"
                )
            )
            unvalidated = list(
                connection.scalars(
                    text(
                        "SELECT conname FROM pg_constraint WHERE connamespace = "
                        "'public'::regnamespace AND NOT convalidated ORDER BY conname"
                    )
                )
            )
            vector_bytes = connection.scalar(
                text("SELECT coalesce(sum(pg_column_size(embedding)),0) FROM chunk_embeddings")
            )
            embedding_outcomes = dict(
                connection.execute(
                    text("SELECT outcome, count(*) FROM embedding_runs GROUP BY outcome")
                ).all()
            )
            superseded = connection.scalar(
                text("SELECT count(*) FROM embedding_runs WHERE superseded OR quarantined")
            )
            typer.echo(
                json.dumps(
                    {
                        "counts": counts,
                        "chunk_content_bytes": content_bytes,
                        "structural_expression_storage_bytes": expression_bytes,
                        "vector_storage_bytes": vector_bytes,
                        "embedding_outcomes": embedding_outcomes,
                        "embedding_superseded_or_quarantined": superseded,
                        "unvalidated_constraints": unvalidated,
                        "history_retention": "No automatic deletion",
                    },
                    indent=2,
                )
            )
    except SQLAlchemyError:
        typer.echo("Database diagnostics are unavailable.")
        raise typer.Exit(code=1) from None
    finally:
        engine.dispose()


@embeddings_app.command("download-model")
def download_embedding_model() -> None:
    """Download and verify the pinned model without reading repository content."""
    settings = get_settings()
    try:
        target = download_snapshot(
            Path(settings.embeddings_cache_dir).expanduser(),
            settings.embeddings_model_revision,
        )
    except (RuntimeError, OSError, ValueError) as error:
        typer.echo(f"embedding model: unavailable ({error})")
        raise typer.Exit(code=1) from None
    typer.echo(f"embedding model: verified at {target}")


@reranking_app.command("download-model")
def download_reranking_model() -> None:
    """Download and verify the pinned reranker without reading repository content."""
    settings = get_settings()
    try:
        target = download_reranker(
            Path(settings.reranker_cache_dir).expanduser(), settings.reranker_model_revision
        )
    except (RuntimeError, OSError, ValueError) as error:
        typer.echo(f"reranker model: unavailable ({error})")
        raise typer.Exit(code=1) from None
    typer.echo(f"reranker model: verified at {target}")
