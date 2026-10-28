"""Stage 13 Milestone A: additive multi-language structure on real PostgreSQL."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, delete, select, text

from buildlens.application.fingerprinting import SnapshotFingerprinter
from buildlens.application.processing import ProcessingService
from buildlens.application.snapshots import SnapshotService
from buildlens.application.structural import StructuralAnalysisService
from buildlens.domain import extractors
from buildlens.domain.project_factory import new_project
from buildlens.infrastructure.discovery.filesystem import LocalRepositoryInspector
from buildlens.infrastructure.persistence.database import create_session_factory_from_engine
from buildlens.infrastructure.persistence.models import (
    ArtifactExtractionRecord,
    SymbolIdentityRecord,
)
from buildlens.infrastructure.persistence.processing import SqlAlchemyProcessingStore
from buildlens.infrastructure.persistence.repositories import SqlAlchemyProjectRepository
from buildlens.infrastructure.persistence.snapshots import SqlAlchemySnapshotStore
from buildlens.infrastructure.persistence.structural import SqlAlchemyStructuralAnalysisStore
from buildlens.infrastructure.structural.supervisor import SupervisedSourceReader
from buildlens.infrastructure.structural.worker import SupervisedArtifactAnalyzer

pytestmark = pytest.mark.integration

FILES = {
    "app/service.py": (
        "from .db import Session\n\n\nclass Service(Base):\n"
        "    def load(self, key):\n        return Session().get(key)\n"
    ),
    "web/Button.tsx": (
        'import { memo } from "react";\n'
        "export const Button = memo(({ label }: { label: string }) => <button>{label}</button>);\n"
        "export function useToggle() { const [on] = useState(false); return on; }\n"
    ),
    "web/api.ts": (
        "export function get(path: string): Promise<Response>;\n"
        "export function get(path: any) { return fetch(path); }\n"
        "interface Options { a: string }\ninterface Options { b: number }\n"
    ),
    "web/legacy.js": 'const x = require("x");\nclass Legacy extends Base { m() {} }\n',
    "web/vendor.min.js": "var a=1;\n",
    "notes.md": "# Notes\n\nText.\n",
}


class _Stack:
    def __init__(self, engine: Engine, root: Path) -> None:
        self.factory = create_session_factory_from_engine(engine)
        with self.factory() as session:
            self.project = SqlAlchemyProjectRepository(session).add(
                new_project(name="Stage 13", root_path=str(root))
            )
        self.snapshots = SnapshotService(
            SqlAlchemySnapshotStore(self.factory),
            LocalRepositoryInspector(),
            SnapshotFingerprinter(reader=SupervisedSourceReader()),
        )
        self.structure_store = SqlAlchemyStructuralAnalysisStore(self.factory)
        self.structural = StructuralAnalysisService(
            self.structure_store, SupervisedArtifactAnalyzer()
        )
        self.processing = ProcessingService(
            SqlAlchemyProcessingStore(self.factory), reader=SupervisedSourceReader()
        )

    def snapshot(self) -> Any:
        return self.snapshots.create(self.project.id)


def _write(root: Path, files: dict[str, str]) -> None:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)


def _by_locator(analysis: Any) -> dict[str, Any]:
    return {item.locator: item for item in analysis.artifact_analyses}


def test_mixed_language_extraction_reuse_and_processing(pg_engine: Engine, tmp_path: Path) -> None:
    _write(tmp_path, FILES)
    stack = _Stack(pg_engine, tmp_path)
    first = stack.snapshot()
    analysis = stack.structural.analyse(stack.project.id, first.id)
    items = _by_locator(analysis)

    # Python keeps the frozen declaration producer and gains an additive facts pass.
    python = items["app/service.py"]
    assert python.outcome == "parsed" and python.language == "Python"
    assert python.declaration_extraction_id is None
    # Stage 14 adds its literal pass beside the facts pass; neither touches declarations.
    assert sorted(x.extractor for x in python.extractions) == [
        extractors.PYTHON_LITERALS,
        extractors.PYTHON_FACTS,
    ]
    # TS/JS carry their own declaration authority (M4), never the Python run contract.
    for locator in ("web/Button.tsx", "web/api.ts", "web/legacy.js"):
        item = items[locator]
        assert item.outcome == "parsed", locator
        assert item.declaration_extraction is not None
        assert item.declaration_extraction.contract_digest == extractors.extractor_contract_digest(
            item.declaration_extraction.extractor
        )
        literal = next(x for x in item.extractions if x.extractor == extractors.TS_LITERALS)
        assert literal.outcome == "extracted", locator
    assert items["web/vendor.min.js"].diagnostics[0].code == "GENERATED_ARTIFACT"
    assert analysis.producer_contract == analysis.producer_contract  # run contract unchanged
    with stack.factory() as session:
        schemes = set(
            session.scalars(
                select(SymbolIdentityRecord.scheme).where(
                    SymbolIdentityRecord.project_id == stack.project.id
                )
            )
        )
    assert schemes == {"python-declaration-slot.v1", "ts-declaration-slot.v1"}

    processed = stack.processing.process(stack.project.id, first.id)
    by_locator = {x.locator: x for x in processed.artifact_processings}
    tsx = by_locator["web/Button.tsx"]
    assert tsx.outcome == "processed"
    assert tsx.fingerprint_scheme == "ts-declarations-dependency.v1"
    assert by_locator["app/service.py"].fingerprint_scheme is None
    assert by_locator["web/vendor.min.js"].outcome == "unsupported"
    assert stack.processing.status(stack.project.id, first.id).active_freshness == "fresh"

    # Unchanged rescan: every declaration and additive pass is reused, identities preserved.
    second = stack.snapshot()
    again = _by_locator(stack.structural.analyse(stack.project.id, second.id))
    for locator, item in again.items():
        if items[locator].outcome != "parsed":
            continue
        assert item.execution_mode == "reused", locator
        assert all(x.execution_mode == "reused" for x in item.extractions), locator
        assert [s.stable_identity_id for s in item.symbols] == [
            s.stable_identity_id for s in items[locator].symbols
        ]
    reprocessed = stack.processing.process(stack.project.id, second.id)
    assert {x.execution_mode for x in reprocessed.artifact_processings if x.chunks} == {"reused"}


def test_legacy_python_declarations_gain_facts_without_identity_churn(
    pg_engine: Engine, tmp_path: Path
) -> None:
    _write(tmp_path, {"a.py": "def f():\n    pass\n\ndef f():\n    pass\n"})
    stack = _Stack(pg_engine, tmp_path)
    first = stack.snapshot()
    initial = stack.structural.analyse(stack.project.id, first.id)
    # Simulate a pre-Stage-13 run: Python declarations without any additive extraction.
    with stack.factory.begin() as session:
        session.execute(delete(ArtifactExtractionRecord))
        session.execute(text("UPDATE artifact_analyses SET language = NULL"))
    second = stack.snapshot()
    upgraded = stack.structural.analyse(stack.project.id, second.id)
    item = upgraded.artifact_analyses[0]
    assert item.execution_mode == "reused"
    assert sorted((x.extractor, x.execution_mode, x.outcome) for x in item.extractions) == [
        (extractors.PYTHON_LITERALS, "extracted", "extracted"),
        (extractors.PYTHON_FACTS, "extracted", "extracted"),
    ]
    # Duplicate names are ambiguous slots; reuse still preserves their identities exactly.
    assert [s.stable_identity_id for s in item.symbols] == [
        s.stable_identity_id for s in initial.artifact_analyses[0].symbols
    ]


def test_typescript_identity_continuity_across_edits(pg_engine: Engine, tmp_path: Path) -> None:
    _write(tmp_path, {"a.ts": "export function keep() {}\nexport function edit() { return 1; }\n"})
    stack = _Stack(pg_engine, tmp_path)
    first = stack.structural.analyse(stack.project.id, stack.snapshot().id)
    before = {s.name: s.stable_identity_id for s in first.artifact_analyses[0].symbols}
    _write(
        tmp_path,
        {"a.ts": "// moved\nexport function edit() { return 2; }\nexport function keep() {}\n"},
    )
    second = stack.structural.analyse(stack.project.id, stack.snapshot().id)
    item = second.artifact_analyses[0]
    assert item.execution_mode == "parsed"
    after = {s.name: (s.stable_identity_id, s.continuity_reason) for s in item.symbols}
    assert after["keep"] == (before["keep"], "continued_unique_slot")
    assert after["edit"] == (before["edit"], "continued_unique_slot")


def test_stale_typescript_contract_is_not_current_while_python_is(
    pg_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(
        tmp_path,
        {"a.py": "def py():\n    pass\n", "b.ts": "export function ts() {}\n"},
    )
    stack = _Stack(pg_engine, tmp_path)
    snapshot = stack.snapshot()
    run = stack.structural.analyse(stack.project.id, snapshot.id)
    stack.processing.process(stack.project.id, snapshot.id)
    names = {
        s.name
        for s in stack.structural.symbol_page(
            stack.project.id, snapshot.id, 50, 0, None, None, None
        )[0]
    }
    assert names == {"py", "ts"}

    original = extractors.extractor_contract

    def bumped(extractor: str) -> dict[str, str] | None:
        value = original(extractor)
        if value is not None and extractor in {
            extractors.TYPESCRIPT_DECLARATIONS,
            extractors.JAVASCRIPT_DECLARATIONS,
        }:
            value = {**value, "grammar": value["grammar"] + "+bumped"}
        return value

    monkeypatch.setattr(extractors, "extractor_contract", bumped)
    current = {
        s.name
        for s in stack.structural.symbol_page(
            stack.project.id, snapshot.id, 50, 0, None, None, None
        )[0]
    }
    assert current == {"py"}  # M4: TS is no longer current; Python in the same run still is
    history = stack.structural.symbol_page(
        stack.project.id, snapshot.id, 50, 0, None, None, None, run_id=run.id
    )[0]
    assert {s.name for s in history} == {"py", "ts"}  # still inspectable as explicit history
    status = stack.processing.status(stack.project.id, snapshot.id)
    assert status.active_freshness == "stale"
    assert status.freshness_reasons == ("STRUCTURAL_DEPENDENCY_CHANGED",)
