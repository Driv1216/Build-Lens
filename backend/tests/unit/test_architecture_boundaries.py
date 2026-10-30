"""Stage 14 boundaries: architecture code is ORM/framework-free and never reaches the network."""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "src" / "buildlens"
NETWORK = ("httpx", "requests", "urllib", "socket", "openai", "google", "http.client")


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _architecture_files() -> list[Path]:
    return sorted(
        [
            *SOURCE.glob("domain/architecture*.py"),
            *SOURCE.glob("application/architecture*.py"),
            SOURCE / "infrastructure" / "persistence" / "architecture.py",
            SOURCE / "api" / "architecture_routes.py",
            SOURCE / "cli" / "architecture.py",
        ]
    )


def test_domain_and_application_are_orm_framework_and_infrastructure_free() -> None:
    files = [p for p in _architecture_files() if p.parent.name in {"domain", "application"}]
    assert len(files) >= 5
    for path in files:
        for name in _imports(path):
            assert not name.startswith(("sqlalchemy", "fastapi", "buildlens.infrastructure")), (
                path,
                name,
            )


def test_architecture_code_has_no_network_or_provider_imports() -> None:
    for path in _architecture_files():
        for name in _imports(path):
            assert not name.startswith(NETWORK), (path, name)
            assert "generation" not in name and "outbound_gate" not in name, (path, name)
