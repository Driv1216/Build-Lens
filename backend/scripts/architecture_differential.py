"""Stage 14 lab-only differential oracle for module edges (``architecture-evaluation.v1``).

Independent of BuildLens: it imports nothing from ``buildlens`` and reads the archive itself.
Python import statements are enumerated with ``ast`` and resolved with
``importlib.machinery.PathFinder.find_spec`` over the pre-registered roots only (a filesystem
search; nothing is imported or executed). TS/JS specifiers are enumerated and resolved by the
TypeScript compiler API in ``architecture_differential.mjs``. The output is the oracle file the
scorer compares with ``buildlens architecture eval module-units``. This is verification
tooling, not a production code path; run it with ``python -I`` on a read-only archive.

Usage (from backend/):
    .venv/bin/python -I scripts/architecture_differential.py <archive-root> \\
        --preregistration <prereg.json> --typescript ../frontend/node_modules/typescript \\
        --out <oracle.json>
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from importlib.machinery import ModuleSpec, PathFinder
from pathlib import Path
from typing import Any

PYTHON = (".py",)
TYPESCRIPT = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
ALIAS = ("@/", "~/", "#", "/")


def tree_sha256(root: Path) -> str:
    """sha256 over sorted ``<sha256(content)>  <relative path>`` lines of every file."""
    lines = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root)}")
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


class PythonOracle:
    def __init__(self, root: Path, roots: list[str]) -> None:
        self.root = root
        self.roots = [str(root / item) for item in roots]
        self.parse_failures: list[str] = []

    def _relative(self, origin: str | None) -> str | None:
        if origin is None:
            return None
        path = Path(origin).resolve()
        return str(path.relative_to(self.root)) if path.is_relative_to(self.root) else None

    def _spec(self, dotted: str, search: list[str]) -> ModuleSpec | None:
        spec: ModuleSpec | None = None
        parts = dotted.split(".")
        for index in range(len(parts)):
            spec = PathFinder.find_spec(".".join(parts[: index + 1]), search)
            if spec is None:
                return None
            search = list(spec.submodule_search_locations or [])
            if not search and index < len(parts) - 1:
                return None
        return spec

    def absolute(self, dotted: str, name: str | None) -> dict[str, Any]:
        top = PathFinder.find_spec(dotted.split(".")[0], self.roots)
        if top is None:
            return {"label": "external", "targets": []}
        if top.origin is None:  # namespace portion only: a regular package elsewhere wins
            return {"label": "external", "targets": [], "note": "PEP420_NAMESPACE_ONLY"}
        return self._resolve(dotted, name, self.roots)

    def relative(self, locator: str, level: int, module: str, name: str) -> dict[str, Any]:
        base = Path(locator).parent
        for _ in range(level - 1):
            base = base.parent
        directory = self.root / base
        if module:
            spec = self._spec(module, [str(directory)])
            if spec is None:
                return {"label": "none", "targets": []}
            return self._from(spec, module, name, [str(directory)])
        init = directory / "__init__.py"
        sub = PathFinder.find_spec(name, [str(directory)]) if name != "*" else None
        if sub is not None and sub.origin is not None:
            return {"label": "module", "targets": [self._relative(sub.origin)]}
        if init.is_file():
            return {"label": "module", "targets": [str(init.relative_to(self.root))]}
        return {"label": "none", "targets": []}

    def _from(
        self, spec: ModuleSpec, dotted: str, name: str | None, search: list[str]
    ) -> dict[str, Any]:
        if name and name != "*" and spec.submodule_search_locations:
            sub = PathFinder.find_spec(f"{dotted}.{name}", list(spec.submodule_search_locations))
            if sub is not None and sub.origin is not None:
                return {"label": "module", "targets": [self._relative(sub.origin)]}
        target = self._relative(spec.origin)
        if target is None:
            return {"label": "none", "targets": [], "note": "NAMESPACE_TARGET"}
        return {"label": "module", "targets": [target]}

    def _resolve(self, dotted: str, name: str | None, search: list[str]) -> dict[str, Any]:
        spec = self._spec(dotted, search)
        if spec is None:
            return {"label": "none", "targets": []}
        return self._from(spec, dotted, name, search)

    def units(self, locator: str) -> list[dict[str, Any]]:
        try:
            tree = ast.parse((self.root / locator).read_bytes(), filename=locator)
        except (SyntaxError, ValueError):
            self.parse_failures.append(locator)
            return []
        found: list[tuple[tuple[int, int, int], tuple[Any, ...], dict[str, Any]]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for index, alias in enumerate(node.names):
                    key = ("import", 0, "", alias.name)
                    found.append(
                        (
                            (node.lineno, node.col_offset, index),
                            key,
                            self.absolute(alias.name, None),
                        )
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for index, alias in enumerate(node.names):
                    key = ("from", node.level, module, alias.name)
                    value = (
                        self.relative(locator, node.level, module, alias.name)
                        if node.level
                        else self.absolute(module, alias.name)
                    )
                    found.append(((node.lineno, node.col_offset, index), key, value))
        found.sort(key=lambda item: item[0])
        seen: Counter[tuple[Any, ...]] = Counter()
        values = []
        for _, key, value in found:
            seen[key] += 1
            unit = "|".join(str(part) for part in (locator, *key, seen[key]))
            values.append({"unit": unit, "locator": locator, "language": "python", **value})
        return values


def typescript_units(
    root: Path, files: list[str], package: Path, options: dict[str, Any]
) -> tuple[str, list[dict[str, Any]]]:
    if not files:
        return "", []
    result = json.loads(
        subprocess.run(
            [
                "node",
                str(Path(__file__).with_name("architecture_differential.mjs")),
                str(package.resolve()),
                str(root),
                json.dumps(options),
                *[str(root / locator) for locator in files],
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    values = []
    for locator in files:
        seen: Counter[tuple[str, str]] = Counter()
        for item in result["files"][locator]:
            key = (item["kind"], item["specifier"])
            seen[key] += 1
            values.append(
                {
                    "unit": "|".join(str(part) for part in (locator, *key, seen[key])),
                    "locator": locator,
                    "language": "typescript",
                    "alias": item["specifier"].startswith(ALIAS),
                    "label": item["label"],
                    "targets": item["targets"],
                    **({"note": item["note"]} if "note" in item else {}),
                }
            )
    return str(result["typescript"]), values


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--preregistration", type=Path, required=True)
    parser.add_argument("--typescript", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    prereg = json.loads(args.preregistration.read_text())
    tree = tree_sha256(root)
    if tree != prereg["corpus"]["tree_sha256"]:
        sys.exit(f"archive tree {tree} differs from the pre-registered corpus")
    locators = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    python = PythonOracle(root, prereg["oracle"]["python_roots"])
    units: list[dict[str, Any]] = []
    for locator in locators:
        if locator.endswith(PYTHON):
            units.extend(python.units(locator))
    version, ts_units = typescript_units(
        root,
        [locator for locator in locators if locator.endswith(TYPESCRIPT)],
        args.typescript,
        prereg["oracle"]["typescript_options"],
    )
    if version != prereg["oracle"]["typescript_version"]:
        sys.exit(f"TypeScript {version} differs from the pre-registered version")
    units.extend(ts_units)
    report = {
        "version": "architecture-differential.v1",
        "corpus_tree_sha256": tree,
        "python": sys.version.split()[0],
        "typescript": version,
        "python_roots": prereg["oracle"]["python_roots"],
        "typescript_options": prereg["oracle"]["typescript_options"],
        "python_parse_failures": python.parse_failures,
        "units": units,
        "labels": dict(Counter(unit["label"] for unit in units)),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.out.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    os.replace(temporary, args.out)
    print(json.dumps({key: report[key] for key in ("corpus_tree_sha256", "labels")}, indent=1))


if __name__ == "__main__":
    main()
