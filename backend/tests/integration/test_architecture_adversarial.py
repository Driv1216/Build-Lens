"""Stage 14 Milestone D4: the pre-registered adversarial suite (``architecture-evaluation.v1``).

The Stage 13 hostile repository plus Stage 14 attacks on API matching, module identity and
literal capture. The designed oracle allows exactly four API connections; every other request
site must produce none. Gates (pre-registered): 0 false connections, 0 source reads during
the build, 0 outbound connections, sentinels never fire, and 100% of tamper operations on
architecture rows or their cited evidence are detected (then repaired and re-verified).
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from buildlens.application.architecture import (
    ArchitectureEvidenceInvalidError,
    ArchitectureGraphCache,
    ArchitectureIntegrityError,
    ArchitectureService,
)
from buildlens.application.architecture_evaluation import ADVERSARIAL, SUITES, criteria
from buildlens.application.architecture_queries import VIEWS, ArchitectureQueries
from buildlens.infrastructure.persistence.architecture import SqlAlchemyArchitectureStore
from tests.adversarial_repository import RLO, SENTINEL, materialize
from tests.semantic_support import CatalogStack, write

pytestmark = pytest.mark.integration

CYRILLIC_E = "е"
HOSTILE = {
    "app/server.py": (
        "from fastapi import FastAPI\n\nfrom app import api\n\n"
        "app = FastAPI()\napp.include_router(api.router, prefix='/api')\n"
    ),
    "app/api.py": (
        "from fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n"
        '"""Docstring text, not code: @router.get("/fake")"""\n'
        "# @router.get('/commented')\n\n\n"
        "@router.get('/users')\ndef users():\n    return []\n\n\n"
        "@router.get('/items/{item_id:int}')\ndef item(item_id: int):\n    return item_id\n\n\n"
        "@router.get('/files/{path:path}')\ndef files(path: str):\n    return path\n\n\n"
        "@router.post('/orders')\ndef create_order():\n    return None\n\n\n"
        "@router.patch('/orders/{order_id}')\ndef patch_order(order_id: str):\n"
        "    return order_id\n\n\n"
        "@router.get('/dup')\ndef dup_one():\n    return 1\n\n\n"
        "@router.get('/dup')\ndef dup_two():\n    return 2\n"
    ),
    "dist/ghost_routes.py": (
        "from fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n"
        "@router.get('/ghost')\ndef ghost():\n    return None\n"
    ),
    "app/users.py": "VALUE = 1\n",
    f"app/us{CYRILLIC_E}rs.py": "VALUE = 2\n",
    "app/pick.py": "from app.users import VALUE\n",
    "app/cyc_a.py": "from app import cyc_b\n",
    "app/cyc_b.py": "from app import cyc_a\n",
    "app/reporting.py": "from tests.helpers import fixture\n",
    "tests/__init__.py": "",
    "tests/helpers.py": "def fixture():\n    return 1\n",
    "web2/package.json": '{"name": "web2", "dependencies": {"axios": "^1"}}\n',
    "web2/ok.ts": (
        "export const users = () => fetch('/api/users')\n"
        "export const files = (a: string, b: string) => fetch(`/api/files/${a}/${b}`)\n"
        "export const order = () => fetch('/api/orders', { method: 'POST' })\n"
        "export const item = (id: string) => fetch(`/api/items/${id}`)\n"
    ),
    "web2/hostile.ts": (
        "// fetch('/api/users') in a comment\n"
        "const B = import.meta.env.VITE_API\n"
        "export const external = () => fetch('https://api.example.com/api/users')\n"
        "export const privateHost = () => fetch('http://10.0.0.5/api/users')\n"
        "export const otherBase = () => fetch('/v2/users')\n"
        "export const wrongMethod = () => fetch('/api/users', { method: 'DELETE' })\n"
        "export const lower = (id: string) => fetch(`/api/orders/${id}`, { method: 'patch' })\n"
        "export const slash = () => fetch('/api/users/')\n"
        "export const typed = () => fetch('/api/items/abc')\n"
        "export const envOnly = () => fetch(`${B}/api/users`)\n"
        "export const opaque = (a: string, b: string) => fetch(`${a}${b}`)\n"
        "export const duplicate = () => fetch('/api/dup')\n"
        "export const ghost = () => fetch('/api/ghost')\n"
        f"export const bidi = () => fetch('/api/us{RLO}ers')\n"
        f"export const homoglyph = () => fetch('/api/us{CYRILLIC_E}rs')\n"
        "export const go = () => navigate('/api/users')\n"
        "export const label = () => t('/api/users')\n"
    ),
    "web2/shadow.ts": "function fetch(url: string) { return url }\nfetch('/api/users')\n",
    "web2/fake.ts": "import axios from './axios-mock'\naxios.get('/api/users')\n",
    "web2/axios-mock.ts": "export default { get: (url: string) => url }\n",
    "web2/client.ts": (
        "export function request(path: string, init: RequestInit = {}) {\n"
        "  return fetch(`${path}`, { ...init })\n}\n"
    ),
    "web2/barrel.ts": "export { request } from './client'\n",
    "web2/viabarrel.ts": "import { request } from './barrel'\nrequest('/api/users')\n",
    "web2/viaalias.ts": "import { request } from '@/client'\nrequest('/api/users')\n",
}
# Designed oracle: (file, function) -> endpoint function; nothing else may connect.
ALLOWED = {
    ("web2/ok.ts", 1): "users",
    ("web2/ok.ts", 2): "files",
    ("web2/ok.ts", 3): "create_order",
    ("web2/ok.ts", 4): "item",
}


@pytest.fixture
def hostile(pg_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    materialize(tmp_path, huge_bytes=200_000, dts_files=5)
    write(tmp_path, HOSTILE)
    stack = CatalogStack(pg_engine, tmp_path, "Stage 14 adversarial")
    snapshot = stack.prepared()
    stack.catalog.build(stack.project.id, snapshot.id)
    reads: list[Any] = []
    connects: list[Any] = []
    database = pg_engine.url.port
    original = socket.socket.connect

    def read(*args: Any, **_: Any) -> Any:
        reads.append(args[1:2])
        raise AssertionError("the architecture build must not read source files")

    def connect(self: socket.socket, address: Any) -> Any:
        if not (isinstance(address, tuple) and address[1] == database):
            connects.append(address)
        return original(self, address)

    monkeypatch.setattr(
        "buildlens.infrastructure.structural.supervisor.SupervisedSourceReader.read", read
    )
    monkeypatch.setattr(socket.socket, "connect", connect)
    service = ArchitectureService(SqlAlchemyArchitectureStore(stack.factory), stack.catalog)
    run = service.build(stack.project.id, snapshot.id)
    queries = ArchitectureQueries(service)
    queries.overview(stack.project.id, snapshot.id, None)
    queries.api(stack.project.id, snapshot.id, None, limit=500)
    for name in VIEWS:
        queries.view(stack.project.id, snapshot.id, None, name)
    monkeypatch.undo()
    return stack, snapshot, service, run, reads, connects, tmp_path


def _connections(loaded: Any) -> list[tuple[str, int, str]]:
    nodes = loaded.nodes
    values = []
    for edge in loaded.edges.values():
        if edge.kind != "calls_endpoint":
            continue
        site, endpoint = nodes[edge.source_key], nodes[edge.target_key]
        values.append((site.locator, site.attributes["line"], endpoint.attributes["function"]))
    return sorted(values)


def test_adversarial_gates_hold(hostile: Any) -> None:
    stack, snapshot, service, run, reads, connects, root = hostile
    assert len(ADVERSARIAL) >= 20
    _, _, loaded = service.graph(stack.project.id, snapshot.id, None)
    connections = _connections(loaded)
    false = [c for c in connections if ALLOWED.get((c[0], c[1])) != c[2].rsplit(".", 1)[-1]]
    missing = set(ALLOWED) - {(c[0], c[1]) for c in connections}
    nodes = loaded.nodes.values()
    # Route-like text in docstrings/comments and routes in excluded directories are not code.
    paths = {p["path"] for n in nodes if n.kind == "api_endpoint" for p in n.attributes["paths"]}
    assert {"/api/fake", "/api/commented", "/ghost", "/api/ghost"}.isdisjoint(paths)
    # Homoglyph files are distinct modules; the Latin import targets the Latin file.
    assert f"module:app/us{CYRILLIC_E}rs.py" in loaded.nodes
    assert "imports|module:app/pick.py|module:app/users.py|resolved|unconditional" in loaded.edges
    codes = {d.code for d in loaded.stored.diagnostics}
    assert {"PRODUCTION_IMPORTS_TEST", "DUPLICATE_ROUTE", "IMPORT_CYCLE"} <= codes
    assert not [n for n in nodes if RLO in repr(n.attributes)]  # bidi never stored
    gates = {
        "false_connections": len(false),
        "source_reads_during_build": len(reads),
        "outbound_connections": len(connects),
        "tamper_detection": 1.0,  # measured in the tamper test below
    }
    rows = criteria(SUITES["adversarial"], {}, gates)
    assert all(row["passed"] for row in rows), (rows, false, connects)
    assert not missing, missing  # the designed positives still connect
    assert not list(root.rglob(SENTINEL)) and not Path(SENTINEL).exists()


def _scalar(stack: CatalogStack, sql: str, **params: Any) -> Any:
    with stack.factory() as session:
        return session.execute(text(sql), params).scalar()


def _execute(stack: CatalogStack, sql: str, **params: Any) -> None:
    with stack.factory.begin() as session:
        session.execute(text(sql), params)


def _detected(stack: CatalogStack, snapshot: Any) -> bool:
    service = ArchitectureService(
        SqlAlchemyArchitectureStore(stack.factory), stack.catalog, cache=ArchitectureGraphCache()
    )
    try:
        report = service.integrity(stack.project.id, snapshot.id, None)
    except (ArchitectureIntegrityError, ArchitectureEvidenceInvalidError):
        return True
    return report["structure"] != "verified" or bool(report["evidence_failure_count"])


def _ref(stack: CatalogStack, run: Any, table: str) -> str:
    value = _scalar(
        stack,
        "SELECT ref->>'id' FROM architecture_evidence e, jsonb_array_elements(e.source_refs) ref "
        "WHERE e.architecture_run_id = :run AND ref->>'table' = :table LIMIT 1",
        run=run.id,
        table=table,
    )
    assert value, table
    return str(value)


def test_tampering_is_always_detected_and_repairable(hostile: Any) -> None:
    stack, snapshot, _, run, *_ = hostile
    r = {"run": run.id}
    literal, chunk, imported = (
        _ref(stack, run, "architecture_literals"),
        _ref(stack, run, "chunks"),
        _ref(stack, run, "import_references"),
    )
    fact = _scalar(
        stack,
        "SELECT catalog_fact_digest FROM architecture_evidence_links WHERE architecture_run_id = "
        ":run AND catalog_fact_digest IS NOT NULL LIMIT 1",
        **r,
    )
    member = _scalar(
        stack,
        "SELECT component_key || '|' || node_key FROM architecture_component_members "
        "WHERE architecture_run_id = :run LIMIT 1",
        **r,
    )
    link = _scalar(
        stack,
        "SELECT ordinal FROM architecture_evidence_links WHERE architecture_run_id = :run "
        "AND edge_ordinal IS NOT NULL AND evidence_digest IS NOT NULL LIMIT 1",
        **r,
    )
    saved_link = None

    def delete_link() -> None:
        nonlocal saved_link
        with stack.factory() as session:
            saved_link = dict(
                session.execute(
                    text(
                        "SELECT * FROM architecture_evidence_links WHERE architecture_run_id = "
                        ":run AND ordinal = :o"
                    ),
                    {**r, "o": link},
                )
                .mappings()
                .one()
            )
        _execute(
            stack,
            "DELETE FROM architecture_evidence_links WHERE architecture_run_id = :run "
            "AND ordinal = :o",
            **r,
            o=link,
        )

    def restore_link() -> None:
        assert saved_link is not None
        columns = ", ".join(saved_link)
        values = ", ".join(f":{name}" for name in saved_link)
        _execute(stack, f"INSERT INTO architecture_evidence_links ({columns}) VALUES ({values})",
                 **saved_link)  # fmt: skip

    component, node = str(member).split("|", 1)
    edits: list[tuple[str, Callable[[], None], Callable[[], None]]] = [
        ("edge tier", *_pair(stack, "architecture_edges", "tier",
                             "tier = 'observed'", "tier = 'derived'",
                             "architecture_run_id = :run AND tier = 'derived'", r)),
        ("edge attributes", *_json(stack, "architecture_edges", "kind = 'imports'", r)),
        ("edge count", *_sql(stack, "UPDATE architecture_edges SET count = count + {d} WHERE "
                             "architecture_run_id = :run AND ordinal = 0", r)),
        ("node attributes", *_json(stack, "architecture_nodes", "kind = 'module'", r)),
        ("diagnostic attributes", *_json(stack, "architecture_diagnostics", "true", r)),
        ("evidence text", *_text(stack, "architecture_evidence", "rendered_text",
                                 "architecture_run_id = :run", r)),
        ("run graph digest", *_sql(stack, "UPDATE architecture_runs SET graph_digest = "
                                   "reverse(graph_digest) WHERE id = :run", r, inverse_same=True)),
        ("evidence link deleted", delete_link, restore_link),
        ("component member deleted",
         lambda: _execute(stack, "DELETE FROM architecture_component_members WHERE "
                          "architecture_run_id = :run AND component_key = :c AND node_key = :n",
                          **r, c=component, n=node),
         lambda: _execute(stack, "INSERT INTO architecture_component_members VALUES "
                          "(:run, :c, :n)", **r, c=component, n=node)),
        ("cited import row", *_text(stack, "import_references", "name", "id = :id",
                                    {"id": imported})),
        ("cited literal row", *_json(stack, "architecture_literals", "id = :id",
                                     {"id": literal}, owned=False)),
        ("cited chunk", *_text(stack, "chunks", "content", "id = :id", {"id": chunk})),
        ("cited catalog fact", *_text(stack, "semantic_facts", "rendered_text",
                                      "fact_digest = :f", {"f": fact})),
    ]  # fmt: skip
    assert not _detected(stack, snapshot)
    detected = []
    for name, tamper, repair in edits:
        tamper()
        detected.append((name, _detected(stack, snapshot)))
        repair()
        assert not _detected(stack, snapshot), f"repair of {name} did not restore integrity"
    rate = sum(found for _, found in detected) / len(detected)
    rows = criteria(SUITES["adversarial"], {}, {"tamper_detection": rate})
    assert [row for row in rows if row["name"] == "tamper_detection_min"][0]["passed"], [
        name for name, found in detected if not found
    ]


def _sql(
    stack: CatalogStack, template: str, params: dict[str, Any], inverse_same: bool = False
) -> tuple[Callable[[], None], Callable[[], None]]:
    return (
        lambda: _execute(stack, template.format(d=1), **params),
        lambda: _execute(stack, template.format(d=1 if inverse_same else -1), **params),
    )


def _pair(
    stack: CatalogStack, table: str, _: str, forward: str, back: str, where: str, params: Any
) -> tuple[Callable[[], None], Callable[[], None]]:
    target = _scalar(stack, f"SELECT ordinal FROM {table} WHERE {where} LIMIT 1", **params)
    clause = "architecture_run_id = :run AND ordinal = :o"
    return (
        lambda: _execute(stack, f"UPDATE {table} SET {forward} WHERE {clause}", **params, o=target),
        lambda: _execute(stack, f"UPDATE {table} SET {back} WHERE {clause}", **params, o=target),
    )


def _json(
    stack: CatalogStack, table: str, where: str, params: Any, owned: bool = True
) -> tuple[Callable[[], None], Callable[[], None]]:
    scope = f"architecture_run_id = :run AND {where}" if owned else where
    key = f"ctid = (SELECT ctid FROM {table} WHERE {scope} LIMIT 1)"
    return (
        lambda: _execute(
            stack,
            f"UPDATE {table} SET attributes = attributes || '{{\"x\": 1}}' WHERE {key}",
            **params,
        ),
        lambda: _execute(
            stack, f"UPDATE {table} SET attributes = attributes - 'x' WHERE attributes ? 'x'"
        ),
    )


def _text(
    stack: CatalogStack, table: str, column: str, where: str, params: Any
) -> tuple[Callable[[], None], Callable[[], None]]:
    key = f"ctid = (SELECT ctid FROM {table} WHERE {where} LIMIT 1)"
    return (
        lambda: _execute(
            stack, f"UPDATE {table} SET {column} = {column} || '~' WHERE {key}", **params
        ),  # fmt: skip
        lambda: _execute(
            stack,
            f"UPDATE {table} SET {column} = left({column}, -1) WHERE {column} LIKE '%~' "
            f"AND {where}",
            **params,
        ),
    )
