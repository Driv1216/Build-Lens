"""Stage 14 Milestone B F-api fixture: routers, mounts, sinks, wrappers, folding, matching.

Every expected outcome is designed in advance (plan F3, C-3, C-5). Hard gates: no local
candidate for an unbound host, no request evidence from URL-shaped strings, no candidate
through an unbound wrapper binding, and no unconditional match.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from buildlens.application.architecture import ArchitectureService
from buildlens.application.architecture_queries import ArchitectureQueries
from buildlens.infrastructure.persistence.architecture import SqlAlchemyArchitectureStore
from tests.semantic_support import CatalogStack, write

pytestmark = pytest.mark.integration

S = "/api/v1"
FILES = {
    "pyproject.toml": '[project]\nname = "fapi"\n',
    "app/__init__.py": "",
    "app/main.py": (
        "from fastapi import FastAPI\n"
        "from . import admin\n"
        "from .routes import router as items_router\n\n"
        "app = FastAPI()\n"
        "app.include_router(items_router, prefix='/api')\n"
        "app.include_router(admin.router, prefix='/api')\n"
    ),
    "app/routes.py": (
        "from fastapi import APIRouter\n\n"
        "router = APIRouter(prefix='/v1')\n\n\n"
        "@router.get('/items/{item_id:int}')\n"
        "def get_item(item_id: int):\n    return item_id\n\n\n"
        "@router.post('/items')\n"
        "def create_item():\n    return None\n\n\n"
        "@router.patch('/items/{item_id}')\n"
        "def patch_item(item_id: str):\n    return item_id\n\n\n"
        "@router.get('/files/{path:path}')\n"
        "def get_file(path: str):\n    return path\n\n\n"
        "@router.get('/users')\n"
        "def users():\n    return []\n"
    ),
    "app/admin.py": (
        "from fastapi import APIRouter\n\n"
        "router = APIRouter(prefix='/admin')\n\n\n"
        "@router.get('/stats')\n"
        "def stats():\n    return {}\n"
    ),
    "app/orphan.py": (
        "from fastapi import APIRouter\n\n"
        "router = APIRouter()\n\n\n"
        "@router.get('/orphan')\n"
        "def orphan():\n    return None\n"
    ),
    "web/client.ts": (
        "const base = import.meta.env.VITE_API ?? 'http://localhost:8000/api'\n"
        "export async function request(path: string, init?: RequestInit) {\n"
        "  return fetch(`${base}${path}`, { ...init })\n"
        "}\n"
    ),
    "web/calls.ts": (
        "import { request } from './client'\n"
        "export const item = (id: string) => request(`/v1/items/${id}`)\n"
        "export const create = () => request('/v1/items', { method: 'POST' })\n"
        "export const lower = (id: string) => request(`/v1/items/${id}`, { method: 'patch' })\n"
        "export const file = (p: string) => request(`/v1/files/${p}`)\n"
        "export const stats = () => request('/admin/stats')\n"
        "export const slash = () => request('/v1/users/')\n"
        "export const missing = () => request('/v1/nothing')\n"
        "export const external = () => fetch('https://api.example.com/api/v1/users')\n"
        "export const privateHost = () => fetch('http://10.0.0.5:8000/api/v1/users')\n"
        "export const relative = () => fetch('/api/v1/users')\n"
        "export const dynamic = (url: string) => fetch(url)\n"
        "export const go = () => navigate('/api/v1/users')\n"
        "log.info('/api/v1/users')\n"
    ),
    "web/shadow.ts": "function fetch(url: string) { return url }\nfetch('/api/v1/users')\n",
    "web/axios.ts": "import axios from 'axios'\naxios.get('/api/v1/users')\n",
    "web/fake.ts": "import axios from './axios-mock'\naxios.get('/api/v1/users')\n",
    "web/axios-mock.ts": "export default { get: (url: string) => url }\n",
    "web/barrel.ts": "export { request } from './client'\n",
    "web/viabarrel.ts": (
        "import { request } from './barrel'\nrequest('/v1/items', { method: 'POST' })\n"
    ),
}


@pytest.fixture
def model(pg_engine: Engine, tmp_path: Path) -> Any:
    write(tmp_path, FILES)
    stack = CatalogStack(pg_engine, tmp_path, "F-api")
    snapshot = stack.prepared()
    stack.catalog.build(stack.project.id, snapshot.id)
    service = ArchitectureService(SqlAlchemyArchitectureStore(stack.factory), stack.catalog)
    run = service.build(stack.project.id, snapshot.id)
    _, _, loaded = service.graph(stack.project.id, snapshot.id, None)
    # Every cited row (literal, mount, import binding, catalog fact) must verify, and the
    # authoritative API view must answer rather than fail closed.
    assert service.integrity(stack.project.id, snapshot.id, None)["evidence_failure_count"] == 0
    view = ArchitectureQueries(service).api(stack.project.id, snapshot.id, None, limit=500)
    assert view.clients_total == len([n for n in loaded.nodes.values() if n.kind == "client_call"])
    return run, loaded


def _endpoint(loaded: Any, function: str) -> Any:
    return next(
        node
        for node in loaded.nodes.values()
        if node.kind == "api_endpoint" and node.attributes["function"] == function
    )


def _calls(loaded: Any, locator: str) -> dict[int, Any]:
    return {
        node.attributes["line"]: node
        for node in loaded.nodes.values()
        if node.kind == "client_call" and node.locator == locator
    }


def _fallback(node: Any) -> dict[str, Any]:
    """The branch built from the ``?? 'literal'`` fallback (the env branch is opaque)."""
    branches = [b for b in node.attributes["branches"] if b["outcome"] != "opaque_origin"]
    assert len(branches) == 1, node.attributes["branches"]
    return dict(branches[0])


def test_full_paths_follow_prefixes_mounts_and_absence_gating(model: Any) -> None:
    _, loaded = model
    assert [p["path"] for p in _endpoint(loaded, "get_item").attributes["paths"]] == [
        "/api/v1/items/{item_id:int}"
    ]
    stats = _endpoint(loaded, "stats").attributes["paths"]
    assert [p["path"] for p in stats] == ["/api/admin/stats"]
    orphan = _endpoint(loaded, "orphan").attributes["paths"]
    assert orphan[0]["path"] is None and orphan[0]["reasons"] == ["ROUTER_NOT_MOUNTED"]
    codes = {d.code for d in loaded.stored.diagnostics}
    assert "ROUTER_NOT_MOUNTED" in codes


def test_wrapper_calls_fold_bases_and_match_conservatively(model: Any) -> None:
    _, loaded = model
    calls = _calls(loaded, "web/calls.ts")
    item = calls[2]
    assert [b["outcome"] for b in item.attributes["branches"]] == [
        "opaque_origin",
        "conditional_candidate",
    ]
    branch = _fallback(item)
    assert branch["candidates"] == [_endpoint(loaded, "get_item").key]
    assert branch["strength"] == "converter_unverified"
    assert set(branch["conditions"]) >= {
        "BASE_FROM_FALLBACK_LITERAL",
        "LOOPBACK_ORIGIN_ASSUMED_LOCAL_BACKEND",
        "GLOBAL_FETCH_ASSUMED",
        "METHOD_VIA_WRAPPER_INIT",
        "CONVERTER_UNVERIFIED",
    }
    create = _fallback(calls[3])
    assert create["outcome"] == "conditional_candidate"
    assert create["candidates"] == [_endpoint(loaded, "create_item").key]
    assert create["strength"] == "literal_exact"
    # Fetch never upper-cases 'patch': a PATCH route is a near miss, not a candidate.
    patch = _fallback(calls[4])
    assert patch["outcome"] == "method_mismatch"
    assert "METHOD_NOT_NORMALISED_BY_FETCH" in patch["conditions"]
    assert _fallback(calls[5])["strength"] == "spanning"
    assert _fallback(calls[6])["candidates"] == [_endpoint(loaded, "stats").key]
    slash = _fallback(calls[7])
    assert slash["outcome"] == "no_candidate" and "TRAILING_SLASH_DIFFERS" in slash["reasons"]
    assert _fallback(calls[8])["outcome"] == "no_candidate"
    # The env-only branch never becomes a candidate.
    env = [b for b in item.attributes["branches"] if b["outcome"] == "opaque_origin"][0]
    assert env["reasons"] == ["BASE_UNKNOWN"] and env["candidates"] == []


def test_destinations_sinks_and_hard_gates(model: Any) -> None:
    _, loaded = model
    calls = _calls(loaded, "web/calls.ts")
    external = calls[9].attributes["branches"][0]
    assert (external["destination"], external["outcome"]) == (
        "public_host_unbound",
        "external_destination",
    )
    private = calls[10].attributes["branches"][0]
    assert private["destination"] == "private_network_host_unbound"
    relative = calls[11].attributes["branches"][0]
    assert relative["outcome"] == "conditional_candidate"
    assert "SAME_ORIGIN_DEPLOYMENT_ASSUMED" in relative["conditions"]
    # ``(url) => fetch(url)`` qualifies as a wrapper (C-3); with no callers it is no request.
    assert 12 not in calls
    assert 13 not in calls and 14 not in calls  # navigate() and log.info() are not requests
    # Shadowed fetch and a non-axios binding are not sinks; the real axios binding is.
    assert _calls(loaded, "web/shadow.ts") == {}
    assert _calls(loaded, "web/fake.ts") == {}
    assert _calls(loaded, "web/axios.ts")[2].attributes["branches"][0]["outcome"] == (
        "conditional_candidate"
    )
    # A wrapper reached through a barrel stays unbound.
    assert _calls(loaded, "web/viabarrel.ts") == {}
    codes = [d.code for d in loaded.stored.diagnostics]
    assert codes.count("UNRECOGNIZED_CLIENT") == 2
    assert "WRAPPER_BINDING_UNKNOWN" in codes
    # Hard gates.
    hosts = [n for n in loaded.nodes.values() if n.kind == "external_destination"]
    assert {n.attributes["host"] for n in hosts} == {"api.example.com", "10.0.0.5"}
    assert all(n.attributes["ownership"] == "unknown" for n in hosts)
    calls_endpoint = [e for e in loaded.edges.values() if e.kind == "calls_endpoint"]
    assert calls_endpoint and all(e.tier == "inferred" and e.conditions for e in calls_endpoint)
    sources = {e.source_key for e in calls_endpoint}
    assert calls[9].key not in sources and calls[10].key not in sources
