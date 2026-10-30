"""Stage 14 Milestone C fixtures: integrations (F-int), database (F-db), frontend routes and
subsystem candidates (F-sub). Every expectation is designed in advance."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from buildlens.application.architecture import ArchitectureService
from buildlens.application.architecture_queries import ArchitectureQueries
from buildlens.infrastructure.persistence.architecture import SqlAlchemyArchitectureStore
from tests.semantic_support import CatalogStack, write

pytestmark = pytest.mark.integration

MODEL = (
    "from sqlalchemy import ForeignKey\n"
    "from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column\n\n\n"
    "class Base(DeclarativeBase):\n    pass\n\n\n"
    "class User(Base):\n"
    '    __tablename__ = "users"\n'
    "    id: Mapped[int] = mapped_column(primary_key=True)\n\n\n"
    "class Order(Base):\n"
    '    __tablename__ = "orders"\n'
    '    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))\n\n\n'
    "class Draft(Base):\n"
    '    __tablename__ = "drafts"\n'
    "    id: Mapped[int] = mapped_column(primary_key=True)\n"
)
FILES = {
    "pyproject.toml": (
        '[project]\nname = "demo"\n'
        'dependencies = ["requests>=2", "PyYAML", "typing_extensions",'
        ' "pkg @ https://example.com/secret/x.whl"]\n\n'
        '[project.optional-dependencies]\ndev = ["pytest"]\n'
    ),
    "app/__init__.py": "",
    "app/domain/__init__.py": "",
    "app/domain/model.py": MODEL,
    "app/domain/bad.py": "from app.application import orders\n",
    "app/application/__init__.py": "",
    "app/application/orders.py": (
        "import os\nimport requests\nimport unknownpkg\nimport yaml\n\n"
        "from app.domain.model import Order\n"
    ),
    "app/application/orders_utils.py": "VALUE = 1\n",
    "app/infrastructure/__init__.py": "",
    "app/infrastructure/db.py": "from app.application import orders\n",
    "app/api/__init__.py": "",
    "app/api/orders_routes.py": (
        "from fastapi import APIRouter\n\n"
        "from app.application import orders, orders_utils\n\n"
        "router = APIRouter()\n\n\n"
        "@router.get('/orders')\n"
        "def list_orders():\n    return []\n"
    ),
    "app/api/users_routes.py": (
        "from fastapi import APIRouter\n\n"
        "from app.application import orders_utils\n\n"
        "router = APIRouter()\n\n\n"
        "@router.get('/users')\n"
        "def list_users():\n    return []\n"
    ),
    "app/main.py": (
        "from fastapi import FastAPI\n\n"
        "from app.api.orders_routes import router as orders_router\n"
        "from app.api.users_routes import router as users_router\n\n"
        "app = FastAPI()\n"
        "app.include_router(orders_router)\n"
        "app.include_router(users_router)\n"
    ),
    "migrations/versions/0001_init.py": (
        "from alembic import op\n\n\ndef upgrade():\n"
        "    op.create_table('users')\n    op.create_table('orders')\n"
        "    op.create_table('audit')\n"
    ),
    "web/package.json": json.dumps(
        {
            "name": "web",
            "dependencies": {"react": "^18", "lodash": "^4", "react-dom": "github:x/y"},
            "devDependencies": {"vite": "^5"},
        }
    ),
    "web/src/api/orders.ts": (
        "import lodash from 'lodash'\nimport fs from 'fs'\nimport left from 'leftpad'\n"
        "export const listOrders = () => fetch('/orders')\n"
    ),
    "web/src/App.tsx": (
        "import { Route, Routes } from 'react-router'\n"
        "import { OrdersPage } from './pages/OrdersPage'\n"
        "export function App() {\n"
        "  return <Routes>\n"
        '    <Route path="/shop" element={<OrdersPage />}>\n'
        '      <Route path="orders" element={<OrdersPage />} />\n'
        "    </Route>\n"
        "  </Routes>\n"
        "}\n"
    ),
    "web/src/pages/OrdersPage.tsx": (
        "import { listOrders } from '../api/orders'\n"
        "import ReactDom from 'react-dom'\n"
        "export function OrdersPage() { listOrders(); return <div /> }\n"
    ),
}


@pytest.fixture
def model(pg_engine: Engine, tmp_path: Path) -> Any:
    write(tmp_path, FILES)
    stack = CatalogStack(pg_engine, tmp_path, "F-views")
    snapshot = stack.prepared()
    stack.catalog.build(stack.project.id, snapshot.id)
    service = ArchitectureService(SqlAlchemyArchitectureStore(stack.factory), stack.catalog)
    run = service.build(stack.project.id, snapshot.id)
    assert service.integrity(stack.project.id, snapshot.id, None)["evidence_failure_count"] == 0
    _, _, loaded = service.graph(stack.project.id, snapshot.id, None)
    return run, loaded


def _diagnostics(loaded: Any, code: str) -> list[Any]:
    return [d for d in loaded.stored.diagnostics if d.code == code]


def _edges(loaded: Any, kind: str) -> list[Any]:
    return sorted((e for e in loaded.edges.values() if e.kind == kind), key=lambda e: e.key)


def test_integrations_declared_vs_imported(model: Any) -> None:
    run, loaded = model
    origin = {
        n.name: n.attributes.get("origin")
        for n in loaded.nodes.values()
        if n.kind == "external_package"
    }
    assert origin["requests"] == "declared"
    assert origin["os"] == "stdlib"
    assert origin["yaml"] == "not_matched_to_declaration"  # PyYAML: import name differs
    assert origin["unknownpkg"] == "not_matched_to_declaration"
    assert origin["PyYAML"] == "declared_not_matched"  # never "declared, not imported"
    assert origin["vite"] == "declared_only"
    assert origin["lodash"] == "declared" and origin["fs"] == "builtin"
    assert origin["leftpad"] == "imported_not_declared"
    undeclared = {d.subjects[0] for d in _diagnostics(loaded, "IMPORTED_NOT_DECLARED")}
    assert undeclared == {"external:npm:leftpad", "external:npm:react-router"}
    python = _diagnostics(loaded, "DECLARED_NOT_MATCHED_BY_IMPORT_NAME")[0]
    assert python.tier == "unknown"
    assert set(python.attributes["names"]) == {"PyYAML", "pkg", "pytest", "typing_extensions"}
    # npm absence is claimed only because the web workspace's coverage is complete.
    absent = _diagnostics(loaded, "DECLARED_NOT_OBSERVED_IMPORTED")[0]
    assert absent.tier == "derived" and absent.attributes["names"] == ["react", "vite"]
    declares = {e.target_key: e for e in _edges(loaded, "declares_dependency")}
    assert declares["external:python:pkg"].attributes["kinds"] == ["direct_reference"]
    assert declares["external:npm:react-dom"].attributes["kinds"] == ["direct_reference"]
    # Version and URL text are never stored.
    stored = json.dumps(
        [
            [n.attributes for n in loaded.nodes.values()],
            [e.attributes for e in loaded.edges.values()],
        ]
    )
    assert "example.com" not in stored and "secret" not in stored and "^18" not in stored
    assert run.coverage["integrations"]["manifests_parsed"] == 2


def test_database_tables_models_migrations_and_absence(model: Any) -> None:
    _, loaded = model
    tables = {n.name: n for n in loaded.nodes.values() if n.kind == "db_table"}
    assert set(tables) == {"users", "orders", "drafts", "audit"}
    assert tables["users"].attributes["merged_by_name"] is True
    references = [(e.source_key, e.target_key) for e in _edges(loaded, "references_table")]
    assert references == [("db_table:orders", "db_table:users")]
    uses = [(e.source_key, e.target_key) for e in _edges(loaded, "uses_model")]
    assert uses == [("module:app/application/orders.py", "db_table:orders")]
    missing = {d.attributes["claim"] if "claim" in d.attributes else d.code: d.subjects
               for d in loaded.stored.diagnostics if d.category == "database"}  # fmt: skip
    assert [d.subjects for d in _diagnostics(loaded, "NO_OBSERVED_MIGRATION")] == [
        ("db_table:drafts",)
    ]
    assert [d.subjects for d in _diagnostics(loaded, "NO_OBSERVED_MODEL")] == [("db_table:audit",)]
    assert len(missing) == 2


def test_frontend_routes_join_nested_paths_and_bind_elements(model: Any) -> None:
    _, loaded = model
    routes = sorted(
        (n.attributes["full_path"], n.attributes["element"])
        for n in loaded.nodes.values()
        if n.kind == "frontend_route"
    )
    assert routes == [("/shop", "OrdersPage"), ("/shop/orders", "OrdersPage")]
    targets = {e.target_key for e in _edges(loaded, "routes_to")}
    assert targets == {"module:web/src/pages/OrdersPage.tsx"}


def test_layer_candidates_and_functional_slices(model: Any) -> None:
    _, loaded = model
    scopes = {
        n.attributes["scope"]: n for n in loaded.nodes.values() if n.kind == "layer_candidate"
    }
    assert set(scopes) == {"container:app", "container:web/src"}
    layers = scopes["container:app"]
    inversion = layers.attributes["inversions"]
    assert [(i["from"], i["to"]) for i in inversion] == [
        ("container:app/domain", "container:app/application")
    ]
    assert layers.attributes["cyclic_groups"] == [
        ["container:app/application", "container:app/domain"]
    ]
    assert _diagnostics(loaded, "LAYER_CANDIDATE_INVERSION")[0].tier == "inferred"
    slices = {n.name: n for n in loaded.nodes.values() if n.kind == "functional_slice"}
    orders = slices["orders"]
    assert set(orders.attributes["grouping"]) == {"API_CANDIDATE", "NAME_STEM"}
    members = {e.source_key for e in _edges(loaded, "member_of") if e.target_key == orders.key}
    assert "module:app/application/orders.py" in members
    assert "module:web/src/pages/OrdersPage.tsx" in members
    # A misleading stem never assigns a module that another slice also reaches.
    assert "module:app/application/orders_utils.py" not in members
    assert "module:app/application/orders_utils.py" in orders.attributes["shared_modules"]
    assert "users" in slices


def test_containment_tree_root_lists_only_containment(pg_engine: Engine, tmp_path: Path) -> None:
    """D7 browser run: parentless view nodes (tables, slices, layers) were listed as files."""
    write(tmp_path, FILES)
    stack = CatalogStack(pg_engine, tmp_path, "F-views tree")
    snapshot = stack.prepared()
    stack.catalog.build(stack.project.id, snapshot.id)
    service = ArchitectureService(SqlAlchemyArchitectureStore(stack.factory), stack.catalog)
    service.build(stack.project.id, snapshot.id)
    root = ArchitectureQueries(service).tree(stack.project.id, snapshot.id, None, None)
    assert root.children and {node.kind for node in root.children} <= {
        "workspace",
        "container",
        "module",
    }
