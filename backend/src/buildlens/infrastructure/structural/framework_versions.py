"""Stage 15 ``framework-version-facts.v1``: package versions from lockfiles and manifests (D17).

Version files are parsed as **data** (``tomllib``, ``json``, ``yaml.safe_load``) inside the
supervised worker; nothing is executed, installed or resolved. Only the versions of the fixed
package list in ``domain.flow_evidence.VERSION_PACKAGES`` are extracted, so an arbitrary file
cannot inject other facts. An unparseable file yields no facts (the caller records it).
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from typing import Any

import yaml

from buildlens.domain.flow_evidence import VERSION_PACKAGES, FrameworkFact

_PYTHON = set(VERSION_PACKAGES["python"])
_NPM = set(VERSION_PACKAGES["npm"])
_REQUIREMENT = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(==|===|>=|<=|~=|!=|>|<)?\s*([^;#\s]*)"
)
_PIN = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*===?\s*([A-Za-z0-9.+!_-]+)\s*(?:[;#].*)?$"
)


class VersionFileError(Exception):
    pass


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


Row = tuple[str, str | None, str | None, str]  # (name, version, constraint, scope)


def _python_facts(
    rows: list[tuple[str, str | None, str | None]], kind: str, locator: str, fp: str
) -> list[FrameworkFact]:
    seen: set[str] = set()
    facts: list[FrameworkFact] = []
    for name, version, constraint in rows:
        package = _normalise(name)
        if package in _PYTHON and package not in seen:
            seen.add(package)
            facts.append(FrameworkFact("python", package, version, constraint, kind, locator, fp))  # type: ignore[arg-type]
    return facts


def _npm_facts(rows: list[Row], kind: str, locator: str, fp: str) -> list[FrameworkFact]:
    """One fact per (package, workspace scope); an npm alias (``npm:x@1``) keeps its text."""
    seen: set[tuple[str, str]] = set()
    facts: list[FrameworkFact] = []
    for name, version, constraint, scope in rows:
        if name in _NPM and (name, scope) not in seen:
            seen.add((name, scope))
            facts.append(FrameworkFact("npm", name, version, constraint, kind, locator, fp, scope))  # type: ignore[arg-type]
    return facts


def _toml_packages(data: dict[str, Any]) -> list[tuple[str, str | None, str | None]]:
    rows: list[tuple[str, str | None, str | None]] = []
    for item in data.get("package") or []:
        if (
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("version"), str)
        ):
            rows.append((item["name"], item["version"], None))
    return rows


def _requirement_rows(lines: list[str]) -> list[tuple[str, str | None, str | None]]:
    rows: list[tuple[str, str | None, str | None]] = []
    for line in lines:
        if not line.strip() or line.lstrip().startswith(("#", "-")):
            continue
        pin = _PIN.match(line)
        if pin:
            rows.append((pin.group(1), pin.group(2), None))
            continue
        match = _REQUIREMENT.match(line)
        if match:
            constraint = (match.group(2) or "") + (match.group(3) or "")
            rows.append((match.group(1), None, constraint or None))
    return rows


def _pnpm_rows(data: dict[str, Any]) -> list[Row]:
    """Resolved versions per workspace importer (the ``packages`` section is not attributed)."""
    rows: list[Row] = []
    for scope, importer in (data.get("importers") or {}).items():
        if not isinstance(importer, dict):
            continue
        for section in ("dependencies", "devDependencies"):
            for name, value in (importer.get(section) or {}).items():
                version = value.get("version") if isinstance(value, dict) else value
                if isinstance(version, str):
                    rows.append(
                        (name, version.split("(", 1)[0], None, "" if scope == "." else str(scope))
                    )
    return rows


def parse_version_file(locator: str, data: bytes) -> tuple[FrameworkFact, ...]:
    """Facts from one version file; raises :class:`VersionFileError` when it cannot be read."""
    fingerprint = hashlib.sha256(data).hexdigest()
    name = locator.rsplit("/", 1)[-1].lower()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise VersionFileError("ENCODING_ERROR") from error
    try:
        if name in {"uv.lock", "poetry.lock", "pdm.lock"}:
            return tuple(
                _python_facts(_toml_packages(tomllib.loads(text)), "lockfile", locator, fingerprint)
            )
        if name.startswith("requirements") and name.endswith(".txt"):
            rows = _requirement_rows(text.splitlines())
            facts = _python_facts(
                [r for r in rows if r[1]], "requirements_pin", locator, fingerprint
            )
            pinned = {f.package for f in facts}
            facts += [
                f
                for f in _python_facts(
                    [r for r in rows if not r[1]], "declared", locator, fingerprint
                )
                if f.package not in pinned
            ]
            return tuple(facts)
        if name == "pyproject.toml":
            project = tomllib.loads(text).get("project") or {}
            values = [d for d in project.get("dependencies") or [] if isinstance(d, str)]
            for extra in (project.get("optional-dependencies") or {}).values():
                values += [d for d in extra if isinstance(d, str)]
            return tuple(_python_facts(_requirement_rows(values), "declared", locator, fingerprint))
        if name == "package-lock.json":
            document = json.loads(text)
            npm_rows: list[Row] = []
            for key, value in (document.get("packages") or {}).items():
                if not (
                    isinstance(key, str) and isinstance(value, dict) and "node_modules/" in key
                ):
                    continue
                prefix, package = key.rsplit("node_modules/", 1)
                # Only a workspace's direct installation, never a nested dependency's copy.
                if isinstance(value.get("version"), str) and "node_modules/" not in prefix:
                    npm_rows.append((package, value["version"], None, prefix.rstrip("/")))
            for package, value in (document.get("dependencies") or {}).items():  # lockfile v1
                if isinstance(value, dict) and isinstance(value.get("version"), str):
                    npm_rows.append((package, value["version"], None, ""))
            return tuple(_npm_facts(npm_rows, "lockfile", locator, fingerprint))
        if name == "pnpm-lock.yaml":
            # pnpm v9 lockfiles may hold several YAML documents; every mapping is read.
            pnpm_rows: list[Row] = []
            for document in yaml.safe_load_all(text):
                if isinstance(document, dict):
                    pnpm_rows += _pnpm_rows(document)
            return tuple(_npm_facts(pnpm_rows, "lockfile", locator, fingerprint))
        if name == "package.json":
            document = json.loads(text)
            declared: list[Row] = []
            for section in ("dependencies", "devDependencies", "peerDependencies"):
                for package, constraint in (document.get(section) or {}).items():
                    if isinstance(constraint, str):
                        declared.append((package, None, constraint, ""))
            return tuple(_npm_facts(declared, "declared", locator, fingerprint))
    except (
        tomllib.TOMLDecodeError,
        json.JSONDecodeError,
        yaml.YAMLError,
        AttributeError,
        TypeError,
    ) as error:
        raise VersionFileError("UNPARSEABLE") from error
    raise VersionFileError("UNSUPPORTED_VERSION_FILE")


def is_version_file(locator: str) -> bool:
    name = locator.rsplit("/", 1)[-1].lower()
    return name in {
        "uv.lock",
        "poetry.lock",
        "pdm.lock",
        "pyproject.toml",
        "package-lock.json",
        "pnpm-lock.yaml",
        "package.json",
    } or (name.startswith("requirements") and name.endswith(".txt"))
