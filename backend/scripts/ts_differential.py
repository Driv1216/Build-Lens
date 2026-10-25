"""Stage 13 lab-only differential check of the tree-sitter TS/JS extractor.

Compares declarations (kind + qualified name) and import specifiers with those seen by the
TypeScript compiler's parser (``ts.createSourceFile`` via ``ts_differential.mjs``; no Program,
no tsconfig, nothing executed). Every disagreement is listed; there is no pass-rate summary
that could hide one. This is verification tooling, not a production code path.

Usage (from backend/):
    .venv/bin/python scripts/ts_differential.py ../frontend/src --typescript \\
        ../frontend/node_modules/typescript --report /path/report.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from buildlens.infrastructure.structural.typescript import extract  # noqa: E402

SYNTHETIC = {
    "synthetic/overloads.ts": (
        "export function f(a: string): void;\nexport function f(a: number): void;\n"
        "export function f(a: any) { return a; }\n"
        "class K { m(): void; m(x: number): void; m(x?: any) {} }\n"
    ),
    "synthetic/merging.ts": (
        "interface Box { a: string }\ninterface Box { b: number }\n"
        "namespace NS { export const x = () => 1; }\nnamespace NS { export function y() {} }\n"
        'declare module "ambient" { export function g(): void; }\n'
    ),
    "synthetic/react.tsx": (
        'import { memo, forwardRef } from "react";\n'
        "export default function () { return <div/>; }\n"
        "export const Button = memo(({ label }: { label: string }) => <b>{label}</b>);\n"
        "export const Field = forwardRef(function Field(p, r) { return <input/>; });\n"
        "export function useThing() { useEffect(() => { function inner() {} }); }\n"
        "class View { handle = () => 1; render() { return <Panel/>; } }\n"
    ),
    "synthetic/commonjs.js": (
        'const x = require("x");\nconst lazy = () => import("./lazy");\n'
        "class Legacy extends Base { m() {} static s = () => 2; }\nmodule.exports = { Legacy };\n"
    ),
}


def _ours(path: Path, locator: str) -> dict[str, list[str]]:
    result = extract(locator, path.read_bytes())
    return {
        "declarations": sorted({f"{s.kind} {s.qualified_name}" for s in result.symbols}),
        "imports": sorted({i.from_module for i in result.imports if i.from_module is not None}),
        "outcome": [result.outcome, result.extraction.outcome],  # type: ignore[dict-item]
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--typescript", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as scratch:
        files: list[tuple[Path, str]] = []
        for locator, body in SYNTHETIC.items():
            target = Path(scratch) / locator
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
            files.append((target, locator))
        for path in sorted(args.root.rglob("*")):
            if path.suffix in {".ts", ".tsx", ".js", ".jsx"} and "node_modules" not in path.parts:
                files.append((path, str(path.relative_to(args.root.parent))))
        compiler = json.loads(
            subprocess.run(
                [
                    "node",
                    str(Path(__file__).with_name("ts_differential.mjs")),
                    str(args.typescript.resolve()),
                    *[str(path) for path, _ in files],
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        rows = []
        for (path, locator), theirs in zip(files, compiler, strict=True):
            ours = _ours(path, locator)
            row = {
                "locator": locator,
                "compiler_parse_diagnostics": theirs["parse_diagnostics"],
                "ours_outcome": ours["outcome"],
                "only_tree_sitter": sorted(set(ours["declarations"]) - set(theirs["declarations"])),
                "only_compiler": sorted(set(theirs["declarations"]) - set(ours["declarations"])),
                "imports_only_tree_sitter": sorted(set(ours["imports"]) - set(theirs["imports"])),
                "imports_only_compiler": sorted(set(theirs["imports"]) - set(ours["imports"])),
                "declarations": len(theirs["declarations"]),
            }
            rows.append(row)
    disagreements = [
        row
        for row in rows
        if row["only_tree_sitter"]
        or row["only_compiler"]
        or row["imports_only_tree_sitter"]
        or row["imports_only_compiler"]
    ]
    report = {
        "files": len(rows),
        "compiler_declarations": sum(int(row["declarations"]) for row in rows),  # type: ignore[call-overload]
        "files_with_disagreements": len(disagreements),
        "disagreements": disagreements,
    }
    output = json.dumps(report, indent=2)
    if args.report:
        args.report.write_text(output + "\n")
    print(output)


if __name__ == "__main__":
    main()
