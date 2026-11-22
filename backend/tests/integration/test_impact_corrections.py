"""Disposable-database review corrections: actual provenance and history paths."""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine, insert, select, update

from buildlens.application.impact import (
    AnalysisRequest,
    Deadline,
    ImpactError,
    ImpactService,
    PrepareRequest,
    SnapshotSelection,
)
from buildlens.domain.source import digest
from buildlens.infrastructure.persistence.impact import SqlAlchemyImpactStore
from buildlens.infrastructure.persistence.models import (
    ImpactPreparationRecord,
    ImpactReportRecord,
    ProjectVersionRecord,
    StructuralAnalysisRecord,
    SymbolRecord,
)
from buildlens.infrastructure.structural.impact import syntax_contract
from buildlens.infrastructure.structural.supervisor import SupervisedSourceReader
from tests.semantic_support import CatalogStack

pytestmark = pytest.mark.integration


def service_for(engine: Engine) -> ImpactService:
    return ImpactService(SqlAlchemyImpactStore(engine), SupervisedSourceReader, syntax_contract())


def test_sealed_reuse_legacy_repreparation_and_source_tampering(
    pg_engine: Engine, tmp_path: Path
) -> None:
    path = tmp_path / "case.py"
    path.write_text("def p(a=0):\n    return a\n")
    stack = CatalogStack(pg_engine, tmp_path)
    before = stack.prepared()
    service = service_for(pg_engine)
    selection = SnapshotSelection(snapshot_id=before.id)
    request = PrepareRequest(selection=selection, locators=["case.py"])
    first = service.prepare(stack.project.id, request)
    identifier = UUID(first["completed"][0]["id"])
    newer = stack.prepared()
    reused = service.prepare(
        stack.project.id,
        PrepareRequest(selection=SnapshotSelection(snapshot_id=newer.id), locators=["case.py"]),
    )
    assert reused["completed"] == [{"locator": "case.py", "id": str(identifier), "reused": True}]
    report = service.analyze(
        stack.project.id,
        AnalysisRequest(
            mode="hypothesis",
            before=selection,
            locators=["case.py"],
            confirm=True,
            operations=[{"kind": "require_parameter", "target": "p", "member": "a"}],
            save=True,
        ),
    )
    proof = service.show(stack.project.id, report["result_digest"], verify=True)
    assert proof["status"] == "verified" and proof["evidence_verification"] == "complete"
    with pg_engine.begin() as connection:
        original = connection.execute(
            select(ImpactPreparationRecord.fingerprint).where(
                ImpactPreparationRecord.id == identifier
            )
        ).scalar_one()
        connection.execute(
            update(ImpactPreparationRecord)
            .where(ImpactPreparationRecord.id == identifier)
            .values(fingerprint="c" * 64)
        )
    with pytest.raises(ImpactError, match="IMPACT_PREPARATION_IDENTITY_CHANGED"):
        service.show(stack.project.id, report["result_digest"], verify=True)
    with pg_engine.begin() as connection:
        connection.execute(
            update(ImpactPreparationRecord)
            .where(ImpactPreparationRecord.id == identifier)
            .values(fingerprint=original, input_seal_digest=None, source_identity=None)
        )
        payload = connection.execute(
            select(ImpactReportRecord.payload).where(
                ImpactReportRecord.result_digest == report["result_digest"]
            )
        ).scalar_one()
        legacy = deepcopy(payload)
        legacy["contract"] = "impact.v4"
        legacy.pop("syntax_contract")
        legacy.pop("comparisons")
        for ref in legacy["preparations"]:
            ref.pop("input_seal")
        legacy.pop("result_digest")
        legacy["result_digest"] = digest(legacy)
        connection.execute(
            insert(ImpactReportRecord).values(
                project_id=stack.project.id,
                result_digest=legacy["result_digest"],
                request_digest=legacy["request_digest"],
                payload=legacy,
            )
        )
    qualified = service.show(stack.project.id, legacy["result_digest"], verify=True)
    assert qualified["status"] == "qualified"
    assert qualified["integrity"] == "unverified_legacy"
    assert qualified["compatibility"] == "changed"
    assert qualified["evidence_verification"] == "partial"
    assert service.show(stack.project.id, legacy["result_digest"])["contract"] == "impact.v4"
    fresh = service.prepare(stack.project.id, request)
    assert fresh["completed"][0]["id"] != str(identifier)
    assert fresh["completed"][0]["reused"] is False
    with pg_engine.connect() as connection:
        assert (
            connection.execute(
                select(ImpactPreparationRecord.input_seal_digest).where(
                    ImpactPreparationRecord.id == identifier
                )
            ).scalar_one()
            is None
        )


def test_declaration_only_report_checks_both_sides_and_freshness(
    pg_engine: Engine, tmp_path: Path
) -> None:
    path = tmp_path / "case.py"
    path.write_text("def p(a=0):\n    return a\n")
    stack = CatalogStack(pg_engine, tmp_path)
    old = stack.prepared()
    path.write_text("def p(a):\n    return a\n")
    new = stack.prepared()
    service = service_for(pg_engine)
    report = service.analyze(
        stack.project.id,
        AnalysisRequest(
            mode="snapshots",
            before=SnapshotSelection(snapshot_id=old.id),
            after=SnapshotSelection(snapshot_id=new.id),
            locators=["case.py"],
            save=True,
        ),
    )
    changes = [
        row
        for row in report["changes"]
        if row.get("authority") == "retained_structural_observation"
    ]
    assert changes and not report["findings"]
    assert (
        service.show(stack.project.id, report["result_digest"], verify=True)["status"] == "verified"
    )
    for side in ("old", "new"):
        identity = UUID(changes[0][f"{side}_evidence"])
        with pg_engine.begin() as connection:
            original = connection.execute(
                select(SymbolRecord.state_fingerprint).where(SymbolRecord.id == identity)
            ).scalar_one()
            connection.execute(
                update(SymbolRecord)
                .where(SymbolRecord.id == identity)
                .values(state_fingerprint="f" * 64)
            )
        with pytest.raises(ImpactError, match="IMPACT_DECLARATION_EVIDENCE_CHANGED"):
            service.show(stack.project.id, report["result_digest"], verify=True)
        with pg_engine.begin() as connection:
            connection.execute(
                update(SymbolRecord)
                .where(SymbolRecord.id == identity)
                .values(state_fingerprint=original)
            )
    structure = UUID(report["authority"]["old"]["structural_id"])
    with pg_engine.begin() as connection:
        original = connection.execute(
            select(StructuralAnalysisRecord.producer_contract).where(
                StructuralAnalysisRecord.id == structure
            )
        ).scalar_one()
        connection.execute(
            update(StructuralAnalysisRecord)
            .where(StructuralAnalysisRecord.id == structure)
            .values(producer_contract={"changed": True})
        )
    with pytest.raises(ImpactError, match="IMPACT_DECLARATION_EVIDENCE_CHANGED"):
        service.show(stack.project.id, report["result_digest"], verify=True)
    with pg_engine.begin() as connection:
        connection.execute(
            update(StructuralAnalysisRecord)
            .where(StructuralAnalysisRecord.id == structure)
            .values(producer_contract=original)
        )
        connection.execute(
            update(ProjectVersionRecord)
            .where(ProjectVersionRecord.id == old.id)
            .values(active_analysis_id=None)
        )
    qualified = service.show(stack.project.id, report["result_digest"], verify=True)
    assert qualified["integrity"] == "verified" and qualified["freshness"] == "stale"
    assert qualified["status"] == "qualified" and qualified["evidence_verification"] == "complete"


def test_inventory_additions_removals_and_unsearched_parents(
    pg_engine: Engine, tmp_path: Path
) -> None:
    (tmp_path / ".gitignore").write_text("ignored/\n")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored/hidden.py").write_text("private = True\n")
    (tmp_path / "old.py").write_text("def p():\n    return 1\n")
    stack = CatalogStack(pg_engine, tmp_path)
    old = stack.prepared()
    (tmp_path / "old.py").unlink()
    (tmp_path / "new.py").write_text("def q():\n    return 2\n")
    new = stack.prepared()
    service = service_for(pg_engine)
    report = service.analyze(
        stack.project.id,
        AnalysisRequest(
            mode="snapshots",
            before=SnapshotSelection(snapshot_id=old.id),
            after=SnapshotSelection(snapshot_id=new.id),
            save=True,
        ),
    )
    assert {(row["locator"], row["comparison"]) for row in report["changes"]} == {
        ("old.py", "removed"),
        ("new.py", "introduced"),
    }
    assert report["status"] == "evidence_incomplete"
    assert (
        service.show(stack.project.id, report["result_digest"], verify=True)["integrity"]
        == "verified"
    )
    with SqlAlchemyImpactStore(pg_engine).open(stack.project.id, Deadline(3)) as db:
        authority = db.authority(SnapshotSelection(snapshot_id=old.id))
        assert db.absent(authority, "new.py")
        assert not db.absent(replace(authority, inventory_complete=False), "new.py")
        assert not db.absent(authority, "old.py")
        assert not db.absent(authority, "ignored/hidden.py")
    with pg_engine.begin() as connection:
        connection.execute(
            update(ProjectVersionRecord)
            .where(ProjectVersionRecord.id == old.id)
            .values(scan_status="partial")
        )
    unknown = service.analyze(
        stack.project.id,
        AnalysisRequest(
            mode="snapshots",
            before=SnapshotSelection(snapshot_id=old.id),
            after=SnapshotSelection(snapshot_id=new.id),
            locators=["new.py"],
        ),
    )
    assert unknown["changes"][0]["comparison"] == "unavailable"


def test_corrected_api_cli_contract_and_bounded_request_smoke(
    pg_engine: Engine, tmp_path: Path, record_property: Callable[[str, object], None]
) -> None:
    import json
    import time

    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    from buildlens.api.main import create_app
    from buildlens.cli.main import app as cli
    from buildlens.core.config import Settings

    (tmp_path / "case.py").write_text(
        "def p():\n    return {'a': True}\ndef c():\n    return p()['a']\n"
    )
    stack = CatalogStack(pg_engine, tmp_path)
    old = stack.prepared()
    selection = SnapshotSelection(snapshot_id=old.id)
    service = service_for(pg_engine)
    service.prepare(stack.project.id, PrepareRequest(selection=selection, locators=["case.py"]))
    base = f"/api/v1/projects/{stack.project.id}/impact/analyses"
    payload = {
        "mode": "hypothesis",
        "before": {"snapshot_id": str(old.id)},
        "locators": ["case.py"],
        "operations": [
            {"kind": "set_return_boolean", "target": "p", "member": "a", "value": False}
        ],
        "confirm": True,
        "save": True,
    }
    settings = Settings(
        _env_file=None, database_url=pg_engine.url.render_as_string(hide_password=False)
    )
    with TestClient(create_app(settings)) as client:
        start = time.monotonic()
        response = client.post(base, json=payload)
        wall = time.monotonic() - start
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["retained"] and any(row["entity"] == "c" for row in report["findings"])
        assert wall < 3.0
        record_property("hypothesis_http_wall_seconds", wall)
        result_id = report["result_digest"]
        proof = client.get(f"{base}/{result_id}?verify=true")
        assert proof.status_code == 200 and proof.json()["status"] == "verified"
        record_property("result_digest", result_id)
    observed = CliRunner().invoke(cli, ["impact", "verify", str(stack.project.id), result_id])
    assert observed.exit_code == 0, observed.output
    assert json.loads(observed.stdout)["evidence_verification"] == "complete"

    # Removing a local proof reference must qualify even a hash-valid retained payload.
    with pg_engine.begin() as connection:
        payload = connection.execute(
            select(ImpactReportRecord.payload).where(ImpactReportRecord.result_digest == result_id)
        ).scalar_one()
        altered = deepcopy(payload)
        for finding in altered["findings"]:
            finding.pop("comparison_id", None)
        altered.pop("result_digest")
        altered["result_digest"] = digest(altered)
        connection.execute(
            insert(ImpactReportRecord).values(
                project_id=stack.project.id,
                result_digest=altered["result_digest"],
                request_digest=altered["request_digest"],
                payload=altered,
            )
        )
    missing = service.show(stack.project.id, altered["result_digest"], verify=True)
    assert missing["status"] == "qualified" and missing["evidence_verification"] == "partial"
