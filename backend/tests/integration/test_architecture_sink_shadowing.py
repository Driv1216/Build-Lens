"""Stage 14 remediation R03: shadowed sink names never become request sites or connections.

From extraction through the API projection: a parameter or local declaration named
``fetch``, ``window``, ``globalThis``, ``axios`` or a wrapper's name is not the global,
imported or wrapper binding the sink rules require, so it produces no ``client_call`` node
and no ``calls_endpoint`` edge. Each file also holds a control call that must still connect.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from buildlens.application.architecture import ArchitectureService
from buildlens.infrastructure.persistence.architecture import SqlAlchemyArchitectureStore
from tests.semantic_support import CatalogStack, write

pytestmark = pytest.mark.integration

FILES = {
    "pyproject.toml": '[project]\nname = "shadow"\n',
    "app/__init__.py": "",
    "app/main.py": (
        "from fastapi import FastAPI\n\n"
        "app = FastAPI()\n\n\n"
        "@app.get('/api/users')\n"
        "def users():\n    return []\n"
    ),
    "web/param.ts": (
        "export function inspect(fetch: (path: string) => string) {\n"
        '  return fetch("/api/users");\n'
        "}\n"
        "export const arrow = (fetch: any) => fetch('/api/users')\n"
        "export const ok = () => fetch('/api/users')\n"
    ),
    "web/globals.ts": (
        "export function f(window: any) { return window.fetch('/api/users') }\n"
        "export function g() { const globalThis = { fetch: (u: string) => u }\n"
        "  return globalThis.fetch('/api/users') }\n"
        "export const ok = () => window.fetch('/api/users')\n"
    ),
    "web/axios.ts": (
        "import axios from 'axios'\n"
        "export function f(axios: any) { return axios.get('/api/users') }\n"
        "export function g() { let axios = make(); return axios.get('/api/users') }\n"
        "export const ok = () => axios.get('/api/users')\n"
    ),
    "web/locals.ts": (
        "export function a() { const fetch = (u: string) => u; return fetch('/api/users') }\n"
        "export function b() { if (flag) { var fetch = mock }; return fetch('/api/users') }\n"
        "export function c() { try { go() } catch (fetch) { fetch('/api/users') } }\n"
        "export function d() { for (const fetch of fns) { fetch('/api/users') } }\n"
        "export function ok() { { const fetch = mock } return fetch('/api/users') }\n"
    ),
    "web/old.js": "with (scope) { fetch('/api/users') }\nfetch('/api/users')\n",
    "web/client.ts": (
        "const base = 'http://localhost:8000'\n"
        "export function request(path: string) { return fetch(`${base}${path}`) }\n"
        "export function hostile(fetch: any, path: string) { return fetch(`${base}${path}`) }\n"
        "export const same = () => request('/api/users')\n"
    ),
    "web/callers.ts": (
        "import { request, hostile } from './client'\n"
        "export function f(request: (p: string) => string) { return request('/api/users') }\n"
        "export const viaHostile = () => hostile(x, '/api/users')\n"
        "export const ok = () => request('/api/users')\n"
    ),
}
# (locator, line) of every call that must connect; every other call must not.
CONTROLS = {
    ("web/param.ts", 5),
    ("web/globals.ts", 4),
    ("web/axios.ts", 4),
    ("web/locals.ts", 5),
    ("web/old.js", 2),
    ("web/client.ts", 4),
    ("web/callers.ts", 4),
}


@pytest.fixture
def loaded(pg_engine: Engine, tmp_path: Path) -> Any:
    write(tmp_path, FILES)
    stack = CatalogStack(pg_engine, tmp_path, "F-shadow")
    snapshot = stack.prepared()
    stack.catalog.build(stack.project.id, snapshot.id)
    service = ArchitectureService(SqlAlchemyArchitectureStore(stack.factory), stack.catalog)
    service.build(stack.project.id, snapshot.id)
    assert service.integrity(stack.project.id, snapshot.id, None)["evidence_failure_count"] == 0
    return service.graph(stack.project.id, snapshot.id, None)[2]


def test_shadowed_sinks_and_wrappers_create_no_connections(loaded: Any) -> None:
    sites = {
        (node.locator, node.attributes["line"]): node
        for node in loaded.nodes.values()
        if node.kind == "client_call"
    }
    assert set(sites) == CONTROLS
    edges = [edge for edge in loaded.edges.values() if edge.kind == "calls_endpoint"]
    connected = {
        (loaded.nodes[edge.source_key].locator, loaded.nodes[edge.source_key].attributes["line"])
        for edge in edges
    }
    assert connected == CONTROLS
    assert {edge.target_key.split(":function:")[0] for edge in edges} == {
        "endpoint:symbol:app/main.py#users"
    }
    reasons = sorted(
        diagnostic.attributes["reason"]
        for diagnostic in loaded.stored.diagnostics
        if diagnostic.code == "UNRECOGNIZED_CLIENT"
    )
    # param x2, globals x2, axios x2, locals x4, hostile's own sink: shadowed locally;
    # the call inside ``with`` cannot be bound and stays unknown.
    assert reasons.count("SINK_SHADOWED_LOCALLY") == 11
    assert reasons.count("SINK_BINDING_UNKNOWN") == 1
