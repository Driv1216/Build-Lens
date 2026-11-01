"""Stage 15 Milestone A: the flow evidence run against real PostgreSQL and the real worker.

Covers INV-2 (rows bound to the structural run and fingerprints), INV-3 (no pre-existing row or
column changes; only the new pointer column), INV-4 (freshness, stale history, rebuild with
reuse) and the HTTP contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text

from alembic import command
from buildlens.api.main import create_app
from buildlens.application.architecture import ArchitectureService
from buildlens.application.flow_evidence import (
    FlowEvidenceService,
    FlowEvidenceStaleError,
)
from buildlens.core.config import Settings
from buildlens.infrastructure.persistence.architecture import SqlAlchemyArchitectureStore
from buildlens.infrastructure.persistence.flow_evidence import SqlAlchemyFlowEvidenceStore
from buildlens.infrastructure.persistence.models import (
    Base,
    FlowScopeRecord,
    SymbolRecord,
)
from buildlens.infrastructure.structural.supervisor import SupervisedSourceReader
from tests.semantic_support import CatalogStack, write

pytestmark = pytest.mark.integration

FILES = {
    "pyproject.toml": '[project]\nname = "flowapp"\ndependencies = ["fastapi>=0.115,<1"]\n',
    "uv.lock": 'version = 1\n\n[[package]]\nname = "fastapi"\nversion = "0.141.1"\n',
    "app/__init__.py": "",
    "app/api.py": (
        "from fastapi import APIRouter, Depends, HTTPException\n\n"
        "router = APIRouter()\n\n\n"
        "@router.post('/login')\n"
        "async def login(body, svc=Depends(get_auth)):\n"
        "    user = await svc.find(body.email)\n"
        "    if user is None:\n"
        "        raise HTTPException(status_code=401)\n"
        "    return svc.issue(user)\n"
    ),
    "app/util.py": "def helper(x):\n    return [f(i) for i in x]\n",
    "web/package.json": json.dumps({"name": "web", "dependencies": {"react": "^18.3.1"}}),
    "web/App.tsx": (
        "export function Save({ onSaved }: P) {\n"
        "  const save = async () => { const r = await create(); onSaved(r.id); };\n"
        "  return <button onClick={save}>Save</button>;\n"
        "}\n"
    ),
    "notes.md": "# Notes\n",
}

NEW_COLUMNS = {"project_versions": {"active_flow_evidence_run_id"}}


def _fingerprint(engine: Engine) -> dict[str, str]:
    """md5 of every pre-existing table over its pre-existing columns (INV-3)."""
    values: dict[str, str] = {}
    with engine.connect() as connection:
        for name, table in Base.metadata.tables.items():
            if name.startswith("flow_"):
                continue
            columns = [c.name for c in table.columns if c.name not in NEW_COLUMNS.get(name, set())]
            projection = ", ".join(f'"{c}"' for c in columns)
            values[name] = connection.execute(
                text(
                    f"SELECT md5(coalesce(string_agg(r::text, '|' ORDER BY r::text), '')) "
                    f'FROM (SELECT {projection} FROM "{name}") r'
                )
            ).scalar_one()
    return values


def _stack(
    engine: Engine, root: Path
) -> tuple[CatalogStack, Any, ArchitectureService, FlowEvidenceService]:
    write(root, FILES)
    stack = CatalogStack(engine, root, "Flow")
    snapshot = stack.prepared()
    stack.catalog.build(stack.project.id, snapshot.id)
    architecture = ArchitectureService(SqlAlchemyArchitectureStore(stack.factory), stack.catalog)
    architecture.build(stack.project.id, snapshot.id)
    flow = FlowEvidenceService(SqlAlchemyFlowEvidenceStore(stack.factory), SupervisedSourceReader)
    return stack, snapshot, architecture, flow


def test_flow_evidence_is_non_interfering_bound_and_fresh(
    pg_engine: Engine, tmp_path: Path
) -> None:
    stack, snapshot, architecture, flow = _stack(pg_engine, tmp_path)
    project_id, snapshot_id = stack.project.id, snapshot.id
    before = _fingerprint(pg_engine)

    run = flow.extract(project_id, snapshot_id)

    # INV-3: every pre-existing table is byte-identical over its pre-existing columns.
    assert _fingerprint(pg_engine) == before
    with pg_engine.connect() as connection:
        pointer = connection.execute(
            text("SELECT active_flow_evidence_run_id FROM project_versions WHERE id = :id"),
            {"id": snapshot_id},
        ).scalar_one()
    assert pointer == run.id
    # Nothing upstream became stale.
    assert stack.catalog.status(project_id, snapshot_id).freshness == "fresh"
    assert architecture.status(project_id, snapshot_id).freshness == "fresh"
    status = flow.status(project_id, snapshot_id)
    assert status.freshness == "fresh" and status.active is not None and status.active.id == run.id
    # An unchanged rerun short-circuits to the same run.
    assert flow.extract(project_id, snapshot_id).id == run.id

    _, _, rows, total = flow.coverage(project_id, snapshot_id, None, None, 100, 0)
    states = {row.locator: row.state for row in rows}
    assert states["app/api.py"] == "extracted" and states["web/App.tsx"] == "extracted"
    assert all(row.execution_mode in {"extracted", "none"} for row in rows)
    assert run.outcome == "complete" and run.evidence_digest is not None

    # INV-2: scopes are owned by symbols of the same structural run.
    api = next(row for row in rows if row.locator == "app/api.py")
    _, _, scopes, texts = flow.artifact(project_id, snapshot_id, None, api.artifact_analysis_id)
    login = next(s for s in scopes if s.name == "login")
    with stack.factory() as session:
        owner = session.get(SymbolRecord, login.owner_symbol_id)
        assert owner is not None and owner.name == "login"
        assert owner.structural_analysis_id == run.structural_analysis_id
    issue = next(c for c in login.callsites if c.callee.get("path") == ["issue"])
    assert issue.text_key is not None and any(t.key == issue.text_key for t in texts)
    tsx = next(row for row in rows if row.locator == "web/App.tsx")
    _, _, ts_scopes, _ = flow.artifact(project_id, snapshot_id, None, tsx.artifact_analysis_id)
    component = next(s for s in ts_scopes if s.name == "Save")
    assert component.owner_symbol_id is not None
    assert [(j.element, j.attribute, j.intrinsic) for j in component.jsx] == [
        ("button", "onClick", True)
    ]

    _, _, facts = flow.framework_facts(project_id, snapshot_id, None)
    found = {(f.locator, f.package, f.version, f.constraint) for f in facts}
    assert ("pyproject.toml", "fastapi", None, ">=0.115,<1") in found
    assert ("web/package.json", "react", None, "^18.3.1") in found
    assert ("uv.lock", "fastapi", "0.141.1", None) in found  # the exact pin (D17)
    files = {v["locator"]: v for v in run.coverage["version_files"]}
    assert files["pyproject.toml"]["state"] == "read" and files["uv.lock"]["state"] == "read"


def test_new_structural_run_stales_flow_and_rebuild_reuses_rebound_evidence(
    pg_engine: Engine, tmp_path: Path
) -> None:
    stack, snapshot, _, flow = _stack(pg_engine, tmp_path)
    project_id, snapshot_id = stack.project.id, snapshot.id
    first = flow.extract(project_id, snapshot_id)
    _, _, first_rows, _ = flow.coverage(project_id, snapshot_id, None, None, 100, 0)

    stack.structural.analyse(project_id, snapshot_id)  # a later structural run
    status = flow.status(project_id, snapshot_id)
    assert status.freshness == "stale" and status.freshness_reasons == (
        "STRUCTURAL_AUTHORITY_CHANGED",
    )
    with pytest.raises(FlowEvidenceStaleError):
        flow.select(project_id, snapshot_id, None)
    history, reasons = flow.select(project_id, snapshot_id, first.id)
    assert history.id == first.id and reasons == ("STRUCTURAL_AUTHORITY_CHANGED",)

    second = flow.extract(project_id, snapshot_id)
    assert second.id != first.id and second.revision == first.revision + 1
    assert second.structural_analysis_id != first.structural_analysis_id
    _, _, rows, _ = flow.coverage(project_id, snapshot_id, None, None, 100, 0)
    reused = {r.locator: r for r in rows if r.execution_mode == "reused"}
    assert {"app/api.py", "web/App.tsx", "app/util.py"} <= set(reused)
    assert all(r.reuse_source_run_id == first.id for r in reused.values())
    previous = {r.locator: r.evidence_digest for r in first_rows}
    assert all(previous[locator] == row.evidence_digest for locator, row in reused.items())
    assert second.evidence_digest == first.evidence_digest
    # Reused owners point at the new structural run's symbols.
    with stack.factory() as session:
        owners = (
            session.execute(
                select(SymbolRecord.structural_analysis_id)
                .join(FlowScopeRecord, FlowScopeRecord.owner_symbol_id == SymbolRecord.id)
                .where(FlowScopeRecord.flow_evidence_run_id == second.id)
            )
            .scalars()
            .all()
        )
    assert owners and set(owners) == {second.structural_analysis_id}


def test_source_changed_after_snapshot_is_not_current(pg_engine: Engine, tmp_path: Path) -> None:
    stack, snapshot, _, flow = _stack(pg_engine, tmp_path)
    (tmp_path / "app/util.py").write_text("def helper(x):\n    return x\n")
    run = flow.extract(stack.project.id, snapshot.id)
    _, _, rows, _ = flow.coverage(stack.project.id, snapshot.id, None, None, 100, 0)
    util = next(r for r in rows if r.locator == "app/util.py")
    assert util.state == "not_current"
    assert [d["code"] for d in util.diagnostics] == ["SOURCE_DIFFERS_FROM_SNAPSHOT"]
    assert run.outcome == "partial"


def test_flow_evidence_http_contract(pg_engine: Engine, tmp_path: Path) -> None:
    write(tmp_path, FILES)
    app = create_app(Settings(database_url=pg_engine.url.render_as_string(hide_password=False)))
    with TestClient(app) as client:
        project = client.post(
            "/api/v1/projects", json={"name": "Flow", "root_path": str(tmp_path)}
        ).json()["id"]
        base = f"/api/v1/projects/{project}/snapshots"
        scope = f"{base}/{client.post(base, json={}).json()['id']}"
        evidence = scope + "/flow/evidence"
        assert client.get(evidence).json()["freshness"] == "none"
        assert (
            client.get(evidence + "/coverage").json()["detail"]["code"] == "FLOW_EVIDENCE_REQUIRED"
        )
        assert client.post(evidence, json={}).json()["detail"]["code"] == "FLOW_STRUCTURE_REQUIRED"
        assert client.post(scope + "/structure", json={}).status_code == 200
        rejected = client.post(evidence, content="{}", headers={"Content-Type": "text/plain"})
        assert rejected.status_code == 415
        built = client.post(evidence, json={})
        assert built.status_code == 200, built.text
        body = built.json()
        assert body["freshness"] == "fresh" and body["active_run"]["outcome"] == "complete"
        run_id = body["active_run"]["run_id"]
        page = client.get(evidence + "/coverage?state=extracted").json()
        assert page["run_id"] == run_id and page["freshness_reasons"] == []
        api = next(i for i in page["items"] if i["locator"] == "app/api.py")
        detail = client.get(f"{evidence}/artifacts/{api['artifact_analysis_id']}").json()
        login = next(s for s in detail["scopes"] if s["name"] == "login")
        assert (
            login["callable_kind"] == "coroutine_function"
            and login["cfg"]["version"] == "flow-cfg.v1"
        )
        assert detail["display_texts"]
        facts = client.get(evidence + "/framework-facts").json()["items"]
        assert {"package": "fastapi", "locator": "pyproject.toml"}.items() <= next(
            f for f in facts if f["locator"] == "pyproject.toml"
        ).items()
        assert client.get(f"{evidence}/runs/{run_id}").json()["run_id"] == run_id
        assert client.get(evidence + "/runs").json()["total"] == 1
        # A later structural run: current reads fail closed, history stays readable.
        assert client.post(scope + "/structure", json={}).status_code == 200
        stale = client.get(evidence + "/coverage")
        assert stale.status_code == 409 and stale.json()["detail"]["code"] == "FLOW_EVIDENCE_STALE"
        labelled = client.get(f"{evidence}/coverage?run_id={run_id}").json()
        assert labelled["freshness_reasons"] == ["STRUCTURAL_AUTHORITY_CHANGED"]


def test_downgrade_refuses_while_flow_evidence_exists(pg_engine: Engine, tmp_path: Path) -> None:
    stack, snapshot, _, flow = _stack(pg_engine, tmp_path)
    flow.extract(stack.project.id, snapshot.id)
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parents[2] / "alembic"))
    with pytest.raises(Exception, match="Stage 15 flow (evidence|model) runs"):
        command.downgrade(config, "0016_architecture_rules")
    with pg_engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version")) == "0018_flow_model"
        )
