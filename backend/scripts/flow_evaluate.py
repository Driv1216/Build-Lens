"""Reproducible Part F lab driver. See docs/FLOW_EVALUATION.md for execution and provenance.

Preparation independently enumerates archived source; it never consults flow model output.
Observation requires complete digest-bound labels and reads production traces from PostgreSQL.
All subprocess execution is fixed tool code, never code in a benchmark repository.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import hashlib
import json
import os
import random
import subprocess
import tarfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from buildlens.application.flow_evaluation import (
    FACETS,
    LABELS_VERSION,
    PACKET_VERSION,
    bound,
    power_calculation,
    preregistration,
    require_preregistration,
    score,
    validate_labels,
)
from buildlens.domain.source import ContentRejectedError, screen_content

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
SKIP = {".git", "node_modules", ".venv", "venv", "dist", ".next", "__pycache__", ".buildlens-eval"}


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ValueError(f"Refusing to replace frozen evidence: {path}")
    encoded = (
        json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
            default=lambda x: (
                dataclasses.asdict(x)
                if dataclasses.is_dataclass(x) and not isinstance(x, type)
                else str(x)
            ),
        )
        + "\n"
    )
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(encoded)
    temporary.rename(path)


def load(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text()))


def files(root: Path) -> dict[str, str]:
    result = {}
    for parent, directories, names in os.walk(root, followlinks=False):
        directories[:] = sorted(
            d for d in directories if d not in SKIP and not (Path(parent) / d).is_symlink()
        )
        for name in sorted(names):
            path = Path(parent) / name
            if path.is_symlink() or not path.is_file():
                continue
            locator = path.relative_to(root).as_posix()
            result[locator] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def target_id(target: dict[str, Any]) -> str:
    return f"{target['locator']}:{target['line']}:{target['name']}"


def python_index(root: Path, locators: list[str]) -> dict[str, Any]:
    calls, functions, errors = [], [], []
    for locator in locators:
        raw = (root / locator).read_bytes()
        try:
            tree = ast.parse(raw, filename=locator)
        except (SyntaxError, ValueError, RecursionError):
            errors.append(locator)
            continue

        def visit(
            node: ast.AST, owner: dict[str, Any] | None = None, locator: str = locator
        ) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = {"locator": locator, "line": node.lineno, "name": node.name}
                functions.append({**owner, "end_line": node.end_lineno})
            if isinstance(node, ast.Call):
                func = node.func
                calls.append(
                    {
                        "locator": locator,
                        "line": node.lineno,
                        "column": node.col_offset,
                        "end_line": node.end_lineno,
                        "end_column": node.end_col_offset,
                        "oracle_column": func.end_col_offset,
                        "oracle_line": func.end_lineno,
                        "language": "python",
                        "form": "direct"
                        if isinstance(func, ast.Name)
                        else "attribute"
                        if isinstance(func, ast.Attribute)
                        else "dynamic",
                        "callee": ast.unparse(func),
                        "owner": owner,
                    }
                )
            for child in ast.iter_child_nodes(node):
                visit(child, owner)

        visit(tree)
    return {"calls": calls, "functions": functions, "parse_errors": errors}


def sample_calls(calls: list[dict[str, Any]], n: int, rng: random.Random) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(calls, key=lambda r: (r["locator"], r["line"], r["column"])):
        groups[row["form"]].append(row)
    for group in groups.values():
        rng.shuffle(group)
    result = []
    while len(result) < n and any(groups.values()):
        for key in sorted(groups):
            if groups[key] and len(result) < n:
                result.append(groups[key].pop())
    return result


def jedi_oracle(root: Path, selected: list[dict[str, Any]], roots: list[str]) -> None:
    import jedi  # type: ignore[import-not-found]

    jedi.settings.cache_directory = "/private/tmp/buildlens-f-jedi-cache"

    project = jedi.Project(
        str(root),
        added_sys_path=[str(root / p) for p in roots],
        load_unsafe_extensions=False,
        smart_sys_path=False,
    )
    scripts: dict[str, Any] = {}
    for call in selected:
        locator = call["locator"]
        if locator not in scripts:
            scripts[locator] = jedi.Script(path=root / locator, project=project)
        try:
            candidates = scripts[locator].infer(call["oracle_line"], call["oracle_column"])
            targets = []
            external = False
            for candidate in candidates:
                path = Path(candidate.module_path).resolve() if candidate.module_path else None
                if (
                    path
                    and path.is_relative_to(root.resolve())
                    and candidate.type in {"function", "class"}
                ):
                    targets.append(
                        {
                            "locator": path.relative_to(root.resolve()).as_posix(),
                            "line": candidate.line,
                            "name": candidate.name,
                        }
                    )
                elif candidate.type in {"function", "class"}:
                    external = True
            state = (
                "internal"
                if targets and not external
                else "external"
                if external and not targets
                else "unknown"
            )
            call["oracle"] = {
                "state": state,
                "targets": targets,
                "note": "Jedi inferred callable declarations; differential proposal only",
            }
        except Exception as error:
            call["oracle"] = {"state": "unknown", "targets": [], "note": type(error).__name__}


def prepare(destination: Path) -> None:
    if destination.exists():
        raise ValueError("Use a new run directory; evidence is immutable")
    destination.mkdir(parents=True)
    corpus_dir = destination / "corpora"
    corpus_dir.mkdir()
    archive = destination / "buildlens.tar"
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    subprocess.run(
        ["git", "archive", "--format=tar", "-o", str(archive), commit], cwd=ROOT, check=True
    )
    self_root = corpus_dir / "buildlens"
    self_root.mkdir()
    with tarfile.open(archive) as handle:
        handle.extractall(self_root, filter="data")
    approved = load(ROOT / ".buildlens-eval/runs/stage15-a0-20261101/manifest.json")["corpora"]
    corpora = [
        {
            "name": "buildlens",
            "root": str(self_root),
            "commit": commit,
            "python_roots": ["backend/src"],
            "license": "local owner-authorized source",
        }
    ]
    for spec in approved:
        # Already owner-approved pinned local archives; no downloads and no git checkout.
        corpora.append(
            {
                "name": spec["name"],
                "root": str(
                    ROOT / ".buildlens-eval/runs/stage15-a0-20261101/corpora" / spec["name"]
                ),
                "commit": spec["commit"],
                "python_roots": ["backend"] if spec["name"].startswith("full") else ["server"],
                "license": spec["licence"],
            }
        )
    for corpus in corpora:
        corpus["files"] = files(Path(corpus["root"]))
        corpus["tree_digest"] = bound(corpus["files"])["digest"]
    prereg = preregistration(
        corpora,
        "Owner instruction 2026-11-12 in current chat: finish Part F autonomously; "
        "act as reviewer. "
        "Amend D12 to AI-assisted exploratory review; do not impersonate a human.",
    )
    save(destination / "preregistration.json", prereg)
    save(destination / "power.json", power_calculation())
    rng = random.Random(1512)
    sampled, indexes, sources = [], {}, {}
    for corpus in corpora:
        root = Path(corpus["root"])
        manifest = corpus["files"]
        py = python_index(root, [f for f in manifest if f.endswith(".py")])
        ts_files = [
            f
            for f in manifest
            if f.endswith((".ts", ".tsx", ".js", ".jsx")) and not f.endswith(".d.ts")
        ]
        list_path = destination / f"{corpus['name']}.ts-files.json"
        save(list_path, ts_files)
        output = subprocess.check_output(
            [
                "node",
                str(BACKEND / "scripts/flow_source_index.mjs"),
                str(ROOT / "frontend/node_modules/typescript"),
                str(root),
                str(list_path),
            ],
            text=True,
        )
        ts = json.loads(output)
        selected_py = sample_calls(py["calls"], 50, rng)
        jedi_oracle(root, selected_py, corpus["python_roots"])
        selected = selected_py + sample_calls(ts["calls"], 50, rng)
        indexes[corpus["name"]] = {
            "functions": py["functions"] + ts["functions"],
            "parse_errors": py["parse_errors"],
            "population": {"python": len(py["calls"]), "typescript": len(ts["calls"])},
        }
        for call in selected:
            call["corpus"] = corpus["name"]
            call["id"] = (
                f"{corpus['name']}:{call['locator']}:{call['line']}:{call['column']}:"
                f"{call['end_line']}:{call['end_column']}"
            )
            sampled.append(call)
            locator = call["locator"]
            content = (root / locator).read_text(errors="replace")
            try:
                screen_content(content)
                sources[f"{corpus['name']}:{locator}"] = {
                    "content": content,
                    "sha256": manifest[locator],
                }
            except ContentRejectedError:
                sources[f"{corpus['name']}:{locator}"] = {
                    "withheld": True,
                    "sha256": manifest[locator],
                }
        print(
            json.dumps(
                {"prepared": corpus["name"], "population": indexes[corpus["name"]]["population"]}
            ),
            flush=True,
        )
    # Oracle proposals are separate from the source-only blind packet.
    save(destination / "differential-proposals.json", {r["id"]: r.get("oracle") for r in sampled})
    packet_calls = [{k: v for k, v in row.items() if k != "oracle"} for row in sampled]
    packet = bound(
        {
            "version": PACKET_VERSION,
            "preregistration_digest": prereg["digest"],
            "calls": packet_calls,
            "cases": [],
            "queries": [],
            "sources": sources,
        }
    )
    save(destination / "packet.calls.json", packet)
    save(destination / "source-index.json", indexes)
    template = bound(
        {
            "version": LABELS_VERSION,
            "packet_digest": packet["digest"],
            "basis": "ai_assisted",
            "reviewer": "Codex source review, owner-authorized; not human",
            "calls": [
                {"id": r["id"], "reviewed": False, "state": None, "targets": [], "reason": ""}
                for r in packet_calls
            ],
            "cases": [],
            "queries": [],
        }
    )
    save(destination / "labels.template.json", template)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    p.add_argument("destination", type=Path)
    p = commands.add_parser("score")
    p.add_argument("directory", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p = commands.add_parser("validate")
    p.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.destination.resolve())
        return
    directory = args.directory
    prereg, packet, labels = [
        load(directory / name) for name in ("preregistration.json", "packet.json", "labels.json")
    ]
    require_preregistration(prereg)
    validate_labels(packet, labels)
    if args.command == "score":
        save(args.out, score(prereg, packet, labels, load(directory / "observations.json")))
    else:
        print(
            json.dumps(
                {
                    "status": "valid",
                    "packet": packet["digest"],
                    "labels": labels["digest"],
                    "facets": FACETS,
                }
            )
        )


if __name__ == "__main__":
    main()
