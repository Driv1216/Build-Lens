"""Shared fixtures for Stage 12 laboratory tests (synthetic source only)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from buildlens.application.context import RerankerRuntime
from buildlens.application.embeddings import EmbeddingRuntime
from buildlens.infrastructure.lab.fakes import HashEmbeddingProvider, OverlapReranker
from buildlens.infrastructure.lab.service import LabWorkerPool, build_lab_service
from buildlens.infrastructure.lab.store import LabStore

LONG_BODY = "\n".join(
    f"    total += compute_invoice_line({i}, rate)  # invoice step {i}" for i in range(120)
)

SYNTHETIC_REPOSITORY = {
    "billing/invoices.py": (
        '"""Invoice computation for the synthetic billing service."""\n\n\n'
        "def compute_invoice_line(index, rate):\n"
        '    """Return the amount for one invoice line."""\n'
        "    return index * rate\n\n\n"
        "def compute_invoice_total(rate):\n"
        '    """Sum every invoice line for the monthly statement."""\n'
        "    total = 0\n" + LONG_BODY + "\n    return total\n"
    ),
    "users/loader.py": (
        '"""User loading."""\n\n\n'
        "class UserRepository:\n"
        '    """Loads user records from the customer database."""\n\n'
        "    def load_user(self, user_id):\n"
        '        """Fetch one customer user record by id."""\n'
        "        return {'id': user_id}\n"
    ),
    "docs/architecture.md": (
        "# Architecture\n\nThe billing service computes invoice totals monthly.\n\n"
        "## Users\n\nUser records are loaded by the user repository from the customer database.\n"
    ),
    "config/settings.json": (
        '{"billing": {"currency": "EUR", "rounding": 2}, "users": {"cache": true}}\n'
    ),
}


def write_repository(root: Path) -> None:
    for locator, text in SYNTHETIC_REPOSITORY.items():
        path = root / locator
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (root / ".buildlens-eval").mkdir(exist_ok=True)
    (root / ".buildlens-eval" / "excluded.md").write_text(
        "Evaluation definitions must stay excluded.\n"
    )


def onboard(client: TestClient, root: Path) -> tuple[UUID, UUID]:
    project = UUID(
        client.post(
            "/api/v1/projects", json={"name": "Lab fixture", "root_path": str(root)}
        ).json()["id"]
    )
    snapshot = UUID(client.post(f"/api/v1/projects/{project}/snapshots", json={}).json()["id"])
    scope = f"/api/v1/projects/{project}/snapshots/{snapshot}"
    for operation in ("structure", "processing", "embeddings"):
        response = client.post(scope + "/" + operation, json={})
        assert response.status_code == 200, response.text
    return project, snapshot


def install_fakes(state: Any) -> HashEmbeddingProvider:
    provider = HashEmbeddingProvider()
    state.embedding_runtime = EmbeddingRuntime(provider)
    state.reranker_runtime = RerankerRuntime(OverlapReranker())
    return provider


def lab_service(state: Any, lab_root: Path) -> Any:
    state.lab_workers = LabWorkerPool(LabStore(lab_root), max_rss_bytes=3 * 1024**3)
    state.lab_service_instance = None
    return build_lab_service(state)
