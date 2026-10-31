"""Stage 15 D17: framework version facts from lockfiles and manifests, parsed as data."""

from __future__ import annotations

import json

import pytest

from buildlens.infrastructure.structural.framework_versions import (
    VersionFileError,
    is_version_file,
    parse_version_file,
)


def _facts(locator: str, text: str) -> list[tuple[str, str, str | None, str | None, str]]:
    return [
        (f.scope, f.package, f.version, f.constraint, f.source_kind)
        for f in parse_version_file(locator, text.encode())
    ]


def test_uv_lock_pins_only_listed_packages() -> None:
    text = (
        'version = 1\n\n[[package]]\nname = "fastapi"\nversion = "0.141.1"\n\n'
        '[[package]]\nname = "evil-package"\nversion = "1.0"\n\n'
        '[[package]]\nname = "SQLAlchemy"\nversion = "2.0.52"\n'
    )
    assert _facts("backend/uv.lock", text) == [
        ("", "fastapi", "0.141.1", None, "lockfile"),
        ("", "sqlalchemy", "2.0.52", None, "lockfile"),
    ]


def test_requirements_pins_beat_ranges_and_pyproject_is_declared() -> None:
    text = "# comment\n-r base.txt\nfastapi==0.120.0\nstarlette>=0.40\nunrelated==1\n"
    assert _facts("requirements-dev.txt", text) == [
        ("", "fastapi", "0.120.0", None, "requirements_pin"),
        ("", "starlette", None, ">=0.40", "declared"),
    ]
    pyproject = '[project]\ndependencies = ["fastapi>=0.115,<1", "httpx"]\n'
    assert _facts("pyproject.toml", pyproject) == [("", "fastapi", None, ">=0.115,<1", "declared")]


def test_package_lock_keeps_workspace_scopes_and_ignores_nested_copies() -> None:
    document = {
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "root"},
            "node_modules/react": {"version": "18.3.1"},
            "apps/web/node_modules/react": {"version": "19.3.0"},
            "node_modules/x/node_modules/react": {"version": "16.0.0"},
        },
    }
    assert _facts("package-lock.json", json.dumps(document)) == [
        ("", "react", "18.3.1", None, "lockfile"),
        ("apps/web", "react", "19.3.0", None, "lockfile"),
    ]


def test_pnpm_lock_with_several_documents() -> None:
    text = (
        "lockfileVersion: '9.0'\n---\n"
        "lockfileVersion: '9.0'\nimporters:\n  .:\n    dependencies:\n"
        "      react:\n        specifier: ^19\n        version: 19.2.3\n"
        "  apps/web:\n    dependencies:\n"
        "      '@tanstack/react-query':\n        specifier: ^5\n"
        "        version: 5.104.1(react@19.3.0)\n"
    )
    assert _facts("pnpm-lock.yaml", text) == [
        ("", "react", "19.2.3", None, "lockfile"),
        ("apps/web", "@tanstack/react-query", "5.104.1", None, "lockfile"),
    ]


def test_unparseable_and_unsupported_files_fail_without_facts() -> None:
    with pytest.raises(VersionFileError, match="UNPARSEABLE"):
        parse_version_file("uv.lock", b"[[package]\nname=")
    with pytest.raises(VersionFileError, match="UNSUPPORTED_VERSION_FILE"):
        parse_version_file("setup.cfg", b"[metadata]\n")
    with pytest.raises(VersionFileError, match="ENCODING_ERROR"):
        parse_version_file("package.json", b"\xff\xfe{")


def test_version_file_names() -> None:
    assert is_version_file("a/b/uv.lock") and is_version_file("requirements.txt")
    assert is_version_file("frontend/package.json") and not is_version_file("setup.py")
