"""Snapshot-qualified Stage 16 PostgreSQL adapter with selective graph reads."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.orm import Session, aliased, sessionmaker

from buildlens.application.flow_inputs import scope_key
from buildlens.application.impact import (
    Artifact,
    Authority,
    CallObservation,
    Deadline,
    EndpointObservation,
    ImpactError,
    Preparation,
    SnapshotSelection,
)
from buildlens.application.impact_properties import compare_syntax
from buildlens.domain.architecture import MODULE_EDGE_KINDS
from buildlens.domain.architecture import edge_digest as architecture_edge_digest
from buildlens.domain.architecture import node_digest as architecture_node_digest
from buildlens.domain.architecture_rules import RULES_VERSION, RuleSet, rules_digest
from buildlens.domain.flow import FLOW_MODEL_VERSION, FlowDiagnostic
from buildlens.domain.impact import CONTRACT, MAX_VERIFIED_ROWS, CellKey, Frontier, Transfer
from buildlens.domain.source import digest
from buildlens.infrastructure.persistence.architecture import SqlAlchemyArchitectureStore
from buildlens.infrastructure.persistence.architecture import _run as architecture_run
from buildlens.infrastructure.persistence.architecture_rules import _rule_set
from buildlens.infrastructure.persistence.flow import SqlAlchemyFlowStore, _edge, _node, _run
from buildlens.infrastructure.persistence.models import (
    ArchitectureEdgeRecord,
    ArchitectureRuleSetRecord,
    ArchitectureRunRecord,
    ArtifactAnalysisRecord,
    ArtifactExtractionRecord,
    ArtifactRecord,
    FlowArtifactCoverageRecord,
    FlowCallsiteRecord,
    FlowDiagnosticRecord,
    FlowEdgeRecord,
    FlowEvidenceItemRecord,
    FlowNodeRecord,
    FlowRunRecord,
    FlowScopeRecord,
    ImpactPreparationRecord,
    ImpactReportRecord,
    ImpactSealRecord,
    ImpactSealShardRecord,
    ProjectVersionRecord,
    SemanticCatalogRunRecord,
    SemanticEntityRecord,
    SemanticFactRecord,
    SemanticRelationRecord,
    StructuralAnalysisRecord,
    SymbolRecord,
)
from buildlens.infrastructure.persistence.semantic import SqlAlchemySemanticCatalogStore, _fact

_PAGE = 96
_SEAL = "impact-flow-slice.v1"
_SHARDS = 256
_MAX_SEAL_ELEMENTS = 1_200_000
_INBOUND = frozenset(
    {
        "contains_callsite",
        "invokes",
        "awaits",
        "handled_by",
        "calls_endpoint",
        "issues_request",
        "injects",
        "on_table",
        "performs",
        "enters",
        "invokes_callback",
        "dispatch_candidate",
    }
)


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _members(rows: list[FlowEdgeRecord]) -> list[Any]:
    sha = hashlib.sha256()
    for row in rows:
        sha.update(_json([row.edge_key, row.edge_digest]) + b"\n")
    return [len(rows), sha.hexdigest()]


class SqlAlchemyImpactStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @contextmanager
    def open(
        self, project: UUID, deadline: Deadline, preparation: bool = False
    ) -> Iterator[PostgresImpactView]:
        deadline.remaining()
        # Session-level locks survive commits of completed preparation units.
        slot = 0x4275696C644D0000 + int(preparation)
        try:
            with self.engine.connect().execution_options(
                isolation_level="REPEATABLE READ"
            ) as connection:
                with Session(bind=connection, expire_on_commit=False) as session:
                    view = PostgresImpactView(session, project, deadline)
                    view.tick()
                    acquired = bool(
                        session.scalar(text("SELECT pg_try_advisory_lock(:slot)"), {"slot": slot})
                    )
                    if not acquired:
                        raise ImpactError("IMPACT_BUSY", 409)
                    try:
                        yield view
                    finally:
                        try:
                            session.rollback()
                            released = session.scalar(
                                text("SELECT pg_advisory_unlock(:slot)"), {"slot": slot}
                            )
                            session.rollback()
                            if not released:
                                connection.invalidate()
                        except DBAPIError:
                            connection.invalidate()
        except PoolTimeout:
            raise ImpactError("IMPACT_POOL_TIMEOUT", 503) from None
        except DBAPIError as error:
            code = getattr(error.orig, "sqlstate", None)
            if code in {"57014", "55P03"}:
                raise ImpactError("IMPACT_DATABASE_TIMEOUT", 408) from None
            if code == "40001":
                raise ImpactError("IMPACT_AUTHORITY_CHANGED", 409) from None
            if code == "42P01":
                raise ImpactError("IMPACT_MIGRATION_REQUIRED", 503) from None
            raise ImpactError("IMPACT_DATABASE_UNAVAILABLE", 503) from None


class PostgresImpactView:
    def __init__(self, session: Session, project: UUID, deadline: Deadline) -> None:
        self.session, self.project, self.deadline = session, project, deadline
        self._roots: dict[str, ProjectVersionRecord] = {}
        self._flow: dict[str, FlowRunRecord] = {}
        self._nodes: dict[tuple[str, str], FlowNodeRecord] = {}
        self._edges: dict[tuple[str, str], FlowEdgeRecord] = {}
        self._adjacency: dict[tuple[str, str, str], Frontier] = {}
        self._verified: set[tuple[str, str]] = set()
        self._catalog_verified: dict[tuple[str, str], dict[str, Any]] = {}
        self._shards: dict[tuple[str, int], dict[str, object]] = {}
        self._seal_records: dict[str, ImpactSealRecord | None] = {}

    def tick(self) -> None:
        remaining = self.deadline.remaining()
        self.session.execute(
            text("SELECT set_config('statement_timeout', :timeout, true)"),
            {"timeout": f"{max(1, int(remaining * 1000))}ms"},
        )

    def authority(self, selection: SnapshotSelection) -> Authority:
        self.tick()
        version = self.session.get(ProjectVersionRecord, selection.snapshot_id)
        if version is None or version.project_id != self.project:
            raise ImpactError("IMPACT_SNAPSHOT_NOT_FOUND", 404)
        self._roots[str(version.id)] = version
        active = version.active_flow_run_id
        requested = selection.flow_run_id or active
        historical = selection.flow_run_id is not None and requested != active
        row = self.session.get(FlowRunRecord, requested) if requested else None
        if row and (row.project_id != self.project or row.project_version_id != version.id):
            row = None
        if row and (
            row.outcome == "failed"
            or not row.graph_digest
            or digest(row.contract) != row.contract_digest
        ):
            row = None
        if selection.flow_run_id and row is None:
            raise ImpactError("IMPACT_FLOW_AUTHORITY_NOT_FOUND", 404)
        structure_id = (
            row.structural_analysis_id if historical and row else version.active_analysis_id
        )
        catalog_id = (
            row.catalog_run_id if historical and row else version.active_semantic_catalog_run_id
        )
        architecture_id = (
            row.architecture_run_id if historical and row else version.active_architecture_run_id
        )
        structure = (
            self.session.get(StructuralAnalysisRecord, structure_id) if structure_id else None
        )
        if structure and (
            structure.project_version_id != version.id
            or structure.quarantined
            or structure.outcome == "failed"
        ):
            structure = None
        catalog = self.session.get(SemanticCatalogRunRecord, catalog_id) if catalog_id else None
        if catalog and (
            catalog.project_version_id != version.id
            or catalog.quarantined
            or catalog.outcome == "failed"
            or not structure
            or catalog.structural_analysis_id != structure.id
            or (not historical and catalog.processing_run_id != version.active_processing_run_id)
        ):
            catalog = None
        architecture = (
            self.session.get(ArchitectureRunRecord, architecture_id) if architecture_id else None
        )
        if architecture and (
            architecture.project_version_id != version.id
            or architecture.quarantined
            or architecture.outcome == "failed"
            or not catalog
            or architecture.catalog_run_id != catalog.id
            or architecture.catalog_digest != catalog.catalog_digest
        ):
            architecture = None
        if row and (
            not structure
            or not catalog
            or not architecture
            or row.structural_analysis_id != structure.id
            or row.catalog_digest != catalog.catalog_digest
            or row.architecture_graph_digest != architecture.graph_digest
            or (not historical and row.flow_evidence_run_id != version.active_flow_evidence_run_id)
        ):
            row = None
        if selection.flow_run_id and row is None:
            raise ImpactError("IMPACT_FLOW_AUTHORITY_INCOHERENT")
        if row:
            self._flow[str(row.id)] = row
        return Authority(
            str(version.id),
            str(version.content_manifest["digest"]) if version.content_manifest else None,
            version.scan_status == "complete" and not version.legacy_unverified,
            str(structure.id) if structure else None,
            str(catalog.id) if catalog else None,
            str(architecture.id) if architecture else None,
            str(row.id) if row else None,
            row.graph_digest if row else None,
            row.contract_digest if row else None,
            historical,
        )

    def artifact(self, authority: Authority, locator: str) -> Artifact | None:
        self.tick()
        query = (
            select(ArtifactRecord)
            .where(
                ArtifactRecord.project_version_id == UUID(authority.snapshot_id),
                ArtifactRecord.locator == locator,
                ArtifactRecord.kind == "file",
            )
            .limit(2)
        )
        rows = self.session.scalars(query).all()
        if len(rows) != 1:
            return None
        row = rows[0]
        comparable = (
            row.fingerprint_status == "fingerprinted" and row.fingerprint_algorithm == "sha256"
        )
        return Artifact(
            row.locator,
            row.content_digest if comparable else None,
            row.language,
            row.inventory_status == "included",
            row.fingerprint_schema_version if comparable else None,
        )

    def changed(self, before: Authority, after: Authority, limit: int) -> list[str]:
        self.tick()
        left, right = aliased(ArtifactRecord), aliased(ArtifactRecord)
        left_id, right_id = UUID(before.snapshot_id), UUID(after.snapshot_id)
        before_rows = (
            select(left).where(left.project_version_id == left_id, left.kind == "file").subquery()
        )
        after_rows = (
            select(right)
            .where(right.project_version_id == right_id, right.kind == "file")
            .subquery()
        )
        a, b = before_rows.c, after_rows.c
        locator = func.coalesce(a.locator, b.locator)
        query = (
            select(locator)
            .select_from(before_rows.join(after_rows, a.locator == b.locator, full=True))
            .where(
                or_(a.inventory_status == "included", b.inventory_status == "included"),
                or_(
                    a.content_digest.is_distinct_from(b.content_digest),
                    a.inventory_status.is_distinct_from(b.inventory_status),
                    a.fingerprint_schema_version.is_distinct_from(b.fingerprint_schema_version),
                    and_(a.content_digest.is_(None), b.content_digest.is_(None)),
                ),
            )
            .distinct()
            .order_by(locator)
            .limit(limit)
        )
        return list(self.session.scalars(query))

    def root(self, authority: Authority) -> tuple[str, tuple[int, int]] | None:
        version = self._roots[authority.snapshot_id]
        value = version.root_observation or {}
        if (
            version.canonical_root
            and {"device", "inode"} <= value.keys()
            and not version.legacy_unverified
        ):
            return version.canonical_root, (int(str(value["device"])), int(str(value["inode"])))
        return None

    def absent(self, authority: Authority, locator: str) -> bool:
        if not authority.inventory_complete:
            return False
        self.tick()
        # An ignored/pruned parent is not a searched namespace, even after a
        # successful overall scan. Include all kinds at the exact path as well.
        components = locator.split("/")
        prefixes = ["/".join(components[:index]) for index in range(1, len(components) + 1)]
        rows = self.session.scalars(
            select(ArtifactRecord).where(
                ArtifactRecord.project_version_id == UUID(authority.snapshot_id),
                ArtifactRecord.locator.in_(prefixes),
            )
        ).all()
        return not any(
            row.locator == locator
            or row.inventory_status != "included"
            or row.pruned
            or row.kind != "directory"
            for row in rows
        )

    def preparation(self, fingerprint_: str, contract: str, locator: str) -> Preparation | None:
        self.tick()
        row = self.session.scalar(
            select(ImpactPreparationRecord).where(
                ImpactPreparationRecord.project_id == self.project,
                ImpactPreparationRecord.fingerprint == fingerprint_,
                ImpactPreparationRecord.locator == locator,
                ImpactPreparationRecord.contract == contract,
                ImpactPreparationRecord.input_seal_digest.is_not(None),
            )
        )
        return self._checked_preparation(row) if row else None

    def architecture_rule_set(self, version: int) -> RuleSet | None:
        self.tick()
        row = self.session.scalar(
            select(ArchitectureRuleSetRecord).where(
                ArchitectureRuleSetRecord.project_id == self.project,
                ArchitectureRuleSetRecord.version == version,
            )
        )
        if row is None:
            return None
        try:
            rules = _rule_set(row)
        except ValueError:
            raise ImpactError("IMPACT_ARCHITECTURE_RULE_INTEGRITY_FAILED") from None
        if rules.contract != RULES_VERSION or rules_digest(rules.rules) != rules.digest:
            raise ImpactError("IMPACT_ARCHITECTURE_RULE_INTEGRITY_FAILED")
        return rules

    def _architecture_run(self, authority: Authority) -> ArchitectureRunRecord | None:
        self.tick()
        run: ArchitectureRunRecord | None = (
            self.session.get(ArchitectureRunRecord, UUID(authority.architecture_id))
            if authority.architecture_id
            else None
        )
        if (
            run is None
            or run.project_id != self.project
            or str(run.project_version_id) != authority.snapshot_id
            or run.quarantined
            or run.outcome == "failed"
            or not run.graph_digest
        ):
            return None
        return run

    def architecture_dependencies(
        self,
        authority: Authority,
        locators: list[str],
    ) -> dict[str, Any] | None:
        run = self._architecture_run(authority)
        if run is None:
            return None
        self.tick()
        sources = [f"module:{path}" for path in locators]
        rows = list(
            self.session.scalars(
                select(ArchitectureEdgeRecord)
                .where(
                    ArchitectureEdgeRecord.architecture_run_id == run.id,
                    ArchitectureEdgeRecord.source_key.in_(sources),
                    ArchitectureEdgeRecord.kind.in_(MODULE_EDGE_KINDS),
                )
                .order_by(ArchitectureEdgeRecord.edge_key.collate("C"))
                .limit(_PAGE + 1)
            )
        )
        gaps = {"ARCHITECTURE_EDGE_LIMIT"} if len(rows) > _PAGE else set()
        if run.outcome == "partial":
            gaps.add("ARCHITECTURE_RUN_PARTIAL")
        supported = [
            row
            for row in rows[:_PAGE]
            if row.target_key and row.target_key.startswith("module:") and row.tier != "unknown"
        ]
        if len(supported) != min(len(rows), _PAGE):
            gaps.add("ARCHITECTURE_UNRESOLVED_DEPENDENCIES")
        return {
            "graph": run.graph_digest,
            "contract": run.contract_digest,
            "gaps": sorted(gaps),
            "edges": self._architecture_witnesses(run, supported),
        }

    def _architecture_witnesses(
        self,
        run: ArchitectureRunRecord,
        rows: list[ArchitectureEdgeRecord],
    ) -> list[dict[str, Any]]:
        if not rows:
            return []
        keys = {key for row in rows for key in (row.source_key, row.target_key) if key}
        self.tick()
        factory = sessionmaker(
            bind=self.session.connection(), join_transaction_mode="rollback_only"
        )
        store = SqlAlchemyArchitectureStore(factory)
        graph = store._graph(
            self.session, run.id, nodes=sorted(keys), edges=[row.ordinal for row in rows]
        )
        if (
            len(graph.edges) != len(rows)
            or {node.key for node in graph.nodes} != keys
            or any(
                architecture_node_digest(node) != expected
                for node, expected in zip(graph.nodes, graph.node_digests, strict=True)
            )
            or any(
                architecture_edge_digest(edge) != expected
                for edge, expected in zip(graph.edges, graph.edge_digests, strict=True)
            )
        ):
            raise ImpactError("IMPACT_ARCHITECTURE_WITNESS_INTEGRITY_FAILED")
        links = {link for node in graph.nodes for link in node.evidence}
        links.update(link for edge in graph.edges for link in edge.evidence)
        if len(links) > MAX_VERIFIED_ROWS:
            raise ImpactError("IMPACT_VERIFICATION_LIMIT")
        states = store.verify_evidence(architecture_run(run), list(links))
        if any(states.get(link) != "verified" for link in links):
            raise ImpactError("IMPACT_ARCHITECTURE_WITNESS_INTEGRITY_FAILED")
        nodes = {node.key: node for node in graph.nodes}
        witnesses = {
            edge.key: {
                "key": edge.key,
                "digest": expected,
                "source": edge.source_key,
                "target": edge.target_key,
                "relation": edge.kind,
                "tier": edge.tier,
                "import_class": edge.import_class,
                "conditions": list(edge.conditions),
                "locator": nodes[edge.source_key].locator,
                "supporting_nodes": [
                    architecture_node_digest(nodes[key])
                    for key in (edge.source_key, edge.target_key)
                    if key
                ],
                "evidence": [[link.source, link.digest] for link in edge.evidence],
            }
            for edge, expected in zip(graph.edges, graph.edge_digests, strict=True)
        }
        return [witnesses[row.edge_key] for row in rows]

    @staticmethod
    def _input_seal(row: ImpactPreparationRecord) -> str:
        return str(
            digest(
                {
                    "version": "impact-preparation.v2",
                    "id": str(row.id),
                    "project": str(row.project_id),
                    "snapshot": str(row.snapshot_id),
                    "locator": row.locator,
                    "fingerprint": row.fingerprint,
                    "syntax_contract": row.contract,
                    "facts_digest": row.facts_digest,
                    "source": row.source_identity,
                }
            )
        )

    @staticmethod
    def _preparation(row: ImpactPreparationRecord) -> Preparation:
        if digest(row.facts) != row.facts_digest:
            raise ImpactError("IMPACT_PREPARATION_INTEGRITY_FAILED")
        if row.input_seal_digest is not None and (
            not row.source_identity or PostgresImpactView._input_seal(row) != row.input_seal_digest
        ):
            raise ImpactError("IMPACT_PREPARATION_IDENTITY_CHANGED")
        if row.input_seal_digest is None and row.source_identity is not None:
            raise ImpactError("IMPACT_PREPARATION_IDENTITY_CHANGED")
        return Preparation(
            str(row.id), row.facts_digest, row.contract, row.facts, row.input_seal_digest
        )

    def _source_identity(
        self, row: ArtifactRecord, snapshot: ProjectVersionRecord
    ) -> dict[str, Any]:
        if (
            snapshot.project_id != self.project
            or snapshot.legacy_unverified
            or row.project_version_id != snapshot.id
            or row.project_id != self.project
            or row.kind != "file"
            or row.inventory_status != "included"
            or row.fingerprint_status != "fingerprinted"
            or row.fingerprint_algorithm != "sha256"
            or not row.content_digest
        ):
            raise ImpactError("IMPACT_PREPARATION_SOURCE_UNAVAILABLE")
        return {
            "artifact_id": str(row.id),
            "artifact_identity": str(row.stable_identity_id),
            "manifest": snapshot.content_manifest,
            "policy": snapshot.policy_version,
            "fingerprint_scheme": row.fingerprint_schema_version,
            "reader": row.fingerprint_reader_version,
            "language": row.language,
            "inventory": row.inventory_status,
            "ignore": row.ignore_status,
            "exclusions": row.exclusion_reasons,
        }

    def _checked_preparation(self, row: ImpactPreparationRecord) -> Preparation:
        result = self._preparation(row)
        if row.project_id != self.project:
            raise ImpactError("IMPACT_PREPARATION_IDENTITY_CHANGED")
        if result.input_seal is None:
            return result  # Historical inspection only; never selected for cache reuse.
        self.tick()
        source = row.source_identity or {}
        snapshot = self.session.get(ProjectVersionRecord, row.snapshot_id)
        artifact = self.session.get(ArtifactRecord, UUID(str(source["artifact_id"])))
        if (
            not snapshot
            or not artifact
            or artifact.locator != row.locator
            or artifact.content_digest != row.fingerprint
            or self._source_identity(artifact, snapshot) != source
        ):
            raise ImpactError("IMPACT_PREPARATION_SOURCE_CHANGED")
        return result

    def prepare(
        self,
        authority: Authority,
        locator: str,
        fingerprint_: str,
        contract: str,
        data: dict[str, Any],
    ) -> Preparation:
        self.tick()
        snapshot = self.session.get(ProjectVersionRecord, UUID(authority.snapshot_id))
        artifact = self.session.scalar(
            select(ArtifactRecord).where(
                ArtifactRecord.project_version_id == UUID(authority.snapshot_id),
                ArtifactRecord.locator == locator,
                ArtifactRecord.kind == "file",
            )
        )
        if not snapshot or not artifact or artifact.content_digest != fingerprint_:
            raise ImpactError("IMPACT_PREPARATION_SOURCE_UNAVAILABLE")
        row = ImpactPreparationRecord(
            id=uuid4(),
            project_id=self.project,
            snapshot_id=UUID(authority.snapshot_id),
            locator=locator,
            fingerprint=fingerprint_,
            contract=contract,
            facts_digest=digest(data),
            facts=data,
            source_identity=self._source_identity(artifact, snapshot),
        )
        row.input_seal_digest = self._input_seal(row)
        self.session.add(row)
        self.deadline.remaining()
        self.session.commit()
        return self._checked_preparation(row)

    def declarations(self, authority: Authority, locator: str) -> dict[str, Any]:
        empty: dict[str, Any] = {"contract": None, "symbols": {}}
        if authority.structural_id is None:
            return empty
        self.tick()
        structure = self.session.get(StructuralAnalysisRecord, UUID(authority.structural_id))
        source = self.session.scalar(
            select(ArtifactAnalysisRecord).where(
                ArtifactAnalysisRecord.structural_analysis_id == UUID(authority.structural_id),
                ArtifactAnalysisRecord.project_version_id == UUID(authority.snapshot_id),
                ArtifactAnalysisRecord.locator == locator,
                ArtifactAnalysisRecord.snapshot_verified.is_(True),
                ArtifactAnalysisRecord.outcome == "parsed",
            )
        )
        if not structure or not source:
            return empty
        if source.language in {"Python", None}:
            contract = digest(structure.producer_contract) if structure.producer_contract else None
        else:
            extraction = self.session.get(
                ArtifactExtractionRecord, source.declaration_extraction_id
            )
            contract = (
                extraction.contract_digest
                if extraction and extraction.outcome == "extracted"
                else None
            )
        if not contract:
            return empty
        rows = self.session.scalars(
            select(SymbolRecord)
            .where(
                SymbolRecord.artifact_analysis_id == source.id,
                SymbolRecord.stable_identity_id.is_not(None),
            )
            .order_by(SymbolRecord.ordinal)
            .limit(129)
        ).all()
        if len(rows) > 128:
            return empty
        symbols = {}
        for row in rows:
            params = (row.signature or {}).get("parameters")
            bindings = (
                [
                    {
                        "name": item.get("name"),
                        "kind": item.get("kind"),
                        "has_default": item.get("has_default"),
                    }
                    for item in params
                    if isinstance(item, dict)
                ]
                if isinstance(params, list)
                else None
            )
            symbols[str(row.stable_identity_id)] = {
                "id": str(row.id),
                "name": row.qualified_name,
                "line": row.start_line,
                "binding_signature": bindings,
                "declaration_state": row.state_fingerprint,
                "proof": str(
                    digest(
                        {
                            "symbol": str(row.id),
                            "identity": str(row.stable_identity_id),
                            "artifact_analysis": str(source.id),
                            "structural": str(structure.id),
                            "fingerprint": source.input_fingerprint,
                            "producer": contract,
                            "name": row.qualified_name,
                            "signature": row.signature,
                            "state": row.state_fingerprint,
                            "span": [
                                row.start_line,
                                row.start_column,
                                row.end_line,
                                row.end_column,
                            ],
                        }
                    )
                ),
            }
        return {"contract": contract, "symbols": symbols}

    def candidates(self, authority: Authority, target: str) -> list[dict[str, Any]]:
        self.tick()
        if authority.catalog_id is None:
            return []
        rows = self.session.scalars(
            select(SemanticEntityRecord)
            .where(
                SemanticEntityRecord.catalog_run_id == UUID(authority.catalog_id),
                SemanticEntityRecord.kind == "symbol",
                SemanticEntityRecord.name == target,
            )
            .order_by(SemanticEntityRecord.locator, SemanticEntityRecord.entity_key)
            .limit(20)
        ).all()
        return [
            {"key": row.entity_key, "locator": row.locator, "line": row.start_line} for row in rows
        ]

    def _get_flow(self, authority: Authority) -> FlowRunRecord:
        if authority.flow_id is None:
            raise ImpactError("IMPACT_FLOW_AUTHORITY_UNAVAILABLE")
        flow = self._flow.get(authority.flow_id)
        if flow is None:
            raise ImpactError("IMPACT_FLOW_AUTHORITY_UNAVAILABLE")
        return flow

    def _get_node(self, authority: Authority, key: str) -> FlowNodeRecord:
        flow = self._get_flow(authority)
        pair = (str(flow.id), key)
        if pair not in self._nodes:
            row = self.session.get(FlowNodeRecord, (flow.id, key))
            if not row:
                raise ImpactError("IMPACT_FLOW_NODE_MISSING")
            self._nodes[pair] = row
        return self._nodes[pair]

    def graph_root(self, authority: Authority, entity: str) -> str:
        self.tick()
        if not authority.flow_id:
            return self._catalog_root(authority, entity)
        flow = self._get_flow(authority)
        if entity.startswith("symbol:"):
            try:
                identity = UUID(entity.removeprefix("symbol:"))
            except ValueError:
                raise ImpactError("IMPACT_ENTITY_NOT_FOUND", 404) from None
            found = self.session.execute(
                select(ArtifactRecord.locator, FlowScopeRecord.scope_key)
                .join(SymbolRecord, SymbolRecord.id == FlowScopeRecord.owner_symbol_id)
                .join(
                    ArtifactRecord,
                    ArtifactRecord.stable_identity_id == SymbolRecord.artifact_identity_id,
                )
                .where(
                    FlowScopeRecord.flow_evidence_run_id == flow.flow_evidence_run_id,
                    SymbolRecord.stable_identity_id == identity,
                    ArtifactRecord.project_version_id == flow.project_version_id,
                )
                .limit(2)
            ).all()
            if len(found) != 1:
                return self._catalog_root(authority, entity)
            entity = scope_key(*found[0])
        root = self.session.get(FlowNodeRecord, (flow.id, entity))
        if root is None:
            return self._catalog_root(authority, entity)
        self._verify(authority, [root], [])
        return entity

    def _catalog_root(self, authority: Authority, key: str) -> str:
        if authority.catalog_id is None:
            raise ImpactError("IMPACT_ENTITY_AUTHORITY_UNAVAILABLE")
        exists = self.session.scalar(
            select(SemanticEntityRecord.id)
            .where(
                SemanticEntityRecord.catalog_run_id == UUID(authority.catalog_id),
                SemanticEntityRecord.entity_key == key,
            )
            .limit(1)
        )
        if not exists:
            raise ImpactError("IMPACT_ENTITY_NOT_FOUND", 404)
        return f"catalog:{key}"

    def _catalog_frontier(self, authority: Authority, key: str) -> Frontier:
        assert authority.catalog_id is not None
        self.tick()
        rows = list(
            self.session.scalars(
                select(SemanticRelationRecord)
                .where(
                    SemanticRelationRecord.catalog_run_id == UUID(authority.catalog_id),
                    SemanticRelationRecord.target_entity_key == key.removeprefix("catalog:"),
                )
                .order_by(SemanticRelationRecord.ordinal)
                .limit(_PAGE + 1)
            )
        )
        gaps = ["CATALOG_STRUCTURAL_EXPOSURE_ONLY", "CATALOG_SLICE_UNSEALED"]
        if len(rows) > _PAGE:
            gaps.append("RELATIONSHIP_PAGE_LIMIT")
        return Frontier(
            tuple(
                Transfer(f"catalog:{row.id}", f"catalog:{row.source_entity_key}", "structural")
                for row in rows[:_PAGE]
                if row.kind in {"imports", "references", "inherits"}
            ),
            tuple(gaps),
        )

    def _catalog_subject_matches(
        self,
        catalog_id: UUID,
        key: str | None,
        refs: list[dict[str, object]],
        index: object,
    ) -> bool:
        if type(index) is not int or not 0 <= index < len(refs) or key is None:
            return False
        entity = self.session.scalar(
            select(SemanticEntityRecord).where(
                SemanticEntityRecord.catalog_run_id == catalog_id,
                SemanticEntityRecord.entity_key == key,
            )
        )
        if entity is None:
            return False
        ref = refs[index]
        try:
            owner = UUID(str(ref.get("id")))
        except ValueError:
            return False
        if ref.get("table") == "symbols":
            symbol = self.session.get(SymbolRecord, owner)
            return bool(
                symbol
                and symbol.stable_identity_id == entity.symbol_identity_id
                and symbol.stable_identity_id is not None
            )
        if ref.get("table") == "artifacts":
            artifact = self.session.get(ArtifactRecord, owner)
            return bool(
                artifact
                and artifact.stable_identity_id == entity.artifact_identity_id
                and artifact.stable_identity_id is not None
            )
        return False

    def _catalog_witness(self, authority: Authority, path: tuple[str, ...]) -> list[dict[str, Any]]:
        assert authority.catalog_id is not None
        values = []
        for identity in path:
            lookup = (authority.catalog_id, identity)
            cached = self._catalog_verified.get(lookup)
            if cached is not None:
                values.append(cached)
                continue
            self.tick()
            relation = self.session.get(
                SemanticRelationRecord, UUID(identity.removeprefix("catalog:"))
            )
            if relation is None or str(relation.catalog_run_id) != authority.catalog_id:
                raise ImpactError("IMPACT_WITNESS_INTEGRITY_FAILED")
            fact = self.session.scalar(
                select(SemanticFactRecord).where(
                    SemanticFactRecord.catalog_run_id == relation.catalog_run_id,
                    SemanticFactRecord.fact_digest == relation.fact_digest,
                )
            )
            if fact is None or fact.subject_entity_key != relation.source_entity_key:
                raise ImpactError("IMPACT_WITNESS_INTEGRITY_FAILED")
            # Relation facts carry the same direction and target in their derivation.
            derivation = fact.derived
            if (
                derivation.get("relation") != relation.kind
                or derivation.get("resolution") != relation.resolution_status
            ):
                raise ImpactError("IMPACT_WITNESS_INTEGRITY_FAILED")
            layout = derivation.get("layout")
            if not isinstance(layout, dict) or not self._catalog_subject_matches(
                relation.catalog_run_id,
                relation.target_entity_key,
                fact.source_refs,
                layout.get("target"),
            ):
                raise ImpactError("IMPACT_WITNESS_INTEGRITY_FAILED")
            if len(self._catalog_verified) + len(self._verified) >= MAX_VERIFIED_ROWS:
                raise ImpactError("IMPACT_VERIFICATION_LIMIT")
            store = SqlAlchemySemanticCatalogStore(sessionmaker(bind=self.session.connection()))
            item = _fact(fact)
            if store.verify_facts([item]).get(item.fact_digest) != "verified":
                raise ImpactError("IMPACT_WITNESS_INTEGRITY_FAILED")
            assert relation.target_entity_key is not None
            value = {
                "key": identity,
                "source": relation.source_entity_key,
                "target": relation.target_entity_key,
                "relation": relation.kind,
                "digest": fact.fact_digest,
                "tier": "inferred",
                "reached_label": relation.source_entity_key,
                "catalog": authority.catalog_id,
            }
            self._catalog_verified[lookup] = value
            values.append(value)
        if any(
            first["source"] != second["target"]
            for first, second in zip(values, values[1:], strict=False)
        ):
            raise ImpactError("IMPACT_WITNESS_INTEGRITY_FAILED")
        return values

    def frontier(self, authority: Authority, key: CellKey) -> Frontier:
        if key.subject.startswith("catalog:"):
            return self._catalog_frontier(authority, key.subject)
        flow = self._get_flow(authority)
        cache_key = (str(flow.id), key.subject, key.mechanism)
        known = self._adjacency.get(cache_key)
        if known is not None:
            return known
        self.tick()
        rows = list(
            self.session.scalars(
                select(FlowEdgeRecord)
                .where(
                    FlowEdgeRecord.flow_run_id == flow.id,
                    FlowEdgeRecord.target_key == key.subject,
                )
                .order_by(FlowEdgeRecord.edge_key.collate("C"))
                .limit(_PAGE + 1)
            )
        )
        gaps = {"STATIC_FLOW_EXPOSURE_ONLY"}
        if len(rows) > _PAGE:
            gaps.add("RELATIONSHIP_PAGE_LIMIT")
        else:
            gaps.update(self._verify_slice(flow, f"in:{key.subject}", rows))
        result = []
        for row in rows[:_PAGE]:
            if row.relation not in _INBOUND:
                continue
            self._edges[(str(flow.id), row.edge_key)] = row
            conditions = tuple(f"model:{condition}" for condition in sorted(row.conditions))
            mechanism = "shared_state" if row.relation == "on_table" else "structural"
            if key.mechanism == "shared_state":
                mechanism = key.mechanism
            result.append(Transfer(row.edge_key, row.source_key, mechanism, conditions))
        page = Frontier(tuple(result), tuple(sorted(gaps)))
        self._adjacency[cache_key] = page
        return page

    def endpoint(
        self, authority: Authority, scope: str, path: str, method: str
    ) -> EndpointObservation | None:
        if scope.startswith("catalog:"):
            return None
        flow = self._get_flow(authority)
        self.tick()
        links = list(
            self.session.scalars(
                select(FlowEdgeRecord)
                .where(
                    FlowEdgeRecord.flow_run_id == flow.id,
                    FlowEdgeRecord.target_key == scope,
                )
                .order_by(FlowEdgeRecord.edge_key.collate("C"))
                .limit(_PAGE + 1)
            )
        )
        if len(links) > _PAGE:
            return None
        gaps = self._verify_slice(flow, f"in:{scope}", links)
        matches = []
        for link in links:
            if link.relation != "handled_by":
                continue
            node = self._get_node(authority, link.source_key)
            methods, paths = node.attributes.get("methods"), node.attributes.get("paths")
            if (
                node.kind == "api_endpoint"
                and isinstance(methods, list)
                and method in methods
                and isinstance(paths, list)
                and any(isinstance(item, dict) and item.get("path") == path for item in paths)
            ):
                matches.append((link, node))
        if len(matches) != 1:
            return None
        link, node = matches[0]
        self._verify(authority, [node, self._get_node(authority, scope)], [link])
        self._edges[(str(flow.id), link.edge_key)] = link
        return EndpointObservation(str(node.node_key), link.edge_key, tuple(sorted(gaps)))

    def requests_to(self, authority: Authority, endpoint: str) -> tuple[list[str], set[str]]:
        flow = self._get_flow(authority)
        self.tick()
        rows = list(
            self.session.scalars(
                select(FlowEdgeRecord)
                .where(
                    FlowEdgeRecord.flow_run_id == flow.id,
                    FlowEdgeRecord.target_key == endpoint,
                )
                .order_by(FlowEdgeRecord.edge_key.collate("C"))
                .limit(_PAGE + 1)
            )
        )
        gaps = (
            {"RELATIONSHIP_PAGE_LIMIT"}
            if len(rows) > _PAGE
            else self._verify_slice(
                flow,
                f"in:{endpoint}",
                rows,
            )
        )
        selected = []
        for row in rows[:_PAGE]:
            if row.relation != "calls_endpoint":
                continue
            self._edges[(str(flow.id), row.edge_key)] = row
            selected.append(row.edge_key)
        return selected, gaps

    def callers(self, authority: Authority, scope: str) -> tuple[list[CallObservation], set[str]]:
        flow = self._get_flow(authority)
        self.tick()
        incoming = list(
            self.session.scalars(
                select(FlowEdgeRecord)
                .where(
                    FlowEdgeRecord.flow_run_id == flow.id,
                    FlowEdgeRecord.target_key == scope,
                )
                .order_by(FlowEdgeRecord.edge_key.collate("C"))
                .limit(_PAGE + 1)
            )
        )
        gaps = (
            {"RELATIONSHIP_PAGE_LIMIT"}
            if len(incoming) > _PAGE
            else self._verify_slice(
                flow,
                f"in:{scope}",
                incoming,
            )
        )
        observations = []
        for edge in incoming[:_PAGE]:
            if edge.relation != "invokes" or edge.attributes.get("normal_continuation") is False:
                if edge.relation in {"awaits", "invokes_callback", "dispatch_candidate"}:
                    gaps.add("RETURN_TRANSFER_INVOCATION_UNSUPPORTED")
                continue
            if self.deadline.near(0.25):
                gaps.add("RETURN_TRANSFER_DEADLINE")
                break
            call = self._observed_call(
                authority, flow, edge, gaps, ("MODELED_CALL_TARGET", "NORMAL_RETURN_REQUIRED")
            )
            if call is not None:
                observations.append(call)
        return observations, gaps

    def _observed_call(
        self,
        authority: Authority,
        flow: FlowRunRecord,
        edge: FlowEdgeRecord,
        gaps: set[str],
        conditions: tuple[str, ...],
    ) -> CallObservation | None:
        self.tick()
        call = self._get_node(authority, edge.source_key)
        if not call.locator or call.kind != "callsite":
            return None
        containing = list(
            self.session.scalars(
                select(FlowEdgeRecord)
                .where(
                    FlowEdgeRecord.flow_run_id == flow.id,
                    FlowEdgeRecord.target_key == call.node_key,
                    FlowEdgeRecord.relation == "contains_callsite",
                )
                .limit(2)
            )
        )
        if len(containing) != 1:
            gaps.add("CALLSITE_SCOPE_UNAVAILABLE")
            return None
        scope_edge = containing[0]
        owner = self._get_node(authority, scope_edge.source_key)
        assert edge.target_key is not None
        callee = self._get_node(authority, edge.target_key)
        self._verify(authority, [call, owner, callee], [edge, scope_edge])
        items = list(
            self.session.scalars(
                select(FlowEvidenceItemRecord)
                .where(
                    FlowEvidenceItemRecord.flow_run_id == flow.id,
                    FlowEvidenceItemRecord.digest.in_(call.evidence),
                    FlowEvidenceItemRecord.kind == "flow_callsite",
                )
                .limit(2)
            )
        )
        if len(items) != 1 or len(items[0].refs) != 1:
            gaps.add("CALLSITE_SOURCE_EVIDENCE_UNAVAILABLE")
            return None
        ref = items[0].refs[0]
        ids = ref.get("ids")
        if (
            ref.get("locator") != call.locator
            or not isinstance(ids, dict)
            or ids.get("run") != str(flow.flow_evidence_run_id)
        ):
            raise ImpactError("IMPACT_WITNESS_INTEGRITY_FAILED")
        artifact_id = UUID(str(ids["artifact_analysis"]))
        record = self.session.get(
            FlowCallsiteRecord,
            (
                flow.flow_evidence_run_id,
                artifact_id,
                ids["scope"],
                ids["ordinal"],
            ),
        )
        coverage = self.session.get(
            FlowArtifactCoverageRecord, (flow.flow_evidence_run_id, artifact_id)
        )
        if record is None or coverage is None or not coverage.input_fingerprint:
            gaps.add("CALLSITE_SOURCE_EVIDENCE_UNAVAILABLE")
            return None
        span = (record.start_line, record.start_column, record.end_line, record.end_column)
        if (
            span[0] is None
            or span[1] is None
            or span[2] is None
            or span[3] is None
            or owner.node_key != scope_key(call.locator, record.scope_key)
        ):
            gaps.add("CALLSITE_SOURCE_EVIDENCE_UNAVAILABLE")
            return None
        return CallObservation(
            call.locator,
            owner.node_key,
            coverage.input_fingerprint,
            (span[0], span[1], span[2], span[3]),
            edge.edge_key,
            scope_edge.edge_key,
            tuple(sorted({*edge.conditions, *conditions})),
        )

    def database_calls(
        self,
        authority: Authority,
        table: str,
    ) -> tuple[list[tuple[str, str, CallObservation]], set[str]]:
        flow = self._get_flow(authority)
        self.tick()
        incoming = list(
            self.session.scalars(
                select(FlowEdgeRecord)
                .where(
                    FlowEdgeRecord.flow_run_id == flow.id,
                    FlowEdgeRecord.target_key == table,
                )
                .order_by(FlowEdgeRecord.edge_key.collate("C"))
                .limit(_PAGE + 1)
            )
        )
        gaps = (
            {"RELATIONSHIP_PAGE_LIMIT"}
            if len(incoming) > _PAGE
            else self._verify_slice(flow, f"in:{table}", incoming)
        )
        relations = [row for row in incoming[:_PAGE] if row.relation == "on_table"]
        if len(relations) > 24:
            gaps.add("DATABASE_CALLSITE_LIMIT")
        observations = []
        for row in relations[:24]:
            if self.deadline.near(0.35):
                gaps.add("DEADLINE")
                break
            self.tick()
            operations = list(
                self.session.scalars(
                    select(FlowEdgeRecord)
                    .where(
                        FlowEdgeRecord.flow_run_id == flow.id,
                        FlowEdgeRecord.target_key == row.source_key,
                        FlowEdgeRecord.relation == "performs",
                    )
                    .limit(2)
                )
            )
            if len(operations) != 1:
                gaps.add("DATABASE_CALLSITE_UNAVAILABLE")
                continue
            call = self._observed_call(
                authority, flow, operations[0], gaps, ("MODELED_TABLE_BINDING",)
            )
            if call:
                items = self.session.scalars(
                    select(FlowEvidenceItemRecord).where(
                        FlowEvidenceItemRecord.flow_run_id == flow.id,
                        FlowEvidenceItemRecord.kind == "architecture_element",
                        FlowEvidenceItemRecord.digest.in_(row.evidence),
                    )
                )
                keys = {
                    key
                    for item in items
                    for ref in item.refs
                    if ref.get("table") == "architecture_edges"
                    and isinstance(ids := ref.get("ids"), dict)
                    and isinstance(key := ids.get("key"), str)
                    and key.startswith("maps_table|")
                }
                if len(keys) == 1:
                    observations.append((row.edge_key, str(next(iter(keys))), call))
                else:
                    gaps.add("DATABASE_MODEL_BINDING_UNAVAILABLE")
        return observations, gaps

    def _verify(
        self, authority: Authority, nodes: list[FlowNodeRecord], edges: list[FlowEdgeRecord]
    ) -> None:
        flow = self._get_flow(authority)
        nodes = [row for row in nodes if (str(flow.id), row.node_key) not in self._verified]
        edges = [row for row in edges if (str(flow.id), row.edge_key) not in self._verified]
        if not nodes and not edges:
            return
        self.tick()
        if any(row.node_digest != _node(row).element_digest() for row in nodes) or any(
            row.edge_digest != _edge(row).element_digest() for row in edges
        ):
            raise ImpactError("IMPACT_WITNESS_INTEGRITY_FAILED")
        refs = {item for row in (*nodes, *edges) for item in row.evidence}
        if len(self._verified) + len(nodes) + len(edges) + len(refs) > MAX_VERIFIED_ROWS:
            raise ImpactError("IMPACT_VERIFICATION_LIMIT")
        # Reuse the Stage 15 cited-row verifier on the same MVCC view as the actual witness.
        upstream = SqlAlchemyFlowStore(lambda: self.session)
        try:
            failures = upstream._verify_evidence(self.session, _run(flow), refs)
        except KeyError:
            raise ImpactError("IMPACT_WITNESS_INTEGRITY_FAILED") from None
        if failures:
            raise ImpactError("IMPACT_WITNESS_INTEGRITY_FAILED")
        self._verified.update((str(flow.id), row.node_key) for row in nodes)
        self._verified.update((str(flow.id), row.edge_key) for row in edges)
        self._verified.update((str(flow.id), ref) for ref in refs)

    def witness(self, authority: Authority, path: tuple[str, ...]) -> list[dict[str, Any]]:
        if path and path[0].startswith("catalog:"):
            return self._catalog_witness(authority, path)
        flow = self._get_flow(authority)
        rows = []
        for identity in path:
            row = self._edges.get((str(flow.id), identity))
            if row is None:
                self.tick()
                row = self.session.scalar(
                    select(FlowEdgeRecord).where(
                        FlowEdgeRecord.flow_run_id == flow.id,
                        FlowEdgeRecord.edge_key == identity,
                    )
                )
            if row is None or row.target_key is None:
                raise ImpactError("IMPACT_WITNESS_INTEGRITY_FAILED")
            rows.append(row)
        nodes = {}
        for row in rows:
            assert row.target_key is not None
            for key in (row.source_key, row.target_key):
                nodes[key] = self._get_node(authority, key)
        for previous, current in zip(rows, rows[1:], strict=False):
            if previous.source_key != current.target_key:
                raise ImpactError("IMPACT_WITNESS_INTEGRITY_FAILED")
        self._verify(authority, list(nodes.values()), rows)
        return [
            {
                **_edge(row).fields(),
                "digest": row.edge_digest,
                "evidence": row.evidence,
                "reached_label": nodes[row.source_key].label,
                "locator": nodes[row.source_key].locator,
                "source_digest": nodes[row.source_key].node_digest,
                "target_digest": nodes[str(row.target_key)].node_digest,
            }
            for row in rows
        ]

    def _verify_slice(self, flow: FlowRunRecord, key: str, rows: list[FlowEdgeRecord]) -> set[str]:
        flow_id = str(flow.id)
        if flow_id not in self._seal_records:
            self._seal_records[flow_id] = self.session.get(ImpactSealRecord, flow.id)
        seal = self._seal_records[flow_id]
        if seal is None:
            return {"FLOW_SLICE_UNSEALED"}
        if (
            seal.graph_digest != flow.graph_digest
            or len(seal.shards) != _SHARDS
            or (digest([_SEAL, seal.graph_digest, seal.shards]) != seal.seal_digest)
        ):
            raise ImpactError("IMPACT_FLOW_SEAL_INTEGRITY_FAILED")
        shard = int(digest(key)[:2], 16)
        identity = (flow_id, shard)
        if identity not in self._shards:
            row = self.session.get(ImpactSealShardRecord, (flow.id, shard))
            if not row or digest(row.members) != seal.shards[shard]:
                raise ImpactError("IMPACT_FLOW_SEAL_INTEGRITY_FAILED")
            self._shards[identity] = row.members
        expected = self._shards[identity].get(key, _members([]))
        if _members(rows) != expected:
            raise ImpactError("IMPACT_FLOW_SLICE_MEMBERSHIP_CHANGED")
        return set()

    def seal(self, authority: Authority) -> dict[str, Any]:
        flow = self._get_flow(authority)
        existing = self.session.get(ImpactSealRecord, flow.id)
        if existing:
            self._verify_slice(flow, "", [])
            return {"id": existing.seal_digest, "reused": True}
        if flow.contract.get("flow_model") != FLOW_MODEL_VERSION:
            raise ImpactError("IMPACT_FLOW_SEAL_INCOMPARABLE")
        self.tick()
        diagnostics: list[str] = []
        for item in self.session.scalars(
            select(FlowDiagnosticRecord)
            .where(
                FlowDiagnosticRecord.flow_run_id == flow.id,
            )
            .limit(30_001)
        ):
            diagnostics.append(FlowDiagnostic(item.code, item.subject, item.attributes).digest)
            if len(diagnostics) > 30_000:
                raise ImpactError("IMPACT_FLOW_SEAL_LIMIT")
        graph = hashlib.sha256()
        graph.update(
            b'{"contract":'
            + _json(FLOW_MODEL_VERSION)
            + b',"diagnostics":'
            + _json(sorted(diagnostics))
            + b',"edges":['
        )
        buckets: dict[str, tuple[int, Any]] = {}
        count = 0
        edge_query = (
            select(FlowEdgeRecord)
            .where(FlowEdgeRecord.flow_run_id == flow.id)
            .order_by(FlowEdgeRecord.edge_key.collate("C"))
            .execution_options(yield_per=1000)
        )
        for index, row in enumerate(self.session.scalars(edge_query)):
            self.deadline.remaining()
            if count >= _MAX_SEAL_ELEMENTS:
                raise ImpactError("IMPACT_FLOW_SEAL_LIMIT")
            count += 1
            if _edge(row).element_digest() != row.edge_digest:
                raise ImpactError("IMPACT_FLOW_SEAL_INTEGRITY_FAILED")
            value = _json([row.edge_key, row.edge_digest])
            graph.update((b"," if index else b"") + value)
            keys = [f"out:{row.source_key}"]
            if row.target_key is not None:
                keys.append(f"in:{row.target_key}")
            for key in keys:
                size, state = buckets.get(key) or (0, hashlib.sha256())
                state.update(value + b"\n")
                buckets[key] = (size + 1, state)
        graph.update(b'],"nodes":[')
        self.tick()
        node_query = (
            select(FlowNodeRecord)
            .where(FlowNodeRecord.flow_run_id == flow.id)
            .order_by(FlowNodeRecord.node_key.collate("C"))
            .execution_options(yield_per=1000)
        )
        for index, node in enumerate(self.session.scalars(node_query)):
            self.deadline.remaining()
            if count >= _MAX_SEAL_ELEMENTS:
                raise ImpactError("IMPACT_FLOW_SEAL_LIMIT")
            count += 1
            node_digest = _node(node).element_digest()
            if node_digest != node.node_digest:
                raise ImpactError("IMPACT_FLOW_SEAL_INTEGRITY_FAILED")
            graph.update((b"," if index else b"") + _json([node.node_key, node_digest]))
        graph.update(b"]}")
        if graph.hexdigest() != flow.graph_digest:
            raise ImpactError("IMPACT_FLOW_RUN_MEMBERSHIP_CHANGED")
        shards: list[dict[str, object]] = [{} for _ in range(_SHARDS)]
        for key, (size, state) in buckets.items():
            self.deadline.remaining()
            shards[int(digest(key)[:2], 16)][key] = [size, state.hexdigest()]
        hashes = [digest(item) for item in shards]
        seal_digest = digest([_SEAL, flow.graph_digest, hashes])
        self.tick()
        self.session.add(
            ImpactSealRecord(
                flow_run_id=flow.id,
                graph_digest=flow.graph_digest,
                seal_digest=seal_digest,
                shards=hashes,
            )
        )
        self.session.flush()
        self.session.add_all(
            ImpactSealShardRecord(flow_run_id=flow.id, shard=i, members=members)
            for i, members in enumerate(shards)
        )
        self.deadline.remaining()
        self.session.commit()
        return {"id": seal_digest, "reused": False, "element_count": count}

    def save(self, result: dict[str, Any]) -> None:
        self.tick()
        authorities = [item for item in result["authority"].values() if item]
        for captured in sorted(authorities, key=lambda item: item["snapshot_id"]):
            if captured["historical"]:
                continue
            version = self.session.scalar(
                select(ProjectVersionRecord)
                .where(ProjectVersionRecord.id == UUID(captured["snapshot_id"]))
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if not version or version.project_id != self.project:
                raise ImpactError("IMPACT_AUTHORITY_CHANGED")
            for name, selected in (
                ("active_flow_run_id", "flow_id"),
                ("active_analysis_id", "structural_id"),
                ("active_semantic_catalog_run_id", "catalog_id"),
                ("active_architecture_run_id", "architecture_id"),
            ):
                value = getattr(version, name)
                expected = captured[selected]
                if expected is not None and str(value) != expected:
                    raise ImpactError("IMPACT_AUTHORITY_CHANGED")
        self.session.execute(
            insert(ImpactReportRecord)
            .values(
                project_id=self.project,
                result_digest=result["result_digest"],
                request_digest=result["request_digest"],
                payload=result,
            )
            .on_conflict_do_nothing()
        )
        self.deadline.remaining()
        self.session.commit()

    def report(self, result_id: str) -> dict[str, Any] | None:
        self.tick()
        row = self.session.get(ImpactReportRecord, (self.project, result_id))
        return row.payload if row else None

    def history(self) -> list[dict[str, Any]]:
        self.tick()
        rows = self.session.execute(
            select(
                ImpactReportRecord.result_digest,
                ImpactReportRecord.created_at,
                ImpactReportRecord.payload["mode"].astext,
                ImpactReportRecord.payload["status"].astext,
            )
            .where(ImpactReportRecord.project_id == self.project)
            .order_by(ImpactReportRecord.created_at.desc(), ImpactReportRecord.result_digest)
            .limit(50)
        )
        return [
            {
                "result_digest": key,
                "created_at": stamped.isoformat(),
                "mode": mode,
                "status": status,
            }
            for key, stamped, mode, status in rows
        ]

    def _verify_comparisons(
        self,
        result: dict[str, Any],
        prepared: dict[str, Preparation],
        reasons: set[str],
        syntax_contract: str,
    ) -> None:
        comparisons = {row["id"]: row for row in result.get("comparisons", [])}
        for comparison in comparisons.values():
            self.deadline.remaining()
            identity = comparison["id"]
            if digest({k: v for k, v in comparison.items() if k != "id"}) != identity:
                raise ImpactError("IMPACT_COMPARISON_INTEGRITY_FAILED")
            old = prepared.get(comparison.get("old_preparation", ""))
            new = prepared.get(comparison.get("new_preparation", ""))
            before = old.data if old else comparison.get("old_facts")
            after = new.data if new else comparison.get("new_facts")
            if comparison["authority"] == "hypothesis" and before is not None:
                if comparison["new_digest"] != result.get("candidate_digest"):
                    raise ImpactError("IMPACT_COMPARISON_INTEGRITY_FAILED")
            if comparison["authority"] == "candidate":
                overlay = result.get("overlay", {})
                files = [
                    row
                    for row in overlay.get("files", [])
                    if row["locator"] == comparison["locator"]
                ]
                if (
                    overlay.get("id") != comparison.get("overlay")
                    or digest({k: v for k, v in overlay.items() if k != "id"}) != overlay.get("id")
                    or len(files) != 1
                    or files[0]["old_facts"] != comparison["old_digest"]
                    or files[0]["candidate_facts"] != comparison["new_digest"]
                ):
                    raise ImpactError("IMPACT_OVERLAY_INTEGRITY_FAILED")
            if (
                before is None
                or after is None
                or digest(before) != comparison["old_digest"]
                or digest(after) != comparison["new_digest"]
            ):
                raise ImpactError("IMPACT_COMPARISON_INTEGRITY_FAILED")
            if (
                result.get("contract") != CONTRACT
                or result.get("syntax_contract") != syntax_contract
            ):
                reasons.add("COMPARISON_CONTRACT_UNVERIFIED")
                continue
            changes, findings, _ = compare_syntax(
                before, after, comparison["locator"], comparison["authority"]
            )
            expected = {row["id"]: row for row in changes}
            for actual in result["changes"]:
                if actual.get("comparison_id") == identity:
                    row = expected.get(actual["id"])
                    if row is None or any(actual.get(k) != v for k, v in row.items()):
                        raise ImpactError("IMPACT_COMPARISON_INTEGRITY_FAILED")
            for actual in result["findings"]:
                if actual.get("comparison_id") == identity and not any(
                    all(actual.get(k) == v for k, v in row.items()) for row in findings
                ):
                    raise ImpactError("IMPACT_LOCAL_WITNESS_INTEGRITY_FAILED")
        for finding in result["findings"]:
            local = finding.get("witness")
            if isinstance(local, dict) and "steps" not in local:
                if finding.get("comparison_id") not in comparisons:
                    reasons.add("LOCAL_WITNESS_PROVENANCE_UNVERIFIED")
        for change in result["changes"]:
            if (
                change.get("authority") in {"snapshot", "hypothesis", "candidate"}
                and (change.get("comparison_id") not in comparisons)
                and change.get("property") != "artifact_bytes"
            ):
                reasons.add("LOCAL_COMPARISON_PROVENANCE_UNVERIFIED")

    def _verify_retained_authority(self, captured: dict[str, Any], reasons: set[str]) -> None:
        self.tick()
        version = self.session.get(ProjectVersionRecord, UUID(captured["snapshot_id"]))
        if not version or version.project_id != self.project:
            raise ImpactError("IMPACT_SNAPSHOT_NOT_FOUND", 404)
        manifest = str(version.content_manifest["digest"]) if version.content_manifest else None
        if manifest != captured["manifest"]:
            raise ImpactError("IMPACT_SNAPSHOT_IDENTITY_CHANGED")
        if (version.scan_status == "complete" and not version.legacy_unverified) != captured[
            "inventory_complete"
        ]:
            raise ImpactError("IMPACT_SNAPSHOT_IDENTITY_CHANGED")
        for field, active in (
            ("structural_id", "active_analysis_id"),
            ("catalog_id", "active_semantic_catalog_run_id"),
            ("architecture_id", "active_architecture_run_id"),
            ("flow_id", "active_flow_run_id"),
        ):
            current = getattr(version, active)
            if (str(current) if current else None) != captured[field]:
                reasons.add("SOURCE_AUTHORITY_HISTORICAL")
        for field, table in (
            ("structural_id", StructuralAnalysisRecord),
            ("catalog_id", SemanticCatalogRunRecord),
            ("architecture_id", ArchitectureRunRecord),
        ):
            if captured[field]:
                row = self.session.get(table, UUID(captured[field]))
                if row is None or str(row.project_version_id) != captured["snapshot_id"]:
                    raise ImpactError("IMPACT_RETAINED_AUTHORITY_MISSING")
        if captured["flow_id"]:
            flow = self.session.get(FlowRunRecord, UUID(captured["flow_id"]))
            if (
                flow is None
                or flow.project_id != self.project
                or str(flow.project_version_id) != captured["snapshot_id"]
                or flow.graph_digest != captured["flow_digest"]
                or flow.contract_digest != captured["flow_contract"]
                or digest(flow.contract) != flow.contract_digest
            ):
                raise ImpactError("IMPACT_FLOW_AUTHORITY_CHANGED")
            self._flow[str(flow.id)] = flow
            if flow.contract.get("flow_model") != FLOW_MODEL_VERSION:
                reasons.add("FLOW_CONTRACT_CHANGED")

    def _verify_declaration_changes(self, result: dict[str, Any], reasons: set[str]) -> None:
        cache: dict[tuple[str, str], dict[str, Any]] = {}
        for change in result["changes"]:
            if change.get("authority") != "retained_structural_observation":
                continue
            for side, prefix in (("old", "old"), ("new", "new")):
                captured = result["authority"].get(side)
                if not captured:
                    raise ImpactError("IMPACT_DECLARATION_EVIDENCE_MISSING")
                key = side, change["locator"]
                if key not in cache:
                    cache[key] = self.declarations(Authority(**captured), change["locator"])
                declarations = cache[key]
                matches = [
                    row
                    for row in declarations["symbols"].values()
                    if row["id"] == change[f"{prefix}_evidence"]
                ]
                if len(matches) != 1 or declarations["contract"] != change["producer"]:
                    raise ImpactError("IMPACT_DECLARATION_EVIDENCE_CHANGED")
                row = matches[0]
                value = change["before" if side == "old" else "after"]
                if row[change["property"]] != value:
                    raise ImpactError("IMPACT_DECLARATION_EVIDENCE_CHANGED")
                proof = change.get(f"{prefix}_proof")
                if proof is None:
                    reasons.add("LEGACY_DECLARATION_PROVENANCE_UNVERIFIED")
                elif row["proof"] != proof:
                    raise ImpactError("IMPACT_DECLARATION_EVIDENCE_CHANGED")

    def verify_report(self, result: dict[str, Any], syntax_contract: str) -> list[str]:
        self.tick()
        reasons: set[str] = set()
        for captured in result["authority"].values():
            if captured:
                self._verify_retained_authority(captured, reasons)
        for change in result["changes"]:
            if change.get("property") != "artifact_bytes":
                continue
            for side, value in (("old", "before"), ("new", "after")):
                authority = Authority(**result["authority"][side])
                artifact = self.artifact(authority, change["locator"])
                if (artifact.fingerprint if artifact else None) != change[value]:
                    raise ImpactError("IMPACT_ARTIFACT_EVIDENCE_CHANGED")
                key = f"{side}_artifact"
                if key not in change:
                    reasons.add("LEGACY_ARTIFACT_PROVENANCE_UNVERIFIED")
                elif (asdict(artifact) if artifact else None) != change[key]:
                    raise ImpactError("IMPACT_ARTIFACT_EVIDENCE_CHANGED")
            absent_side = {"introduced": "old", "removed": "new"}.get(change["comparison"])
            if absent_side and not self.absent(
                Authority(**result["authority"][absent_side]), change["locator"]
            ):
                raise ImpactError("IMPACT_ARTIFACT_ABSENCE_UNVERIFIED")
        captured_rules = result.get("architecture_rules")
        if captured_rules:
            rules = self.architecture_rule_set(captured_rules["version"])
            if (
                not rules
                or str(rules.id) != captured_rules["id"]
                or rules.digest != captured_rules["digest"]
            ):
                raise ImpactError("IMPACT_ARCHITECTURE_RULE_INTEGRITY_FAILED")
        prepared = {}
        for ref in result.get("preparations", []):
            row = self.session.get(ImpactPreparationRecord, UUID(ref["id"]))
            if (
                row is None
                or row.project_id != self.project
                or self._checked_preparation(row).digest != ref["digest"]
            ):
                raise ImpactError("IMPACT_PREPARATION_INTEGRITY_FAILED")
            prepared_value = self._checked_preparation(row)
            if ref.get("input_seal") is None:
                reasons.add("LEGACY_PREPARATION_PROVENANCE_UNVERIFIED")
            elif prepared_value.input_seal != ref["input_seal"]:
                raise ImpactError("IMPACT_PREPARATION_IDENTITY_CHANGED")
            prepared[ref["id"]] = prepared_value
        self._verify_comparisons(result, prepared, reasons, syntax_contract)
        self._verify_declaration_changes(result, reasons)
        for side, captured in result["authority"].items():
            if not captured:
                continue
            paths = [
                path
                for finding in result["findings"]
                for path in [
                    *finding.get("witnesses", []),
                    *(
                        [finding["witness"]]
                        if isinstance(finding.get("witness"), dict)
                        and "steps" in finding["witness"]
                        else []
                    ),
                ]
                if path.get("side", finding.get("side", "old")) == side
            ]
            original = Authority(**captured)
            if captured_rules:
                run = self._architecture_run(original)
                if run is None or run.graph_digest != captured_rules[f"{side}_graph"]:
                    raise ImpactError("IMPACT_ARCHITECTURE_AUTHORITY_CHANGED")
                architecture_paths = [
                    path for path in paths if path.get("namespace") == "architecture"
                ]
                keys = {step["key"] for path in architecture_paths for step in path["steps"]}
                if keys:
                    self.tick()
                    rows = list(
                        self.session.scalars(
                            select(ArchitectureEdgeRecord).where(
                                ArchitectureEdgeRecord.architecture_run_id == run.id,
                                ArchitectureEdgeRecord.edge_key.in_(keys),
                            )
                        )
                    )
                    verified = {row["key"]: row for row in self._architecture_witnesses(run, rows)}
                    for path in architecture_paths:
                        for step in path["steps"]:
                            if (
                                verified.get(step["key"]) != step
                                or path["conditions"] != step["conditions"]
                            ):
                                raise ImpactError("IMPACT_ARCHITECTURE_WITNESS_INTEGRITY_FAILED")
                paths = [path for path in paths if path.get("namespace") != "architecture"]
            if not paths:
                continue
            if not original.flow_id or all(
                path["steps"] and path["steps"][0]["key"].startswith("catalog:") for path in paths
            ):
                model = (
                    self.session.get(SemanticCatalogRunRecord, UUID(original.catalog_id))
                    if original.catalog_id
                    else None
                )
                if (
                    not model
                    or model.project_id != self.project
                    or str(model.project_version_id) != original.snapshot_id
                ):
                    raise ImpactError("IMPACT_CATALOG_AUTHORITY_CHANGED")
                for path in paths:
                    checked = self.witness(original, tuple(item["key"] for item in path["steps"]))
                    if checked != path["steps"]:
                        raise ImpactError("IMPACT_WITNESS_INTEGRITY_FAILED")
                continue
            for path in paths:
                checked = self.witness(original, tuple(item["key"] for item in path["steps"]))
                if checked != path["steps"]:
                    raise ImpactError("IMPACT_WITNESS_INTEGRITY_FAILED")
        if result["mode"] == "patch":
            reasons.add("OVERLAY_REPLAY_REQUIRES_ORIGINAL_BYTES")
        return sorted(reasons)
