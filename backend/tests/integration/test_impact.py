from copy import deepcopy
from difflib import unified_diff
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine, delete, select, update

from buildlens.application.impact import (
    AnalysisRequest,
    Deadline,
    ImpactError,
    ImpactService,
    PrepareRequest,
    SnapshotSelection,
)
from buildlens.domain.architecture_rules import validate_rules
from buildlens.infrastructure.persistence.architecture_rules import SqlAlchemyArchitectureRuleStore
from buildlens.infrastructure.persistence.impact import SqlAlchemyImpactStore
from buildlens.infrastructure.persistence.models import (
    ArchitectureEdgeRecord,
    FlowEdgeRecord,
    ImpactReportRecord,
)
from buildlens.infrastructure.structural.impact import syntax_contract
from buildlens.infrastructure.structural.supervisor import SupervisedSourceReader
from tests.flow_support import FLOW_REPOSITORY, FlowStack

pytestmark = pytest.mark.integration


def test_sealed_slice_detects_changed_rows_modified_witnesses_and_missing_relationships(
    pg_engine: Engine, tmp_path: Path
) -> None:
    stack = FlowStack(pg_engine, tmp_path, FLOW_REPOSITORY)
    run = stack.build()
    service = ImpactService(
        SqlAlchemyImpactStore(pg_engine), SupervisedSourceReader, syntax_contract()
    )
    selection = SnapshotSelection(snapshot_id=stack.snapshot_id)
    prepared = service.prepare(
        stack.project_id, PrepareRequest(selection=selection, seal_flow=True)
    )
    assert prepared["seal"]
    candidates = [
        edge for edge in stack.edges(run) if edge.relation == "invokes" and edge.target_key
    ]
    edge = candidates[0]
    request = AnalysisRequest(mode="entity", before=selection, entity=edge.target_key, save=True)
    report = service.analyze(stack.project_id, request)
    assert report["findings"]
    assert (
        service.show(stack.project_id, report["result_digest"], verify=True)["status"] == "verified"
    )
    row = (FlowEdgeRecord.flow_run_id == run.id, FlowEdgeRecord.edge_key == edge.key)
    saved = (
        ImpactReportRecord.project_id == stack.project_id,
        ImpactReportRecord.result_digest == report["result_digest"],
    )
    with pg_engine.begin() as connection:
        payload = connection.execute(select(ImpactReportRecord.payload).where(*saved)).scalar_one()
        changed = deepcopy(payload)
        changed["findings"][0]["witnesses"][0]["steps"][0]["conditions"].append("tampered")
        connection.execute(update(ImpactReportRecord).where(*saved).values(payload=changed))
    with pytest.raises(ImpactError, match="IMPACT_REPORT_INTEGRITY_FAILED"):
        service.show(stack.project_id, report["result_digest"], verify=True)

    with pg_engine.begin() as connection:
        connection.execute(update(ImpactReportRecord).where(*saved).values(payload=payload))
        connection.execute(update(FlowEdgeRecord).where(*row).values(conditions=["tampered"]))
    with pytest.raises(ImpactError, match="IMPACT_WITNESS_INTEGRITY_FAILED"):
        service.show(stack.project_id, report["result_digest"], verify=True)

    with pg_engine.begin() as connection:
        connection.execute(delete(FlowEdgeRecord).where(*row))
    with pytest.raises(ImpactError, match="IMPACT_FLOW_SLICE_MEMBERSHIP_CHANGED"):
        service.analyze(stack.project_id, request)


def test_empty_impact_migration_round_trip(pg_engine: Engine) -> None:
    from alembic.config import Config
    from sqlalchemy import inspect

    from alembic import command

    root = Path(__file__).parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    expected = {name for name in inspect(pg_engine).get_table_names() if name.startswith("impact_")}
    assert len(expected) == 4
    command.downgrade(config, "0019_flow_explanations")
    assert expected.isdisjoint(inspect(pg_engine).get_table_names())
    command.upgrade(config, "head")
    assert expected <= set(inspect(pg_engine).get_table_names())


def test_cross_file_field_transfer_matches_callsite_and_forwarding(
    pg_engine: Engine, tmp_path: Path
) -> None:
    files = {
        "shop/__init__.py": "",
        "shop/provider.py": "def reserve(qty):\n    return {'accepted': True, 'quantity': qty}\n",
        "shop/forward.py": (
            "from shop.provider import reserve\n\ndef forwarded():\n    return reserve(7)\n"
        ),
        "shop/clients.py": (
            "from shop.provider import reserve\n"
            "from shop.forward import forwarded\n\n"
            "def consume():\n    result = reserve(7); reserve(8)\n"
            "    return result['accepted']\n\n"
            "def indirect():\n    result = forwarded()\n"
            "    return result['accepted']\n\n"
            "def discard():\n    reserve(7)\n\n"
            "def other_field():\n    result = reserve(7)\n"
            "    return result['quantity']\n\n"
            "def overwritten():\n    result = reserve(7)\n    result = {}\n"
            "    return result['accepted']\n"
        ),
    }
    stack = FlowStack(pg_engine, tmp_path, files)
    stack.build()
    service = ImpactService(
        SqlAlchemyImpactStore(pg_engine), SupervisedSourceReader, syntax_contract()
    )
    old = SnapshotSelection(snapshot_id=stack.snapshot_id)
    paths = sorted(path for path in files if not path.endswith("__init__.py"))
    service.prepare(stack.project_id, PrepareRequest(selection=old, locators=paths, seal_flow=True))

    provider_path = "shop/provider.py"
    candidate_source = files[provider_path].replace("True", "False")
    patch = "".join(
        unified_diff(
            files[provider_path].splitlines(keepends=True),
            candidate_source.splitlines(keepends=True),
            fromfile=f"a/{provider_path}",
            tofile=f"b/{provider_path}",
        )
    )
    candidate = service.analyze(
        stack.project_id, AnalysisRequest(mode="patch", before=old, patch=patch, save=True)
    )
    assert (tmp_path / provider_path).read_text() == files[provider_path]
    assert candidate["authority"]["new"] is None
    exposed = [
        row
        for row in candidate["findings"]
        if row["consequence"] == "baseline_relationship_exposure"
    ]
    assert any(
        any(step["relation"] == "invokes" for step in row["witnesses"][0]["steps"])
        for row in exposed
    )
    assert all(
        row["side"] == "old" and "CANDIDATE_RELATIONSHIP_NOT_RELINKED" in row["uncertainty"]
        for row in exposed
    )
    assert not any(row["mechanism"] == "data" for row in candidate["findings"])
    assert service.show(stack.project_id, candidate["result_digest"], verify=True)["reasons"] == [
        "OVERLAY_REPLAY_REQUIRES_ORIGINAL_BYTES",
    ]

    (tmp_path / provider_path).write_text(candidate_source)
    new_snapshot = stack.stack.prepared()
    stack.stack.catalog.build(stack.project_id, new_snapshot.id)
    stack.architecture.build(stack.project_id, new_snapshot.id)
    stack.evidence.extract(stack.project_id, new_snapshot.id)
    stack.flow.build(stack.project_id, new_snapshot.id)
    new = SnapshotSelection(snapshot_id=new_snapshot.id)
    service.prepare(stack.project_id, PrepareRequest(selection=new, locators=paths, seal_flow=True))

    request = AnalysisRequest(
        mode="snapshots",
        before=old,
        after=new,
        locators=[provider_path, "shop/clients.py"],
        save=True,
    )
    report = service.analyze(stack.project_id, request)
    replay = request.model_copy(
        update={"locators": list(reversed(request.locators)), "save": False}
    )
    repeated = service.analyze(stack.project_id, replay)
    assert repeated["request_digest"] == report["request_digest"]
    assert repeated["result_digest"] == report["result_digest"]
    assert repeated["attempt_id"] != report["attempt_id"]

    class FrontierOnly(Deadline):
        def near(self, reserve: float = 0.2) -> bool:
            return True

    partial = service.analyze(stack.project_id, replay, FrontierOnly(3))
    assert partial["status"] == "deadline"
    assert partial["request_digest"] == report["request_digest"]
    assert partial["preparations"] == [] and report["preparations"]
    assert partial["result_digest"] != report["result_digest"]
    consumers = [
        row
        for row in report["findings"]
        if row["consequence"] == "matched_cross_file_field_consumer"
    ]
    assert {(row["side"], row["entity"], row["distance"]) for row in consumers} == {
        (side, name, distance)
        for side in ("old", "new")
        for name, distance in (("consume", "direct"), ("indirect", "indirect"))
    }
    assert all(row["witnesses"][0]["local"]["field"] == "accepted" for row in consumers)
    assert (
        service.show(stack.project_id, report["result_digest"], verify=True)["status"] == "verified"
    )

    provider = tmp_path / "shop/provider.py"
    provider.write_text(
        files["shop/provider.py"]
        .replace("reserve(qty)", "reserve(qty, *, notify)")
        .replace("True", "False")
    )
    combined_snapshot = stack.stack.prepared()
    stack.stack.catalog.build(stack.project_id, combined_snapshot.id)
    stack.architecture.build(stack.project_id, combined_snapshot.id)
    stack.evidence.extract(stack.project_id, combined_snapshot.id)
    stack.flow.build(stack.project_id, combined_snapshot.id)
    combined = SnapshotSelection(snapshot_id=combined_snapshot.id)
    service.prepare(
        stack.project_id, PrepareRequest(selection=combined, locators=paths, seal_flow=True)
    )
    changed = service.analyze(
        stack.project_id,
        AnalysisRequest(
            mode="snapshots",
            before=old,
            after=combined,
            locators=["shop/provider.py"],
            save=True,
        ),
    )
    assert any(
        row["entity"] == "discard"
        and row["consequence"] == "missing_required_argument"
        and [proof["side"] for proof in row["witnesses"]] == ["old", "new"]
        for row in changed["findings"]
    )
    assert not any(
        row["side"] == "new" and row["consequence"] == "matched_cross_file_field_consumer"
        for row in changed["findings"]
    )
    assert (
        service.show(stack.project_id, changed["result_digest"], verify=True)["status"]
        == "verified"
    )


def test_route_shape_and_column_change_have_directional_exposure(
    pg_engine: Engine, tmp_path: Path
) -> None:
    api_path = "backend/app/api.py"
    model_path = "backend/app/models.py"
    consumer_path = "backend/app/persistence.py"
    fixture = dict(FLOW_REPOSITORY)
    fixture[api_path] = (
        ("from pydantic import BaseModel\n" + fixture[api_path])
        .replace("Depends, HTTPException", "Depends, HTTPException, Query")
        .replace(
            "@router.get('')\ndef list_items(svc: ItemService = Depends(get_service)):\n"
            "    return svc.listing()\n",
            "class Result(BaseModel):\n    count: int\n    ready: bool = True\n\n"
            "@router.get('', response_model=Result)\n"
            "def list_items(limit: int = Query(10)):\n    return {'count': limit, 'ready': True}\n",
        )
    )
    fixture[model_path] = fixture[model_path].replace(
        "name: Mapped[str]", "name: Mapped[str] = mapped_column(nullable=True)"
    )
    fixture[consumer_path] = (
        "from sqlalchemy.orm import Session\nfrom app.models import Item\n\n"
        "def change_name(session: Session):\n    session.add(Item(name='updated'))\n\n"
        "def select_name(session: Session):\n"
        "    return session.query(Item).with_entities(Item.name)\n\n"
        "def unrelated(session: Session):\n    return session.query(Item).with_entities(Item.id)\n"
    )
    stack = FlowStack(pg_engine, tmp_path, fixture)
    stack.build()
    rules = SqlAlchemyArchitectureRuleStore(stack.stack.factory).add(
        stack.project_id,
        validate_rules(
            [
                {
                    "name": "No API import of models",
                    "kind": "forbid",
                    "from": [f"module:{api_path}"],
                    "to": ["backend/app/model*.py"],
                }
            ]
        ),
    )
    service = ImpactService(
        SqlAlchemyImpactStore(pg_engine), SupervisedSourceReader, syntax_contract()
    )
    old = SnapshotSelection(snapshot_id=stack.snapshot_id)
    paths = [api_path, model_path, consumer_path]
    service.prepare(stack.project_id, PrepareRequest(selection=old, locators=paths, seal_flow=True))

    (tmp_path / api_path).write_text(
        fixture[api_path]
        .replace(
            "from app.db import get_db", "from app.db import get_db\nfrom app.models import Item"
        )
        .replace("@router.post('',", "@router.post('/new',")
        .replace("Query(10)", "Query(...)")
        .replace("    count: int\n", "")
    )
    (tmp_path / model_path).write_text(
        fixture[model_path].replace("nullable=True", "nullable=False")
    )
    new_snapshot = stack.stack.prepared()
    stack.stack.catalog.build(stack.project_id, new_snapshot.id)
    stack.architecture.build(stack.project_id, new_snapshot.id)
    stack.evidence.extract(stack.project_id, new_snapshot.id)
    stack.flow.build(stack.project_id, new_snapshot.id)
    new = SnapshotSelection(snapshot_id=new_snapshot.id)
    service.prepare(stack.project_id, PrepareRequest(selection=new, locators=paths, seal_flow=True))

    report = service.analyze(
        stack.project_id,
        AnalysisRequest(
            mode="snapshots",
            before=old,
            after=new,
            locators=paths,
            architecture_rule_version=rules.version,
            save=True,
        ),
    )
    http = [
        row
        for row in report["findings"]
        if row["consequence"] == "old_consumer_exposed_to_route_shape_change"
    ]
    assert http and all(
        row["side"] == "old" and row["relationship_tier"] == "inferred" for row in http
    )
    assert all(
        "HTTP_LINK_INDEPENDENT_EVALUATION_PENDING" in row["witnesses"][0]["conditions"]
        for row in http
    )
    assert all(row["witnesses"][0]["new_route_shape"] == ["POST", "/api/items/new"] for row in http)
    field_findings = {
        row["consequence"]: row
        for row in report["findings"]
        if row["consequence"]
        in {
            "old_request_contract_allows_omitting_new_required_query",
            "new_response_declaration_omits_old_field",
        }
    }
    assert set(field_findings) == {
        "old_request_contract_allows_omitting_new_required_query",
        "new_response_declaration_omits_old_field",
    }
    coverage = service.coverage(stack.project_id, old, api_path, new)
    assert coverage["property_comparison"]["api_query_contract"] == "partially_comparable"
    assert coverage["property_comparison"]["api_response_contract"] == "partially_comparable"
    assert coverage["property_comparison"]["database_schema"] == "comparison_unavailable"
    for row in field_findings.values():
        old_proof, new_proof = row["witnesses"]
        assert [old_proof["side"], new_proof["side"]] == ["old", "new"]
        assert old_proof["old_request_shape"] == ["GET", "/api/items"]
        assert [step["relation"] for step in old_proof["steps"]] == ["handled_by", "calls_endpoint"]
        assert new_proof["steps"][0]["relation"] == "handled_by"
        assert "HTTP_LINK_INDEPENDENT_EVALUATION_PENDING" in old_proof["conditions"]
        assert "RUNTIME_SCHEMA_AND_CLIENT_FIELD_BEHAVIOR_UNKNOWN" in old_proof["conditions"]
    table = [row for row in report["findings"] if row["property"] == "table_exposure"]
    assert table and {row["side"] for row in table} == {"old", "new"}
    assert all(row["mechanism"] == "shared_state" for row in table)
    assert "COLUMN_CONSUMER_LINEAGE_PARTIAL" in report["gaps"]
    columns = [
        row
        for row in report["findings"]
        if row["consequence"] in {"matched_column_read", "matched_column_write_input"}
    ]
    assert {(row["side"], row["entity"], row["consequence"]) for row in columns} == {
        (side, entity, consequence)
        for side in ("old", "new")
        for entity, consequence in [
            ("change_name", "matched_column_write_input"),
            ("select_name", "matched_column_read"),
        ]
    }
    assert all(
        [step["relation"] for step in row["witnesses"][0]["steps"]]
        == [
            "on_table",
            "performs",
            "contains_callsite",
        ]
        for row in columns
    )
    assert report["architecture_rules"]["digest"] == rules.digest
    explicit = [row for row in report["findings"] if row["mechanism"] == "architecture"]
    assert [(row["side"], row["entity"]) for row in explicit] == [("new", f"module:{model_path}")]
    assert explicit[0]["witnesses"][0]["namespace"] == "architecture"
    assert "ARCHITECTURE_SCOPED_MEMBERSHIP_UNVERIFIED" in report["gaps"]
    assert (
        service.show(stack.project_id, report["result_digest"], verify=True)["status"] == "verified"
    )
    proof = explicit[0]["witnesses"][0]["steps"][0]
    with pg_engine.begin() as connection:
        connection.execute(
            update(ArchitectureEdgeRecord)
            .where(
                ArchitectureEdgeRecord.architecture_run_id
                == UUID(report["authority"]["new"]["architecture_id"]),
                ArchitectureEdgeRecord.edge_key == proof["key"],
            )
            .values(conditions=["modified"])
        )
    with pytest.raises(ImpactError, match="IMPACT_ARCHITECTURE_WITNESS_INTEGRITY_FAILED"):
        service.show(stack.project_id, report["result_digest"], verify=True)
