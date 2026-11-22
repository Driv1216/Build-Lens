"""Stage 14 Milestone B literal passes: allow-listed rows, negatives and sanitisation."""

from __future__ import annotations

import json
from typing import Any

from buildlens.domain.architecture_literals import ROWS_PER_ARTIFACT, url_shaped, url_template
from buildlens.domain.structural import ArtifactExtraction
from buildlens.infrastructure.structural.architecture_literals_python import (
    extract_python_literals,
)
from buildlens.infrastructure.structural.architecture_literals_typescript import (
    extract_ts_literals,
)


def _py(source: str) -> ArtifactExtraction:
    return extract_python_literals(source.encode())


def _ts(source: str, locator: str = "web/api.ts") -> ArtifactExtraction:
    return extract_ts_literals(locator, source.encode())


def _rows(extraction: ArtifactExtraction, kind: str) -> list[tuple[str | None, dict[str, Any]]]:
    return [(row.subject, row.attributes) for row in extraction.literals if row.kind == kind]


def test_python_routers_mounts_and_migrations() -> None:
    extraction = _py(
        "from fastapi import APIRouter, FastAPI\n"
        "PREFIX = '/x'\n"
        "router = APIRouter(prefix='/api/v1/items')\n"
        "other = APIRouter(prefix=PREFIX)\n"
        "spread = APIRouter(**options)\n"
        "def create_app():\n"
        "    app = FastAPI()\n"
        "    app.include_router(router, prefix='/v2')\n"
        "    app.include_router(routes.other)\n"
        "    app.mount('/static', files)\n"
        "    return app\n"
        "def upgrade():\n"
        "    op.create_table('users', sa.Column('id'))\n"
        "    op.create_table(name)\n"
        '"""router = APIRouter(prefix=\'/doc\')"""\n'
        "# app.include_router(comment_router)\n"
    )
    assert extraction.outcome == "extracted"
    routers = dict(_rows(extraction, "app_or_router"))
    assert routers["router"]["prefix"] == {"state": "literal", "value": "/api/v1/items"}
    assert routers["other"]["prefix"] == {"state": "opaque"}
    assert routers["spread"]["prefix"] == {"state": "opaque"}
    assert routers["app"] == {
        "constructor": "FastAPI",
        "callee": "FastAPI",
        "prefix": {"state": "absent"},
        "scope": "function",
        "owner": "create_app",
    }
    mounts = [attributes for _, attributes in _rows(extraction, "router_mount")]
    assert [(m["method"], m["router"], m["prefix"]) for m in mounts] == [
        ("include_router", "router", {"state": "literal", "value": "/v2"}),
        ("include_router", "routes.other", {"state": "absent"}),
        ("mount", "files", {"state": "literal", "value": "/static"}),
    ]
    assert [subject for subject, _ in _rows(extraction, "migration_table")] == ["users"]
    # Text in docstrings and comments never produces rows.
    assert "/doc" not in json.dumps([row.attributes for row in extraction.literals])


def test_python_import_guards_are_syntactic_only() -> None:
    extraction = _py(
        "import plain\n"
        "from typing import TYPE_CHECKING as TC\n"
        "import typing\n"
        "if TYPE_CHECKING:\n"
        "    import checked\n"
        "else:\n"
        "    import fallback\n"
        "if typing.TYPE_CHECKING:\n"
        "    import checked_attr\n"
        "if TC:\n"
        "    import aliased\n"
        "if not TYPE_CHECKING:\n"
        "    import negated\n"
        "try:\n"
        "    import fast\n"
        "except ImportError:\n"
        "    import slow\n"
        "class C:\n"
        "    if flag:\n"
        "        import in_class\n"
        "def f():\n"
        "    if flag:\n"
        "        import in_function\n"
    )
    guards = {
        row.span.start_line: row.attributes["guard"]
        for row in extraction.literals
        if row.kind == "import_guard"
    }
    assert guards == {
        5: "type_checking",
        7: "conditional",
        9: "type_checking",
        11: "conditional",  # aliased sentinels are never trusted as TYPE_CHECKING
        13: "conditional",
        15: "conditional",
        17: "conditional",
        20: "conditional",
    }
    assert extraction.artifact_facts and extraction.artifact_facts["classification_complete"]


def test_python_opaque_dynamic_imports_record_no_argument_text() -> None:
    extraction = _py(
        "import importlib\n"
        "importlib.import_module(secret_name)\n"
        "importlib.import_module('literal.module')\n"
        "__import__(name)\n"
    )
    rows = _rows(extraction, "opaque_dynamic_import")
    assert [attributes["form"] for _, attributes in rows] == [
        "importlib.import_module",
        "__import__",
    ]
    assert "secret_name" not in json.dumps([row.attributes for row in extraction.literals])


def test_python_unsafe_prefix_is_dropped_and_counted() -> None:
    extraction = _py("router = APIRouter(prefix='/a\u202eb')\n")
    assert _rows(extraction, "app_or_router")[0][1]["prefix"] == {"state": "sanitized_out"}
    assert extraction.artifact_facts and extraction.artifact_facts["sanitized_out"] == 1
    assert "\u202e" not in json.dumps([row.attributes for row in extraction.literals])


def test_python_syntax_error_fails_only_the_pass() -> None:
    extraction = _py("def broken(:\n")
    assert extraction.outcome == "failed"
    assert extraction.diagnostics[0].code == "SYNTAX_ERROR"


def test_ts_sinks_methods_and_wrapper_context() -> None:
    extraction = _ts(
        "const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000/api/v1'\n"
        "export async function request<T>(path: string, init?: RequestInit): Promise<T> {\n"
        "  const response = await fetch(`${apiBaseUrl}${path}`, { headers: {}, ...init })\n"
        "  return response.json()\n"
        "}\n"
        "export const remove = (id: string) => fetch(`/items/${id}`, { method: 'patch' })\n"
        "function guarded(flag: boolean) {\n"
        "  if (flag) { fetch('/a') }\n"
        "  items.forEach(() => window.fetch('/b'))\n"
        "}\n"
        "axios.get('/c', { params: {} })\n"
        "axios.post('/d', body, { method: 'put' })\n"
        "globalThis.fetch(url, init)\n"
        "// fetch('/comment')\n"
        "const text = \"fetch('/string')\"\n"
    )
    calls = _rows(extraction, "http_call")
    by_line: dict[int | None, Any] = {
        row.span.start_line: row.attributes
        for row in extraction.literals
        if row.kind == "http_call"
    }
    assert len(calls) == 7
    wrapper = by_line[3]
    assert wrapper["function"] == "request"
    assert wrapper["parameters"] == ["path", "init"]
    assert (wrapper["conditional"], wrapper["loop"], wrapper["callback"]) == (False, False, False)
    assert wrapper["url"] == {
        "state": "template",
        "origin": {"kind": "relative_or_unknown"},
        "parts": [{"e": "apiBaseUrl"}, {"e": "path"}],
        "truncated": False,
        "query_dropped": False,
    }
    assert wrapper["init"] == {
        "kind": "object",
        "method": {"state": "absent"},
        "spreads": ["init"],
        "computed": False,
        "keys": ["headers"],
    }
    assert by_line[6]["function"] == "remove"
    assert by_line[6]["init"]["method"] == {"state": "literal", "value": "patch"}  # byte-exact
    assert by_line[8]["conditional"] is True
    assert by_line[9]["callback"] is True
    assert by_line[11]["verb"] == "get" and by_line[11]["init"]["method"] == {"state": "absent"}
    assert by_line[12]["init"]["method"] == {"state": "literal", "value": "put"}
    assert by_line[13]["init"] == {"kind": "identifier", "name": "init"}
    constant = dict(_rows(extraction, "url_constant"))["apiBaseUrl"]
    assert constant["operator"] == "??"
    assert constant["primary"] == "import_meta.env.VITE_API_BASE_URL"
    assert constant["value"] == {
        "origin": {"kind": "absolute", "scheme": "http", "host": "127.0.0.1", "port": "8000"},
        "parts": [{"s": "/api/v1"}],
        "truncated": False,
        "query_dropped": False,
    }
    assert "/comment" not in json.dumps([r.attributes for r in extraction.literals])


def test_ts_url_usage_without_a_sink_is_only_url_call() -> None:
    extraction = _ts(
        "navigate('/api/v1/projects')\n"
        "logger.info('/api/v1/x')\n"
        "request(`${base(p, s)}/catalog`, { method: 'POST' })\n"
        "t('hello world')\n"
        "notAUrl('/has space')\n"
    )
    assert _rows(extraction, "http_call") == []
    calls = _rows(extraction, "url_call")
    assert [subject for subject, _ in calls] == ["navigate", "logger.info", "request"]
    request = calls[2][1]
    assert request["url"]["parts"] == [{"e": "base(p, s)"}, {"s": "/catalog"}]
    assert request["objects"] == {
        "1": {
            "kind": "object",
            "method": {"state": "literal", "value": "POST"},
            "spreads": [],
            "computed": False,
            "keys": ["method"],
        }
    }


def test_ts_url_sanitisation_drops_userinfo_query_and_fragment() -> None:
    extraction = _ts(
        "fetch('https://user:hunter2@API.Example.com:8443/x/y?token=abc#frag')\n"
        "fetch('/ok\u202e/evil')\n"
        "fetch(`http://${host}/path`)\n"
    )
    stored = json.dumps([row.attributes for row in extraction.literals])
    for secret in ("user", "hunter2", "token", "abc", "frag", "\u202e"):
        assert secret not in stored
    rows: list[Any] = [row.attributes["url"] for row in extraction.literals]
    assert rows[0] == {
        "state": "template",
        "origin": {
            "kind": "absolute",
            "scheme": "https",
            "host": "api.example.com",
            "port": "8443",
        },
        "parts": [{"s": "/x/y"}],
        "truncated": False,
        "query_dropped": True,
    }
    assert rows[1] == {"state": "sanitized_out"}
    assert rows[2]["origin"] == {
        "kind": "absolute_opaque_host",
        "scheme": "http",
        "host": None,
        "port": None,
    }
    assert extraction.artifact_facts and extraction.artifact_facts["sanitized_out"] == 1


def test_ts_routes_helpers_guards_type_imports_and_opaque_imports() -> None:
    extraction = _ts(
        "import type { A } from './a'\n"
        "import { type B } from './b'\n"
        "if (process.env.X) { require('./optional') }\n"
        "const lazy = require(name)\n"
        "const page = import(`./pages/${p}`)\n"
        "const base = (projectId: string) => `/projects/${projectId}/semantic`\n"
        "function plain() { return 'not a url' }\n"
        "export const routes = <Routes>\n"
        '  <Route path="/explore" element={<ExplorePage />} />\n'
        "  <Route index element={<Home />} />\n"
        "  <Route path={dynamic} element={<Other />} />\n"
        "</Routes>\n",
        "web/App.tsx",
    )
    assert [row.span.start_line for row in extraction.literals if row.kind == "type_import"] == [1]
    guards = _rows(extraction, "import_guard")
    assert [attributes["form"] for _, attributes in guards] == ["require"]
    opaque = [attributes["form"] for _, attributes in _rows(extraction, "opaque_dynamic_import")]
    assert opaque == ["require", "dynamic_import"]
    helpers = dict(_rows(extraction, "url_helper"))
    assert set(helpers) == {"base"}
    assert helpers["base"]["parameters"] == ["projectId"]
    routes = _rows(extraction, "frontend_route")
    assert routes == [
        (
            "ExplorePage",
            {"path": {"state": "literal", "value": "/explore"}, "index": False, "parent": None},
        ),
        ("Home", {"path": {"state": "absent"}, "index": True, "parent": None}),
        ("Other", {"path": {"state": "opaque"}, "index": False, "parent": None}),
    ]
    nested = _ts(
        'const r = <Route path="/lab" element={<Lab />}>\n'
        "  <Route index element={<Overview />} />\n"
        '  <Route path="runs" element={<Runs />}>\n'
        '    <Route path=":id" element={<Run />} />\n'
        "  </Route>\n"
        "</Route>\n",
        "web/Nested.tsx",
    )
    parents = {s: (a["parent"], a["path"].get("value")) for s, a in _rows(nested, "frontend_route")}
    lab = next(r.ordinal for r in nested.literals if r.subject == "Lab")
    runs = next(r.ordinal for r in nested.literals if r.subject == "Runs")
    assert parents == {
        "Lab": (None, "/lab"),
        "Overview": (lab, None),
        "Runs": (lab, "runs"),
        "Run": (runs, ":id"),
    }


def test_ts_caps_and_syntax_recovery_make_the_pass_partial() -> None:
    many = "".join(f"fetch('/item/{index}')\n" for index in range(ROWS_PER_ARTIFACT + 10))
    capped = _ts(many)
    assert capped.outcome == "partial"
    assert len(capped.literals) == ROWS_PER_ARTIFACT
    assert capped.artifact_facts and capped.artifact_facts["dropped_rows"] == 10
    broken = _ts("fetch('/a')\nconst = = ;\nfetch('/b')\n")
    assert broken.outcome == "partial"
    assert [row.span.start_line for row in broken.literals] == [1, 3]
    assert broken.artifact_facts and broken.artifact_facts["classification_complete"] is False


def test_url_shape_and_template_rules() -> None:
    assert url_shaped([("s", "/a")])
    assert url_shaped([("e", "base"), ("s", "/a")])
    assert not url_shaped([("e", "a"), ("e", "b")])
    assert not url_shaped([("s", "a/b")])
    assert url_template([("s", "//cdn.example.org/x")])["origin"] == {
        "kind": "absolute",
        "scheme": None,
        "host": "cdn.example.org",
        "port": None,
    }
    loopback = url_template([("s", "http://[::1]:8000/api")])
    assert loopback["origin"] == {
        "kind": "absolute",
        "scheme": "http",
        "host": "[::1]",
        "port": "8000",
    }
    assert loopback["parts"] == [{"s": "/api"}]


def test_ts_context_survives_try_and_if_blocks() -> None:
    extraction = _ts(
        "export async function req(p: string) {\n"
        "  try { return await fetch(`${B}${p}`) } catch (e) { throw e }\n"
        "}\n"
    )
    row = next(r.attributes for r in extraction.literals if r.kind == "http_call")
    assert row["function"] == "req" and row["parameters"] == ["p"]
    assert row["conditional"] is False and row["callback"] is False


def _scopes(source: str, locator: str = "web/api.ts") -> dict[int | None, str]:
    result: dict[int | None, str] = {}
    for row in _ts(source, locator).literals:
        if row.kind in {"http_call", "url_call"}:
            scope = row.attributes["callee_scope"]
            assert isinstance(scope, str)
            result[row.span.start_line] = scope
    return result


def test_ts_callee_scope_sees_parameters_and_local_declarations() -> None:
    scopes = _scopes(
        "export function inspect(fetch: (path: string) => string) {\n"  # 1
        "  return fetch('/api/users')\n"  # 2 parameter
        "}\n"
        "export const arrow = (fetch: any) => fetch('/api/users')\n"  # 4 arrow parameter
        "export function f({ axios }: any, [window]: any[]) {\n"  # 5
        "  axios.get('/api/users'); window.fetch('/api/users')\n"  # 6 destructured
        "}\n"
        "export function g() { const globalThis = mock; return globalThis.fetch('/api/x') }\n"  # 8
        "export function h() { if (x) { var fetch = mock }; return fetch('/api/users') }\n"  # 9
        "export function k() { try {} catch (fetch) { fetch('/api/users') } }\n"  # 10
        "export function m() { for (const fetch of fns) { fetch('/api/users') } }\n"  # 11
        "export function n() { { const fetch = mock } return fetch('/api/users') }\n"  # 12 control
        "export function p(request: any) { return request('/api/users') }\n"  # 13 url_call
        "export const q = () => fetch('/api/users')\n"  # 14 control
        "axios.get('/api/users'); window.fetch('/api/users')\n"  # 15 control
        "function named() { return named('/api/users') }\n"  # 16 module-level function name
    )
    assert scopes == {
        2: "local", 4: "local", 6: "local", 8: "local", 9: "local", 10: "local", 11: "local",
        12: "none", 13: "local", 14: "none", 15: "none", 16: "module",
    }  # fmt: skip


def test_ts_callee_scope_module_declarations_and_with() -> None:
    scopes = _scopes("const axios = make()\naxios.get('/api/users')\nfetch('/api/users')\n")
    assert scopes == {2: "module", 3: "none"}
    sloppy = _scopes("with (obj) { fetch('/api/users') }\n", "web/old.js")
    assert sloppy == {1: "unknown"}
