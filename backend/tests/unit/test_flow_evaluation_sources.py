"""Source-only evaluation enumeration must distinguish nested calls with a shared start."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.flow_evaluate import python_index


def test_python_nested_call_identity_includes_its_end_position(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def run():\n    return factory().method('é')\n")
    rows = python_index(tmp_path, ["app.py"])["calls"]
    assert len(rows) == 2
    assert len({(r["line"], r["column"]) for r in rows}) == 1
    assert len({(r["line"], r["column"], r["end_line"], r["end_column"]) for r in rows}) == 2
    assert {r["callee"] for r in rows} == {"factory", "factory().method"}


def test_typescript_checker_enumerates_without_executing_repository_source(tmp_path: Path) -> None:
    root = Path(__file__).parents[3]
    package = root / "frontend/node_modules/typescript"
    if not shutil.which("node") or not package.is_dir():
        pytest.skip("Lab differential tool requires Node and the frontend TypeScript package")
    (tmp_path / "app.ts").write_text(
        "throw new Error('source must never execute');\n"
        "function run() { return factory().method('é'); }\n"
    )
    locators = tmp_path / "files.json"
    locators.write_text('["app.ts"]')
    output = subprocess.check_output(
        [
            "node",
            str(root / "backend/scripts/flow_source_index.mjs"),
            str(package),
            str(tmp_path),
            str(locators),
        ],
        text=True,
        timeout=30,
    )
    data = json.loads(output)
    assert data["version"] == "5.7.3"
    rows = [r for r in data["calls"] if r["callee"] in {"factory", "factory().method"}]
    assert len(rows) == 2
    assert len({(r["line"], r["column"]) for r in rows}) == 1
    assert len({(r["line"], r["column"], r["end_line"], r["end_column"]) for r in rows}) == 2
