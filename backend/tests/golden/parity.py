"""Stage 13 golden parity (invariant M3): frozen pre-Stage-13 producer outputs.

The corpus under ``corpus/`` and the ``python-*.json`` files were captured from commit
``816b136`` before any Stage 13 code change. They must never be regenerated to make a
failing test pass: a difference means an existing producer changed and retained Stage 1–12
evidence would silently lose currency. Regeneration is only legitimate together with an
explicit contract version bump.

Runtime-independent results (declarations, spans, chunk boundaries, stable chunk keys) are
compared on every interpreter with the same minor version. ``state_fingerprint`` and
``dependency_fingerprint`` embed the exact interpreter patch version, so they are compared
only on the patch version that produced them.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from buildlens.application.processing import dependency_fingerprint
from buildlens.application.processors import process_source
from buildlens.domain.source import (
    ContentRejectedError,
    decode_source,
    processing_contract,
    structural_contract,
)
from buildlens.domain.structural import ArtifactAnalysis
from buildlens.infrastructure.structural.parser import PythonAstParser

ROOT = Path(__file__).parent
CORPUS = ROOT / "corpus"
NAMESPACE = UUID("6f1d3c55-7d0e-4c55-9a43-5ad7d1f3c113")
PROCESSING_ID = UUID(int=1)
CAPTURED_FROM = "816b136"
# The pre-Stage-13 ``Symbol`` fields. Stage 13 adds optional fields that the frozen Python
# producer must leave at their defaults (asserted separately), so they are not projected here.
LEGACY_SYMBOL_FIELDS = (
    "id",
    "artifact_analysis_id",
    "parent_id",
    "ordinal",
    "kind",
    "name",
    "qualified_name",
    "is_async",
    "declaration_span",
    "full_start_line",
    "signature",
    "decorators",
    "type_parameters",
    "stable_identity_id",
    "continuity_reason",
    "locator",
)


def _locator(name: str) -> str:
    base = name.removesuffix(".src")
    return "pkg/__init__.py" if base == "__init__.py" else "golden/" + base


def _variants() -> list[tuple[str, bytes]]:
    values: list[tuple[str, bytes]] = []
    for path in sorted(CORPUS.glob("*.src")):
        data = path.read_bytes()
        values.append((_locator(path.name), data))
        if path.name in {"edge_cases.py.src", "infrastructure_structural_parser.py.src"}:
            stem = _locator(path.name).removesuffix(".py")
            values.append((stem + "_crlf.py", data.replace(b"\n", b"\r\n")))
            values.append((stem + "_cr.py", data.replace(b"\n", b"\r")))
            values.append((stem + "_bom.py", b"\xef\xbb\xbf" + data))
    latin = "# -*- coding: latin-1 -*-\nNAME = 'café'\n\n\ndef f():\n    return NAME\n"
    values.append(("golden/latin1.py", latin.encode("latin-1")))
    values.append(("golden/astral.md", "# Têtes \U0001f600\n\nbody \U0001f600 text\n".encode()))
    return values


class _Ids:
    def __init__(self) -> None:
        self.names: dict[UUID, str] = {}

    def name(self, value: UUID, label: str) -> str:
        return self.names.setdefault(value, label)


def _plain(value: Any, ids: _Ids) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name), ids) for f in dataclasses.fields(value)}
    if isinstance(value, UUID):
        return ids.names.get(value, "uuid:unmapped")
    if isinstance(value, tuple | list):
        return [_plain(item, ids) for item in value]
    if isinstance(value, dict):
        return {str(k): _plain(v, ids) for k, v in value.items()}
    return value


def _artifact(locator: str, data: bytes) -> dict[str, Any]:
    ids = _Ids()
    suffix = locator.rsplit(".", 1)[-1]
    artifact_anchor = str(uuid5(NAMESPACE, "artifact|" + locator))
    result: dict[str, Any] = {"locator": locator}
    analysis: ArtifactAnalysis | None = None
    if suffix == "py":
        parsed = PythonAstParser().parse(
            analysis_id=UUID(int=2),
            version_id=UUID(int=3),
            artifact_id=UUID(int=4),
            locator=locator,
            data=data,
        )
        symbols = []
        for index, symbol in enumerate(parsed.symbols):
            ids.name(symbol.id, f"symbol:{index}")
            identity = uuid5(NAMESPACE, f"{locator}|{symbol.qualified_name}|{index}")
            ids.name(identity, str(identity))
            symbols.append(dataclasses.replace(symbol, stable_identity_id=identity))
        for index, item in enumerate(parsed.imports):
            ids.name(item.id, f"import:{index}")
        analysis = dataclasses.replace(
            parsed, symbols=tuple(symbols), input_fingerprint="input-fingerprint"
        )
        result["outcome"] = parsed.outcome
        result["encoding"] = parsed.encoding
        result["line_count"] = parsed.line_count
        result["is_package_initializer"] = parsed.is_package_initializer
        result["diagnostics"] = [[d.code, d.message, d.severity] for d in parsed.diagnostics]
        result["symbols"] = [
            {k: _plain(getattr(s, k), ids) for k in LEGACY_SYMBOL_FIELDS} for s in symbols
        ]
        result["imports"] = [_plain(item, ids) for item in parsed.imports]
        result["state_fingerprints"] = [s.state_fingerprint for s in symbols]
        result["dependency_fingerprint"] = (
            dependency_fingerprint(analysis) if parsed.outcome == "parsed" else None
        )
    try:
        source, encoding = decode_source(data, python=suffix == "py")
    except (ContentRejectedError, SyntaxError, UnicodeDecodeError, LookupError) as error:
        result["processing"] = {"error": type(error).__name__}
        return result
    symbols_for_chunks = analysis.symbols if analysis and analysis.outcome == "parsed" else ()
    chunks, diagnostics, contract = process_source(
        source, suffix, PROCESSING_ID, artifact_anchor, symbols_for_chunks, 1024, 30.0
    )
    result["processing"] = {
        "encoding": encoding,
        "contract": _plain(contract, ids),
        "diagnostics": [[d.code, d.severity] for d in diagnostics],
        "chunks": [
            {
                "stable_key": c.stable_key,
                "ordinal": c.ordinal,
                "unit_anchor": c.unit_anchor,
                "part_index": c.part_index,
                "kind": c.kind,
                "content_hash": c.content_hash,
                "span": _plain(c.span, ids),
                "symbol": ids.names.get(c.symbol_id, None) if c.symbol_id else None,
                "symbol_identity": str(c.symbol_identity_id) if c.symbol_identity_id else None,
                "heading_path": list(c.heading_path),
                "json_path": c.json_path,
            }
            for c in chunks
        ],
    }
    return result


def _split(artifact: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    runtime = {
        "state_fingerprints": artifact.pop("state_fingerprints", None),
        "dependency_fingerprint": artifact.pop("dependency_fingerprint", None),
    }
    return artifact, runtime


def _contract_without_runtime(contract: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in contract.items() if k not in {"runtime_version", "runtime"}}


def compute() -> dict[str, Any]:
    independent: dict[str, Any] = {}
    runtime: dict[str, Any] = {}
    for locator, data in _variants():
        value, specific = _split(_artifact(locator, data))
        independent[locator] = value
        runtime[locator] = specific
    return {
        "captured_from": CAPTURED_FROM,
        "python_minor": f"{sys.version_info[0]}.{sys.version_info[1]}",
        "python_patch": sys.version.split()[0],
        "structural_contract": _contract_without_runtime(structural_contract()),
        "processing_contract": _contract_without_runtime(processing_contract()),
        "contract_runtime": {
            "structural": structural_contract()["runtime_version"],
            "processing": processing_contract()["runtime"],
        },
        "independent": independent,
        "runtime_specific": runtime,
    }


def golden_path(minor: str | None = None) -> Path:
    current = f"{sys.version_info[0]}.{sys.version_info[1]}"
    return ROOT / f"python-{minor or current}.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="capture (pre-Stage-13 only)")
    args = parser.parse_args()
    value = compute()
    if args.write:
        path = golden_path()
        if path.exists():
            raise SystemExit(f"{path} exists; golden parity files are never overwritten")
        path.write_text(json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
        print(f"wrote {path}")
    else:
        print(json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False)[:2000])


if __name__ == "__main__":
    main()
