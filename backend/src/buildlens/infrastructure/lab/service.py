"""Composition of the laboratory: worker pool, bridge factory, catalog and LabService."""

from __future__ import annotations

import importlib.metadata
import os
import sys
import threading
from pathlib import Path
from typing import Any

from buildlens.application.embeddings import EmbeddingRuntime
from buildlens.application.lab import LabService
from buildlens.domain.lab import PIPELINES, LabError
from buildlens.infrastructure.lab.bridge import CapabilityBridge
from buildlens.infrastructure.lab.corpus import CorpusGateway
from buildlens.infrastructure.lab.fakes import HashEmbeddingProvider
from buildlens.infrastructure.lab.native import NativePipeline
from buildlens.infrastructure.lab.store import LabStore, default_lab_root
from buildlens.infrastructure.lab.supervisor import (
    SupervisedFrameworkWorker,
    WorkerSpec,
    buildlens_python_path,
    module_argv,
)

BACKEND = Path(__file__).resolve().parents[4]
ISOLATED_ENVS = BACKEND / "lab_envs"
MODULES = {
    "langchain": "buildlens.infrastructure.lab.workers.langchain.main",
    "llamaindex": "buildlens.infrastructure.lab.workers.llamaindex.main",
    "selftest": "buildlens.infrastructure.lab.workers.common.selftest",
}
EXTRA_PACKAGES = {
    "lab-langchain": (
        "langchain",
        "langchain-core",
        "langchain-classic",
        "langchain-text-splitters",
        "langchain-community",
        "bm25s",
    ),
    "lab-llamaindex": ("llama-index-core", "llama-index-retrievers-bm25"),
    "lab-providers": ("langchain-google-genai", "llama-index-llms-google-genai"),
    "lab-pgvector": ("llama-index-vector-stores-postgres",),
    "lab-code": ("tree-sitter-language-pack",),
}


def _version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def isolated_python(name: str) -> Path:
    return ISOLATED_ENVS / name / ".venv" / "bin" / "python"


class LabWorkerPool:
    def __init__(
        self, store: LabStore, *, max_rss_bytes: int, lab_db: tuple[str, int, str] | None = None
    ) -> None:
        self.store = store
        self.max_rss_bytes = max_rss_bytes
        self.lab_db = lab_db  # (host, port, url) for pgvector-capable workers only
        self._workers: dict[str, SupervisedFrameworkWorker] = {}
        self._lock = threading.Lock()

    def index_directory_root(self) -> str:
        return str(self.store.area("indexes"))

    def spec(self, family: str) -> WorkerSpec:
        common: dict[str, Any] = {
            "write_roots": [str(self.store.area("indexes"))],
            "read_roots": [str(self.store.area("indexes")), str(self.store.sandbox_base)],
            "protected_roots": [str(Path.home()), str(BACKEND.parent)],
            "max_rss_bytes": self.max_rss_bytes,
        }
        if family == "langchain_postgres":
            python = isolated_python("langchain_postgres")
            if not python.is_file():
                raise LabError(
                    "LAB_ISOLATED_ENV_MISSING", "Run `buildlens lab env sync langchain-postgres`"
                )
            if self.lab_db is None:
                raise LabError("LAB_DATABASE_NOT_CONFIGURED")
            host, port, url = self.lab_db
            return WorkerSpec(
                family,
                [str(python), "-s", "-m", "buildlens_lab_pg_worker.main"],
                python_path=[str(ISOLATED_ENVS / "langchain_postgres" / "src")],
                allow_net=[(host, port)],
                secret_env={"BUILDLENS_LAB_PG_URL": url},
                **common,
            )
        if family not in MODULES:
            raise LabError("LAB_FRAMEWORK_UNKNOWN", family)
        spec = WorkerSpec(
            family, module_argv(MODULES[family]), python_path=buildlens_python_path(), **common
        )
        if self.lab_db is not None and family in {"langchain", "llamaindex"}:
            host, port, url = self.lab_db
            spec.allow_net = [(host, port)]
            spec.secret_env = {"BUILDLENS_LAB_PG_URL": url}
        return spec

    def get(self, family: str) -> SupervisedFrameworkWorker:
        with self._lock:
            worker = self._workers.get(family)
            if worker is None:
                home = self.store.area("workers") / family
                home.mkdir(mode=0o700, exist_ok=True)
                worker = SupervisedFrameworkWorker(self.spec(family), home)
                self._workers[family] = worker
            return worker

    def close(self) -> None:
        with self._lock:
            for worker in self._workers.values():
                worker.close()
            self._workers.clear()


def catalog(
    store: LabStore | None,
    *,
    lab_db_configured: bool,
    generation_enabled: bool,
    embedding: Any = None,
    reranker: Any = None,
) -> dict[str, Any]:
    extras = {
        name: {package: _version(package) for package in packages}
        for name, packages in EXTRA_PACKAGES.items()
    }
    installed = {
        name: all(v is not None for v in packages.values()) for name, packages in extras.items()
    }
    isolated = {"langchain-postgres": isolated_python("langchain_postgres").is_file()}
    pipelines = []
    for spec in PIPELINES.values():
        reasons = []
        if spec.extra and spec.extra.startswith("isolated:"):
            if not isolated[spec.extra.split(":", 1)[1]]:
                reasons.append(
                    "Isolated environment not synced (buildlens lab env sync langchain-postgres)"
                )
        elif spec.extra and not installed.get(spec.extra, False):
            reasons.append(f"Install the {spec.extra} extra")
        if spec.needs_lab_database and not lab_db_configured:
            reasons.append("BUILDLENS_LAB_DATABASE_URL is not configured")
        pipelines.append(
            {
                "id": spec.id,
                "family": spec.family,
                "label": spec.label,
                "outcome": spec.outcome,
                "corpus_modes": list(spec.corpus_modes),
                "native_chunk_identity": spec.native_chunk_identity,
                "available": not reasons,
                "reasons": reasons,
                "params": {
                    name: {
                        "kind": p.kind,
                        "default": p.default,
                        "minimum": p.minimum,
                        "maximum": p.maximum,
                        "choices": list(p.choices),
                        "help": p.help,
                    }
                    for name, p in sorted(spec.params.items())
                },
            }
        )
    model_status: dict[str, dict[str, Any] | None] = {}
    for name, runtime in (("embedding", embedding), ("reranker", reranker)):
        if runtime is None:
            model_status[name] = None
            continue
        try:
            status = runtime.provider.status()
            model_status[name] = {
                "available": bool(status.available),
                "contract": runtime.provider.contract.model_id,
            }
        except Exception:
            model_status[name] = {"available": False}
    return {
        "contract": "stage12.lab.v1",
        "python": sys.version.split()[0],
        "extras": extras,
        "extras_installed": installed,
        "isolated_environments": isolated,
        "lab_database_configured": lab_db_configured,
        "generation": {
            "enabled": generation_enabled,
            "state": "authorized"
            if generation_enabled
            else "offline-only (no external provider calls)",
        },
        "models": model_status,
        "lab_root": str(store.root) if store else None,
        "pipelines": pipelines,
    }


def bridge_factory(state: Any) -> Any:
    fake_runtime = EmbeddingRuntime(HashEmbeddingProvider())

    def make(**kwargs: Any) -> CapabilityBridge:
        from buildlens.infrastructure.runtime import context_service, retrieval_service

        fake = kwargs.pop("fake", False)
        return CapabilityBridge(
            embedding=fake_runtime if fake else state.embedding_runtime,
            reranker=state.reranker_runtime,
            retrieval=retrieval_service(state),
            context=context_service(state),
            generation_authorized=bool(state.settings.lab_generation_enabled),
            **kwargs,
        )

    return make


def lab_db(settings: Any) -> tuple[str, int, str] | None:
    if settings.lab_database_url is None:
        return None
    from sqlalchemy.engine import make_url

    url = settings.lab_database_url.get_secret_value()
    parsed = make_url(url)
    return (parsed.host or "127.0.0.1", int(parsed.port or 5432), url)


def lab_store(settings: Any) -> LabStore:
    root = Path(settings.lab_root).expanduser() if settings.lab_root else default_lab_root()
    return LabStore(root)


def build_lab_service(state: Any) -> LabService:
    from buildlens.infrastructure.runtime import context_service

    settings = state.settings
    pool = getattr(state, "lab_workers", None)
    if pool is None:
        pool = LabWorkerPool(
            lab_store(settings),
            max_rss_bytes=settings.lab_worker_max_rss_bytes,
            lab_db=lab_db(settings),
        )
        state.lab_workers = pool
    service = getattr(state, "lab_service_instance", None)
    if service is None:
        service = LabService(
            corpus=CorpusGateway(state.session_factory),
            workers=pool,
            store=pool.store,
            bridges=bridge_factory(state),
            native=NativePipeline(context_service(state)),
            build_timeout_seconds=settings.lab_build_timeout_seconds,
            query_timeout_seconds=settings.lab_query_timeout_seconds,
            max_index_documents=settings.lab_max_index_documents,
            max_index_bytes=settings.lab_max_index_bytes,
            generation_authorized=bool(settings.lab_generation_enabled),
        )
        state.lab_service_instance = service
    return service


def worker_environment_report() -> dict[str, Any]:
    return {"pid": os.getpid(), "python": sys.executable}
