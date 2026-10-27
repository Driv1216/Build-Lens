"""Invariant M3: existing producers stay byte-identical to their pre-Stage-13 outputs."""

import json
import sys

import pytest

from tests.golden.parity import compute, golden_path


def _golden() -> dict[str, object]:
    path = golden_path()
    if not path.exists():
        pytest.fail(f"No golden parity capture for Python {sys.version_info[:2]} ({path.name})")
    value: dict[str, object] = json.loads(path.read_text())
    return value


def test_existing_producer_outputs_are_unchanged() -> None:
    golden = _golden()
    current = compute()
    assert current["structural_contract"] == golden["structural_contract"]
    assert current["processing_contract"] == golden["processing_contract"]
    expected = golden["independent"]
    actual = current["independent"]
    assert isinstance(expected, dict) and isinstance(actual, dict)
    assert sorted(actual) == sorted(expected)
    for locator in expected:
        assert actual[locator] == expected[locator], locator


def test_existing_fingerprints_are_unchanged_on_captured_runtime() -> None:
    golden = _golden()
    if golden["python_patch"] != sys.version.split()[0]:
        pytest.skip(
            f"state/dependency fingerprints embed the interpreter patch version; captured on "
            f"{golden['python_patch']}"
        )
    assert compute()["runtime_specific"] == golden["runtime_specific"]


def test_frozen_python_producer_leaves_stage13_fields_at_defaults() -> None:
    from uuid import uuid4

    from buildlens.infrastructure.structural.parser import PythonAstParser
    from tests.golden.parity import CORPUS

    data = (CORPUS / "edge_cases.py.src").read_bytes()
    parsed = PythonAstParser().parse(
        analysis_id=uuid4(), version_id=uuid4(), artifact_id=uuid4(), locator="x.py", data=data
    )
    assert parsed.language is None and parsed.extractions == ()
    for symbol in parsed.symbols:
        assert (
            symbol.declaration_form,
            symbol.declaration_space,
            symbol.name_source,
            symbol.exported,
            symbol.start_offset,
            symbol.end_offset,
            symbol.additional_spans,
        ) == (None, None, None, None, None, None, ())
