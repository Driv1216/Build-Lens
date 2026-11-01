"""Stage 15 Milestone B rules as pure functions: the FastAPI teardown version table, request
descriptor matching, and module resolution (``ts-path-aliases.v1``, ``ts-workspace-packages.v1``).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from buildlens.application.flow_builder import Facts, _satisfies
from buildlens.application.flow_typescript import _endpoint_matches, _Modules
from buildlens.application.semantic_catalog import ImportResolver
from buildlens.domain.flow_evidence import FrameworkFact, ModuleFact
from buildlens.infrastructure.structural.framework_versions import parse_module_file

BEFORE = {
    "scope": "any",
    "teardown": "before_response_send",
    "background": "after_send_after_teardown",
}
AFTER = {"scope": "any", "teardown": "after_response_send", "background": "before_teardown"}
FUNCTION = {
    "scope": "function",
    "teardown": "before_response_send",
    "background": "after_send_after_teardown",
}
REQUEST = {"scope": "request", "teardown": "after_response_send", "background": "before_teardown"}


def _fact(
    version: str | None, constraint: str | None = None, locator: str = "backend/uv.lock"
) -> FrameworkFact:
    return FrameworkFact(
        "python",
        "fastapi",
        version,
        constraint,
        "lockfile" if version else "declared",
        locator,
        "0" * 64,
    )


def _alternatives(*facts: FrameworkFact, at: str = "backend/app/api.py") -> dict[str, Any]:
    return Facts(SimpleNamespace(framework_facts=facts)).fastapi_alternatives(at)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("version", "rows", "conditions"),
    [
        ("0.105.0", [AFTER], ()),
        ("0.106.0", [BEFORE], ()),
        ("0.117.1", [BEFORE], ()),
        ("0.118.0", [AFTER], ()),
        ("0.120.4", [AFTER], ()),
        ("0.121.0", [FUNCTION, REQUEST], ()),
        ("0.141.1", [FUNCTION, REQUEST], ()),
        ("0.100.0", [AFTER], ("FASTAPI_VERSION_UNVERIFIED",)),
        ("0.160.0", [FUNCTION, REQUEST], ("FASTAPI_VERSION_UNVERIFIED",)),
    ],
)
def test_exact_pins_select_one_verified_row(
    version: str, rows: list[dict[str, str]], conditions: tuple[str, ...]
) -> None:
    value = _alternatives(_fact(version))
    assert value["alternatives"] == rows and value["conditions"] == conditions


def test_declared_ranges_yield_alternatives_only_across_semantics_boundaries() -> None:
    spanning = _alternatives(_fact(None, ">=0.115,<1", "backend/pyproject.toml"))
    assert spanning["alternatives"] == [BEFORE, AFTER, FUNCTION, REQUEST]
    assert spanning["conditions"] == ("FASTAPI_VERSION_DECLARED_RANGE", "SEMANTICS_ALTERNATIVE")
    # Two rows that differ only by version (0.106 and 0.118 boundaries) are alternatives.
    two = _alternatives(_fact(None, ">=0.106,<0.121", "backend/pyproject.toml"))
    assert two["alternatives"] == [BEFORE, AFTER]
    assert "SEMANTICS_ALTERNATIVE" in two["conditions"]
    # One version row: its function/request rows are chosen by scope, not alternatives.
    modern = _alternatives(_fact(None, ">=0.121", "backend/pyproject.toml"))
    assert modern["alternatives"] == [FUNCTION, REQUEST]
    assert modern["conditions"] == ("FASTAPI_VERSION_DECLARED_RANGE",)


def test_unknown_versions_and_fact_precedence() -> None:
    unknown = _alternatives()
    assert unknown["version"] is None and len(unknown["alternatives"]) == 5
    assert unknown["conditions"] == ("FASTAPI_VERSION_UNKNOWN", "SEMANTICS_ALTERNATIVE")
    # A lockfile pin beats a declared range; facts outside the locator's tree are ignored.
    pinned = _alternatives(
        _fact(None, ">=0.115,<1", "backend/pyproject.toml"),
        _fact("0.110.0"),
        _fact("0.141.1", locator="other/uv.lock"),
    )
    assert pinned["version"] == "0.110.0" and pinned["alternatives"] == [BEFORE]


@pytest.mark.parametrize(
    ("version", "constraint", "expected"),
    [
        ((0, 115, 2), ">=0.115,<1", True),
        ((1, 0, 0), ">=0.115,<1", False),
        ((0, 121, 3), "~=0.121.0", True),
        ((0, 122, 0), "~=0.121.0", False),
        ((0, 118, 0), "!=0.118.0", False),
        ((0, 118, 0), "==0.118.0", True),
    ],
)
def test_constraint_satisfaction(
    version: tuple[int, int, int], constraint: str, expected: bool
) -> None:
    assert _satisfies(version, constraint) is expected


def _endpoint(methods: list[str], *paths: str) -> Any:
    return SimpleNamespace(attributes={"methods": methods, "paths": [{"path": p} for p in paths]})


def test_descriptor_paths_match_segments_and_methods() -> None:
    items = _endpoint(["POST"], "/api/items/{item_id}")
    assert _endpoint_matches(items, "POST", "/api/items/{param}")
    assert _endpoint_matches(items, "POST", "/api/items/5")
    assert not _endpoint_matches(items, "GET", "/api/items/5")  # method mismatch: no link
    assert not _endpoint_matches(items, "POST", "/api/items")
    assert not _endpoint_matches(items, "POST", "/api/items/5/extra")
    typed = _endpoint(["GET"], "/api/items/{item_id:int}")
    assert _endpoint_matches(typed, "GET", "/api/items/12")
    assert not _endpoint_matches(typed, "GET", "/api/items/abc")
    nested = _endpoint(["GET"], "/files/{rest:path}")
    assert _endpoint_matches(nested, "GET", "/files/a/b/c")
    assert _endpoint_matches(_endpoint([], "/x"), "DELETE", "/x")  # method unknown: conditioned


def _modules(files: dict[str, Any], sources: list[str]) -> _Modules:
    locators = frozenset([*files, *sources])
    modules = _Modules(ImportResolver(locators), locators)
    for locator, body in files.items():
        raw = body if isinstance(body, str) else json.dumps(body)
        parsed = parse_module_file(locator, raw.encode())
        assert parsed is not None
        fact = ModuleFact(parsed["kind"], locator, "0" * 64, parsed["facts"])
        if fact.kind == "tsconfig":
            modules.tsconfigs[locator] = fact
        else:
            modules.packages[fact.facts["name"]] = fact
    return modules


def test_tsconfig_paths_follow_extends_and_jsonc() -> None:
    modules = _modules(
        {
            "apps/web/tsconfig.json": '{\n  // app config\n  "extends": "./tsconfig.base.json",\n}',
            "apps/web/tsconfig.base.json": {
                "compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}
            },
        },
        ["apps/web/src/client/index.ts", "apps/web/src/App.tsx"],
    )
    app = "apps/web/src/App.tsx"
    assert modules.resolve(app, "@/client") == (
        "apps/web/src/client/index.ts",
        "resolved",
        ("TSCONFIG_PATHS_TRUSTED",),
    )
    assert modules.resolve(app, "@/missing") == (None, "not_found", ("TSCONFIG_PATHS_TRUSTED",))
    assert modules.resolve(app, "react")[0] is None  # bare external specifiers stay external


def test_workspace_packages_map_dist_to_source_only_with_literal_evidence() -> None:
    package = {"name": "@acme/api", "exports": {".": {"import": "./dist/index.js"}}}
    sources = ["packages/api/src/index.ts", "apps/web/src/App.tsx"]
    mapped = _modules(
        {
            "packages/api/package.json": package,
            "packages/api/tsconfig.json": {"compilerOptions": {"rootDir": "src", "outDir": "dist"}},
        },
        sources,
    )
    assert mapped.resolve("apps/web/src/App.tsx", "@acme/api") == (
        "packages/api/src/index.ts",
        "resolved",
        ("WORKSPACE_DIST_TO_SOURCE_MAPPED",),
    )
    unmapped = _modules({"packages/api/package.json": package}, sources)
    assert unmapped.resolve("apps/web/src/App.tsx", "@acme/api") == (
        None,
        "not_found",
        ("WORKSPACE_PACKAGE_NOT_RESOLVED",),
    )
    assert unmapped.resolve("apps/web/src/App.tsx", "@acme/apiary")[0] is None  # not a prefix
