"""Stage 16 application service shared by the HTTP and CLI adapters."""

from __future__ import annotations

import base64
import binascii
import re
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from threading import Event
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from buildlens.application.impact_architecture import compare_dependencies
from buildlens.application.impact_properties import argument_problem, compare_syntax, hypothetical
from buildlens.application.source_ports import SourceRead, SourceReadError
from buildlens.domain.architecture_rules import RuleSet
from buildlens.domain.eligibility import exclusion_reason
from buildlens.domain.impact import (
    CONTRACT,
    MAX_ARTIFACTS,
    MAX_ORIGINS,
    MAX_REPORT_BYTES,
    CellKey,
    Frontier,
    Side,
    walk,
)
from buildlens.domain.impact_patch import (
    MAX_PATCH_BYTES,
    MAX_SOURCE_BYTES,
    PatchRejected,
    apply_patch,
    fingerprint,
    parse_patch,
)
from buildlens.domain.source import ContentRejectedError, digest, screen_content

INTERACTIVE_SECONDS = 3.0
PREPARATION_SECONDS = 300.0
PREPARATION_ARTIFACTS = 64
PREPARATION_BYTES = 32 * 1024 * 1024
SUPPORTED = {".py", ".ts", ".tsx", ".js", ".jsx"}
FRAMEWORK_MANIFESTS = {"pyproject.toml", "package.json"}


class ImpactError(Exception):
    def __init__(self, code: str, status: int = 409) -> None:
        self.code, self.status = code, status
        super().__init__(code)


class SnapshotSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_id: UUID
    flow_run_id: UUID | None = None


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal[
        "require_parameter",
        "remove_parameter",
        "remove_return_field",
        "set_return_boolean",
        "set_integer_threshold",
        "rename_symbol",
        "remove_symbol",
        "remove_api_field",
        "require_api_field",
        "rename_api_field",
        "set_endpoint_path",
        "set_endpoint_method",
        "remove_database_column",
        "set_database_constraint",
        "set_configuration_default",
    ]
    target: str = Field(pattern=r"^[^\W\d]\w{0,127}$")
    member: str = Field(default="", max_length=128, pattern=r"^(?:[^\W\d]\w{0,127})?$")
    value: bool | int | str | None = None
    new_name: str = Field(default="", pattern=r"^(?:[^\W\d]\w{0,127})?$")
    family: Literal["query", "body", "response"] | None = None
    constraint: Literal["nullable", "unique", "primary_key"] | None = None

    @model_validator(mode="after")
    def check_operation(self) -> Operation:
        if isinstance(self.value, str) and len(self.value) > 2048:
            raise ValueError("Operation value exceeds its bound.")
        needs_name = self.kind in {"rename_symbol", "rename_api_field"}
        api_field = self.kind in {"remove_api_field", "require_api_field", "rename_api_field"}
        if bool(self.new_name) != needs_name or (self.family is not None) != api_field:
            raise ValueError("Invalid operation fields.")
        if (self.constraint is not None) != (self.kind == "set_database_constraint"):
            raise ValueError("Invalid constraint selection.")
        return self


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["entity", "snapshots", "hypothesis", "patch"]
    before: SnapshotSelection
    after: SnapshotSelection | None = None
    entity: str | None = Field(default=None, min_length=1, max_length=2048)
    locators: list[str] = Field(default_factory=list, max_length=MAX_ARTIFACTS)
    architecture_rule_version: int | None = Field(default=None, ge=1, le=2**31 - 1)
    operations: list[Operation] = Field(default_factory=list, max_length=MAX_ORIGINS)
    patch: str | None = Field(default=None, max_length=MAX_PATCH_BYTES)
    baseline_bytes: dict[str, str] = Field(default_factory=dict, max_length=8, repr=False)
    confirm: bool = False
    save: bool = False

    @model_validator(mode="after")
    def check_mode(self) -> AnalysisRequest:
        if self.mode == "entity":
            valid = (
                self.entity is not None
                and self.after is None
                and not self.operations
                and self.patch is None
            )
        elif self.mode == "snapshots":
            valid = (
                self.after is not None
                and self.entity is None
                and not self.operations
                and self.patch is None
            )
        elif self.mode == "hypothesis":
            valid = len(self.locators) == 1 and bool(self.operations) and self.confirm
            valid = valid and self.after is None and self.patch is None and self.entity is None
        else:
            valid = self.patch is not None and self.after is None and not self.operations
            valid = valid and not self.locators and self.entity is None
        if self.architecture_rule_version is not None:
            valid = valid and self.mode == "snapshots" and 0 < len(set(self.locators)) <= 16
        if not valid or (self.baseline_bytes and self.mode != "patch"):
            raise ValueError("Invalid fields for selected impact mode.")
        if any(not _safe_locator(locator) for locator in self.locators):
            raise ValueError("Invalid project-relative locator.")
        return self


class PrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selection: SnapshotSelection
    locators: list[str] = Field(default_factory=list, max_length=PREPARATION_ARTIFACTS)
    seal_flow: bool = False


class ProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selection: SnapshotSelection
    text: str = Field(min_length=1, max_length=2048)


@dataclass(frozen=True, slots=True)
class Authority:
    snapshot_id: str
    manifest: str | None
    inventory_complete: bool
    structural_id: str | None
    catalog_id: str | None
    architecture_id: str | None
    flow_id: str | None
    flow_digest: str | None
    flow_contract: str | None
    historical: bool


@dataclass(frozen=True, slots=True)
class Artifact:
    locator: str
    fingerprint: str | None
    language: str | None
    admitted: bool
    fingerprint_scheme: str | None


@dataclass(frozen=True, slots=True)
class Preparation:
    id: str
    digest: str
    contract: str
    data: dict[str, Any]
    input_seal: str | None = None


@dataclass(frozen=True, slots=True)
class CallObservation:
    locator: str
    scope: str
    fingerprint: str
    span: tuple[int, int, int, int]
    edge: str
    contains: str
    conditions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EndpointObservation:
    node: str
    handler_edge: str
    gaps: tuple[str, ...]


class ImpactView(Protocol):
    def authority(self, selection: SnapshotSelection) -> Authority: ...
    def artifact(self, authority: Authority, locator: str) -> Artifact | None: ...
    def changed(self, before: Authority, after: Authority, limit: int) -> list[str]: ...
    def absent(self, authority: Authority, locator: str) -> bool: ...
    def root(self, authority: Authority) -> tuple[str, tuple[int, int]] | None: ...
    def preparation(self, fingerprint_: str, contract: str, locator: str) -> Preparation | None: ...
    def prepare(
        self,
        authority: Authority,
        locator: str,
        fingerprint_: str,
        contract: str,
        data: dict[str, Any],
    ) -> Preparation: ...
    def declarations(self, authority: Authority, locator: str) -> dict[str, Any]: ...
    def architecture_rule_set(self, version: int) -> RuleSet | None: ...
    def architecture_dependencies(
        self, authority: Authority, locators: list[str]
    ) -> dict[str, Any] | None: ...
    def callers(
        self, authority: Authority, scope: str
    ) -> tuple[list[CallObservation], set[str]]: ...
    def database_calls(
        self,
        authority: Authority,
        table: str,
    ) -> tuple[list[tuple[str, str, CallObservation]], set[str]]: ...
    def graph_root(self, authority: Authority, entity: str) -> str: ...
    def endpoint(
        self, authority: Authority, scope: str, path: str, method: str
    ) -> EndpointObservation | None: ...
    def requests_to(self, authority: Authority, endpoint: str) -> tuple[list[str], set[str]]: ...
    def frontier(self, authority: Authority, key: CellKey) -> Frontier: ...
    def witness(self, authority: Authority, path: tuple[str, ...]) -> list[dict[str, Any]]: ...
    def seal(self, authority: Authority) -> dict[str, Any]: ...
    def candidates(self, authority: Authority, target: str) -> list[dict[str, Any]]: ...
    def save(self, result: dict[str, Any]) -> None: ...
    def report(self, result_id: str) -> dict[str, Any] | None: ...
    def history(self) -> list[dict[str, Any]]: ...
    def verify_report(self, result: dict[str, Any], syntax_contract: str) -> list[str]: ...


class ImpactStore(Protocol):
    def open(
        self, project: UUID, deadline: Deadline, preparation: bool = False
    ) -> AbstractContextManager[ImpactView]: ...


class ImpactSource(Protocol):
    def read_with_timeout(
        self,
        root: str,
        locator: str,
        max_bytes: int,
        expected_root_identity: tuple[int, int],
        timeout: float,
    ) -> SourceRead: ...
    def extract_impact(self, locator: str, raw: bytes, timeout: float) -> dict[str, Any]: ...
    def close(self) -> None: ...


class Deadline:
    def __init__(self, seconds: float, canceled: Event | None = None) -> None:
        self.expires = time.monotonic() + seconds
        self.canceled = canceled or Event()

    def remaining(self) -> float:
        if self.canceled.is_set():
            raise ImpactError("IMPACT_CANCELED", 408)
        remaining = self.expires - time.monotonic()
        if remaining <= 0:
            raise ImpactError("IMPACT_DEADLINE", 408)
        return remaining

    def near(self, reserve: float = 0.2) -> bool:
        return self.canceled.is_set() or time.monotonic() + reserve >= self.expires


def _safe_locator(locator: str) -> bool:
    path = PurePosixPath(locator)
    return (
        0 < len(locator) <= 1024
        and not path.is_absolute()
        and ".." not in path.parts
        and path.as_posix() == locator
        and all(char.isprintable() for char in locator)
    )


def _length(result: dict[str, Any]) -> int:
    import json

    return len(json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode())


def _property_coverage(projection: dict[str, Any]) -> dict[str, str]:
    functions = projection.get("functions", [])
    module = projection.get("module_properties", [])
    routes = [row["value"] for row in module if row["property"] == "api_route_declaration"]
    observed = {
        "signature": ["signature" in function for function in functions],
        "dependence": [
            function.get("syntax") is not None and not function.get("gaps")
            for function in functions
        ],
        "integer_guard": [bool(function.get("conditional_calls")) for function in functions],
        "api_query_contract": [route.get("query_fields") is not None for route in routes],
        "api_body_contract": [route.get("body_fields") is not None for route in routes],
        "api_response_contract": [route.get("response_fields") is not None for route in routes],
        "database_schema": [True for row in module if row["property"] == "orm_column_declaration"],
        "database_column_access": [True for row in projection.get("database_accesses", [])],
        "transaction_sequence": [True for row in projection.get("transactions", [])],
        "configuration_defaults": [
            True for row in module if row["property"] == "environment_default"
        ],
        "framework_version": [
            True for row in module if row["property"] == "framework_version_declaration"
        ],
    }
    # Gaps on a function can weaken its analysis while leaving supported local facts.
    has_body_facts = any("calls" in function for function in functions)
    states = {}
    for name, flags in observed.items():
        if name == "dependence" and has_body_facts and not all(flags):
            states[name] = "partial"
        elif not any(flags):
            states[name] = "no_supported_facts"
        elif not all(flags) or not projection["complete"]:
            states[name] = "partial"
        else:
            states[name] = "available_for_supported_shapes"
    return states


_PROPOSALS = (
    (re.compile(r"require (?:parameter )?(\w+) (?:in|on) (\w+)", re.I), "require_parameter"),
    (re.compile(r"remove (?:parameter )?(\w+) from (\w+)", re.I), "remove_parameter"),
    (re.compile(r"remove return field (\w+) from (\w+)", re.I), "remove_return_field"),
    (
        re.compile(r"set return field (\w+) (?:in|on) (\w+) to (true|false)", re.I),
        "set_return_boolean",
    ),
    (
        re.compile(r"set (?:integer )?threshold (?:in|on) (\w+) to (-?\d{1,10})", re.I),
        "set_integer_threshold",
    ),
)


class ImpactService:
    def __init__(
        self,
        store: ImpactStore,
        source: Callable[[], ImpactSource],
        syntax_contract: str,
        *,
        max_source: int = MAX_SOURCE_BYTES,
    ) -> None:
        self.store, self.source, self.syntax_contract = store, source, syntax_contract
        self.max_source = min(max_source, MAX_SOURCE_BYTES)

    def interpret(
        self, project: UUID, request: ProposalRequest, deadline: Deadline | None = None
    ) -> dict[str, Any]:
        try:
            screen_content(request.text)
        except ContentRejectedError:
            raise ImpactError("PROPOSAL_SCREEN_REJECTED", 422) from None
        with self.store.open(project, deadline or Deadline(INTERACTIVE_SECONDS)) as db:
            authority = db.authority(request.selection)
            for grammar, kind in _PROPOSALS:
                match = grammar.fullmatch(request.text.strip().removesuffix("."))
                if not match:
                    continue
                args = match.groups()
                if kind == "set_integer_threshold":
                    operation = Operation(kind=kind, target=args[0], value=int(args[1]))
                else:
                    value = args[2].lower() == "true" if len(args) == 3 else None
                    operation = Operation(kind=kind, target=args[1], member=args[0], value=value)
                candidates = db.candidates(authority, operation.target)
                return {
                    "authority": asdict(authority),
                    "candidate": operation.model_dump(),
                    "targets": candidates,
                    "requires_confirmation": True,
                    "unknowns": [] if candidates else ["TARGET_UNRESOLVED"],
                }
        return {
            "candidate": None,
            "targets": [],
            "requires_confirmation": True,
            "unknowns": ["UNSUPPORTED_PROPOSAL"],
        }

    def _read(
        self,
        db: ImpactView,
        authority: Authority,
        artifact: Artifact,
        worker: ImpactSource,
        deadline: Deadline,
    ) -> bytes:
        root = db.root(authority)
        if root is None:
            raise ImpactError("VERIFIED_ROOT_UNAVAILABLE")
        raw = worker.read_with_timeout(
            root[0], artifact.locator, self.max_source, root[1], min(3, deadline.remaining())
        )
        return raw.data

    def coverage(
        self,
        project: UUID,
        selection: SnapshotSelection,
        locator: str | None = None,
        after: SnapshotSelection | None = None,
    ) -> dict[str, Any]:
        if locator is not None and not _safe_locator(locator):
            raise ImpactError("IMPACT_INVALID_LOCATOR", 422)
        with self.store.open(project, Deadline(INTERACTIVE_SECONDS)) as db:
            old = db.authority(selection)
            new = db.authority(after) if after else None
            profiles: dict[str, dict[str, Any]] = {}
            for side, authority in (("old", old), ("new", new)):
                if authority is None:
                    continue
                artifact = db.artifact(authority, locator) if locator else None
                prepared = (
                    db.preparation(artifact.fingerprint, self.syntax_contract, artifact.locator)
                    if artifact and artifact.admitted and artifact.fingerprint
                    else None
                )
                profiles[side] = {
                    "authority": asdict(authority),
                    "inventory": "admitted"
                    if artifact and artifact.admitted
                    else "unavailable"
                    if locator
                    else "snapshot",
                    "fingerprint": artifact.fingerprint if artifact else None,
                    "declarations": "available" if authority.structural_id else "unavailable",
                    "relationship_exposure": "flow"
                    if authority.flow_id
                    else "catalog"
                    if authority.catalog_id
                    else "unavailable",
                    "supplemental_properties": (
                        "prepared"
                        if prepared
                        else "unavailable"
                        if artifact is None
                        else "not_prepared"
                    ),
                    "producer": prepared.contract if prepared else None,
                    "properties": _property_coverage(prepared.data) if prepared else {},
                }
            comparison = None
            property_comparison: dict[str, str] = {}
            if new is not None:
                prior = profiles["old"]["producer"]
                latter = profiles["new"]["producer"]
                comparison = (
                    "comparable"
                    if prior and prior == latter
                    else (
                        "incomparable"
                        if prior and latter
                        else "supplemental_comparison_unavailable"
                    )
                )
                old_properties, new_properties = (
                    profiles["old"]["properties"],
                    profiles["new"]["properties"],
                )
                for name in sorted(old_properties.keys() | new_properties.keys()):
                    states = {old_properties.get(name), new_properties.get(name)}
                    if comparison != "comparable" or states & {None, "no_supported_facts"}:
                        property_comparison[name] = "comparison_unavailable"
                    elif "partial" in states:
                        property_comparison[name] = "partially_comparable"
                    else:
                        property_comparison[name] = "comparable_for_supported_shapes"
        return {
            "sides": profiles,
            "supplemental_comparison": comparison,
            "property_comparison": property_comparison,
        }

    def prepare(
        self, project: UUID, request: PrepareRequest, deadline: Deadline | None = None
    ) -> dict[str, Any]:
        deadline = deadline or Deadline(PREPARATION_SECONDS)
        completed, gaps = [], []
        seal = None
        with self.store.open(project, deadline, preparation=True) as db:
            authority = db.authority(request.selection)
            worker = self.source()
            total = 0
            try:
                for locator in sorted(set(request.locators)):
                    if deadline.near():
                        gaps.append({"locator": locator, "reason": "DEADLINE"})
                        break
                    if total >= PREPARATION_BYTES:
                        gaps.append({"locator": locator, "reason": "PREPARATION_BYTE_LIMIT"})
                        break
                    artifact = db.artifact(authority, locator) if _safe_locator(locator) else None
                    if (
                        not artifact
                        or not artifact.admitted
                        or not artifact.fingerprint
                        or (
                            PurePosixPath(locator).suffix not in SUPPORTED
                            and PurePosixPath(locator).name not in FRAMEWORK_MANIFESTS
                        )
                    ):
                        gaps.append({"locator": locator, "reason": "ARTIFACT_UNAVAILABLE"})
                        continue
                    previous = db.preparation(artifact.fingerprint, self.syntax_contract, locator)
                    if previous:
                        completed.append({"locator": locator, "id": previous.id, "reused": True})
                        continue
                    try:
                        raw = self._read(db, authority, artifact, worker, deadline)
                        total += len(raw)
                        if fingerprint(raw) != artifact.fingerprint:
                            raise ImpactError("SNAPSHOT_BYTES_UNAVAILABLE")
                        data = worker.extract_impact(locator, raw, min(3, deadline.remaining()))
                        ready = db.prepare(
                            authority, locator, artifact.fingerprint, self.syntax_contract, data
                        )
                        completed.append({"locator": locator, "id": ready.id, "reused": False})
                    except (SourceReadError, ImpactError) as error:
                        gaps.append({"locator": locator, "reason": error.code})
                if request.seal_flow and not deadline.near():
                    try:
                        seal = db.seal(authority)
                    except ImpactError as error:
                        if "INTEGRITY" in error.code or "MEMBERSHIP" in error.code:
                            raise
                        gaps.append({"locator": "", "reason": error.code})
            finally:
                worker.close()
        return {
            "authority": asdict(authority),
            "completed": completed,
            "gaps": gaps,
            "seal": seal,
            "status": "partial" if gaps or (request.seal_flow and not seal) else "complete",
        }

    def analyze(
        self, project: UUID, request: AnalysisRequest, deadline: Deadline | None = None
    ) -> dict[str, Any]:
        start = time.monotonic()
        deadline = deadline or Deadline(INTERACTIVE_SECONDS)
        attempt = str(uuid4())
        retained = False
        with self.store.open(project, deadline) as db:
            old = db.authority(request.before)
            new = db.authority(request.after) if request.after else None
            base: dict[str, Any] = {
                "contract": CONTRACT,
                "mode": request.mode,
                "authority": {"old": asdict(old), "new": asdict(new) if new else None},
                "request_digest": self._request_identity(project, request, old, new),
                "changes": [],
                "findings": [],
                "gaps": [],
                "frontier": [],
                "preparations": [],
                "syntax_contract": self.syntax_contract,
                "comparisons": [],
                "verification": "scoped",
            }
            gaps: set[str] = set()
            if request.mode == "entity":
                self._entity(db, old, request.entity or "", base, gaps, deadline)
            elif request.mode == "snapshots" and new:
                if request.architecture_rule_version is not None:
                    self._architecture_comparison(db, old, new, request, base, gaps)
                self._snapshots(db, old, new, request.locators, base, gaps, deadline)
            elif request.mode == "hypothesis":
                self._hypothesis(db, old, request, base, gaps)
                self._candidate_baseline_exposure(db, old, base, gaps, deadline)
            else:
                self._patch(db, old, request, base, gaps, deadline)
                self._candidate_baseline_exposure(db, old, base, gaps, deadline)
            base["tests"] = [
                {
                    "origin": finding["origin"],
                    "test": finding["entity"],
                    "locator": finding["locator"],
                    "potential_execution": True,
                    "property_assertions": finding.get("test_assertions", []),
                    **(
                        {"matching_input": finding["matching_input"]}
                        if "matching_input" in finding
                        else {}
                    ),
                    "condition_specific_execution": "unmeasured",
                }
                for finding in base["findings"]
                if finding.get("test_assertions") or finding.get("test_candidate")
            ]
            checked_origins = {
                test["origin"] for test in base["tests"] if test["property_assertions"]
            }
            base["verification_gaps"] = [
                {
                    "origin": row["id"],
                    "property": row["property"],
                    "locator": row["locator"],
                    "reason": "NO_APPLICABLE_ASSERTION_DISCOVERED_IN_ANALYZED_SCOPE",
                }
                for row in base["changes"]
                if row["property"]
                in {
                    "signature",
                    "return_field",
                    "integer_guard",
                    "api_response_contract",
                    "api_body_contract",
                    "api_query_contract",
                }
                and row["id"] not in checked_origins
            ]
            self._prioritize(base)
            base["gaps"] = sorted(gaps)
            base["status"] = _status(gaps)
            self._limit_size(base)
            base["result_digest"] = digest(base)
            if request.save and not deadline.near(0.05):
                db.save(base)
                retained = True
        return {
            **base,
            "attempt_id": attempt,
            "retained": retained,
            "elapsed_ms": int((time.monotonic() - start) * 1000),
        }

    @staticmethod
    def _architecture_comparison(
        db: ImpactView,
        old: Authority,
        new: Authority,
        request: AnalysisRequest,
        base: dict[str, Any],
        gaps: set[str],
    ) -> None:
        version = request.architecture_rule_version
        assert version is not None
        rules = db.architecture_rule_set(version)
        if rules is None:
            raise ImpactError("IMPACT_ARCHITECTURE_RULE_VERSION_UNAVAILABLE", 404)
        locators = sorted(set(request.locators))
        before = db.architecture_dependencies(old, locators)
        after = db.architecture_dependencies(new, locators)
        if before is None or after is None:
            gaps.add("ARCHITECTURE_AUTHORITY_UNAVAILABLE")
            return
        if before["contract"] != after["contract"]:
            gaps.add("ARCHITECTURE_AUTHORITIES_INCOMPARABLE")
            return
        base["architecture_rules"] = {
            "version": version,
            "id": str(rules.id),
            "digest": rules.digest,
            "old_graph": before["graph"],
            "new_graph": after["graph"],
        }
        changes, findings, limitations = compare_dependencies(rules.rules, before, after)
        base["changes"].extend(changes)
        base["findings"].extend(findings)
        gaps.update((*limitations, *before["gaps"], *after["gaps"]))

    def _request_identity(
        self, project: UUID, request: AnalysisRequest, old: Authority, new: Authority | None
    ) -> str:
        # Resolved authorities replace selection spelling. Preparations and verified
        # witnesses discovered during the attempt belong to the retained payload.
        normalized = request.model_dump(
            mode="json", exclude={"before", "after", "baseline_bytes", "patch", "save", "confirm"}
        )
        normalized["locators"] = sorted(set(request.locators))
        if request.patch is not None:
            try:
                normalized["patch_digest"] = fingerprint(request.patch.encode())
            except UnicodeEncodeError:
                raise ImpactError("PATCH_ENCODING", 422) from None
        authority = {
            side: {key: value for key, value in asdict(selected).items() if key != "historical"}
            for side, selected in (("old", old), ("new", new))
            if selected is not None
        }
        return str(
            digest(
                {
                    "project": str(project),
                    "request": normalized,
                    "authority": authority,
                    "contract": CONTRACT,
                    "syntax": self.syntax_contract,
                }
            )
        )

    @staticmethod
    def _prioritize(report: dict[str, Any]) -> None:
        priority = {
            "compatibility": (0, "INVOCATION_CONTRACT"),
            "control": (1, "CONDITIONAL_OPERATION"),
            "data": (2, "MATCHED_VALUE_CONSUMER"),
            "shared_state": (3, "SHARED_STATE_EXPOSURE"),
            "architecture": (3, "OWNER_DECLARED_RULE"),
            "configuration": (3, "DECLARED_CONFIGURATION"),
        }
        gaps = {item["origin"] for item in report["verification_gaps"]}
        properties = {row["id"]: row["property"] for row in report["changes"]}
        for finding in report["findings"]:
            finding.setdefault("property", properties.get(finding["origin"], "exposure"))
            order, reason = priority.get(finding["mechanism"], (4, "STRUCTURAL_EXPOSURE"))
            verification_gap = finding["origin"] in gaps
            finding["priority"] = {
                "level": "review_first" if order <= 1 else "review" if order <= 2 else "inspect",
                "reasons": [reason, *(["ASSERTION_NOT_DISCOVERED"] if verification_gap else [])],
            }
        report["findings"].sort(
            key=lambda item: (
                priority.get(item["mechanism"], (4, ""))[0],
                item["origin"] not in gaps,
                item["origin"],
                item["entity"],
            )
        )

    @staticmethod
    def _limit_size(base: dict[str, Any]) -> None:
        if _length(base) <= MAX_REPORT_BYTES - 2048:
            return
        base["status"] = "budget_limited"
        base["gaps"] = sorted(set(base["gaps"]) | {"REPORT_BYTES_LIMIT"})
        for key in ("findings", "tests", "verification_gaps", "changes", "frontier"):
            while base[key] and _length(base) > MAX_REPORT_BYTES - 2048:
                values = base[key]
                del values[len(values) // 2 :]
        if _length(base) > MAX_REPORT_BYTES - 2048:
            raise ImpactError("IMPACT_REPORT_TOO_LARGE", 413)

    def _entity(
        self,
        db: ImpactView,
        authority: Authority,
        entity: str,
        base: dict[str, Any],
        gaps: set[str],
        deadline: Deadline,
    ) -> None:
        target = db.graph_root(authority, entity)
        base["entity"] = target
        root = CellKey(entity, "old", target, "structural", "exposure")
        self._propagate(db, {"old": authority}, [root], base, gaps, deadline)

    @staticmethod
    def _propagate(
        db: ImpactView,
        authorities: dict[Side, Authority],
        roots: list[CellKey],
        base: dict[str, Any],
        gaps: set[str],
        deadline: Deadline,
        *,
        baseline_only: bool = False,
    ) -> None:
        root_set = set(roots)
        result = walk(root_set, lambda key: db.frontier(authorities[key.side], key), deadline.near)
        gaps.update(result.gaps)
        base["frontier"].extend(asdict(key) for key in sorted(result.frontier))
        for key, supports in sorted(result.supporting.items()):
            if key in root_set or not supports.records or key not in result.cells:
                continue
            if deadline.near(0.1):
                gaps.add("DEADLINE")
                break
            witnesses: list[dict[str, Any]] = []
            for term, path in supports.records:
                if deadline.near(0.1):
                    gaps.add("DEADLINE")
                    break
                try:
                    edges = db.witness(authorities[key.side], path)
                except ImpactError as error:
                    if error.code not in {"IMPACT_VERIFICATION_LIMIT", "IMPACT_DEADLINE"}:
                        raise
                    gaps.add(
                        "DEADLINE" if error.code.endswith("DEADLINE") else "VERIFICATION_LIMIT"
                    )
                    base["frontier"].append(asdict(key))
                    break
                witnesses.append({"conditions": sorted(term), "steps": edges})
            if witnesses:
                first = witnesses[0]["steps"]
                label = first[-1].get("reached_label") if first else None
                base["findings"].append(
                    {
                        "origin": key.origin,
                        "side": key.side,
                        "property": key.property,
                        "entity": key.subject,
                        "label": label,
                        "mechanism": key.mechanism,
                        "consequence": "baseline_relationship_exposure"
                        if baseline_only
                        else "potential_exposure",
                        "relationship_tier": "inferred"
                        if any(step["tier"] in {"inferred", "unknown"} for step in first)
                        else "derived",
                        "distance": "direct" if len(first) == 1 else "indirect",
                        "witnesses": witnesses,
                        "uncertainty": sorted(
                            result.cells[key].gaps
                            | ({"CANDIDATE_RELATIONSHIP_NOT_RELINKED"} if baseline_only else set())
                        ),
                    }
                )

    def _candidate_baseline_exposure(
        self,
        db: ImpactView,
        baseline: Authority,
        base: dict[str, Any],
        gaps: set[str],
        deadline: Deadline,
    ) -> None:
        changes = [
            row
            for row in base["changes"]
            if row["property"]
            in {
                "signature",
                "declaration",
                "return_field",
                "integer_guard",
                "invocations",
                "explicit_exceptions",
            }
            and row["comparison"] != "introduced"
        ]
        if len(changes) > MAX_ORIGINS:
            gaps.add("ORIGIN_LIMIT")
            base["frontier"].extend(row["id"] for row in changes[MAX_ORIGINS:])
        targets: dict[tuple[str, str], str | None] = {}
        roots = []
        for index, row in enumerate(changes[:MAX_ORIGINS]):
            if deadline.near():
                gaps.add("DEADLINE")
                base["frontier"].extend(change["id"] for change in changes[index:MAX_ORIGINS])
                break
            key = row["locator"], row["entity"]
            if key not in targets:
                targets[key] = self._symbol_root(db, baseline, *key)
            if target := targets[key]:
                roots.append(CellKey(row["id"], "old", target, "structural", row["property"]))
            else:
                gaps.add("BASELINE_DECLARATION_RELATIONSHIPS_UNAVAILABLE")
        if roots:
            self._propagate(db, {"old": baseline}, roots, base, gaps, deadline, baseline_only=True)

    @staticmethod
    def _symbol_root(db: ImpactView, authority: Authority, locator: str, entity: str) -> str | None:
        candidates = [
            candidate
            for candidate in db.candidates(authority, entity)
            if candidate["locator"] == locator
        ]
        if len(candidates) != 1:
            return None
        try:
            return db.graph_root(authority, candidates[0]["key"])
        except ImpactError as error:
            if error.code not in {
                "IMPACT_ENTITY_FLOW_LINK_UNAVAILABLE",
                "IMPACT_FLOW_NODE_MISSING",
                "IMPACT_ENTITY_NOT_FOUND",
                "IMPACT_ENTITY_AUTHORITY_UNAVAILABLE",
            }:
                raise
            return None

    def _snapshots(
        self,
        db: ImpactView,
        old: Authority,
        new: Authority,
        locators: list[str],
        base: dict[str, Any],
        gaps: set[str],
        deadline: Deadline,
    ) -> None:
        locators = sorted(set(locators)) or db.changed(old, new, MAX_ARTIFACTS + 1)
        if len(locators) > MAX_ARTIFACTS:
            gaps.add("ARTIFACT_LIMIT")
            base["frontier"] = locators[MAX_ARTIFACTS:]
        for locator in locators[:MAX_ARTIFACTS]:
            if deadline.near():
                gaps.add("DEADLINE")
                base["frontier"].append(locator)
                continue
            before, after = db.artifact(old, locator), db.artifact(new, locator)
            if before == after and before is not None and before.fingerprint:
                continue
            if (
                before is None
                and after
                and after.admitted
                and after.fingerprint
                and db.absent(old, locator)
            ):
                status = "introduced"
            elif (
                after is None
                and before
                and before.admitted
                and before.fingerprint
                and db.absent(new, locator)
            ):
                status = "removed"
            elif (
                before is None
                or after is None
                or not before.admitted
                or not after.admitted
                or not before.fingerprint
                or not after.fingerprint
            ):
                status = "unavailable"
            elif before.fingerprint_scheme != after.fingerprint_scheme:
                status = "incomparable"
            elif before.fingerprint == after.fingerprint:
                continue
            else:
                status = "modified"
            row = {
                "locator": locator,
                "property": "artifact_bytes",
                "comparison": status,
                "before": before.fingerprint if before else None,
                "after": after.fingerprint if after else None,
                "authority": "snapshot",
                "old_artifact": asdict(before) if before else None,
                "new_artifact": asdict(after) if after else None,
            }
            base["changes"].append({**row, "id": digest(row)[:24]})
            if status in {"introduced", "removed"}:
                gaps.add("ARTIFACT_MEMBERSHIP_ONLY_PROPERTIES_UNEXAMINED")
                continue
            if status != "modified" or before is None or after is None:
                gaps.add("ARTIFACT_COMPARISON_UNAVAILABLE")
                continue
            a = self._prepared(db, before, base)
            b = self._prepared(db, after, base)
            if a is None or b is None:
                gaps.add("SUPPLEMENTAL_SYNTAX_UNAVAILABLE")
                self._retained_declarations(db, old, new, locator, base, gaps)
                continue
            self._properties(
                a.data,
                b.data,
                locator,
                "snapshot",
                base,
                gaps,
                {"old_preparation": a.id, "new_preparation": b.id},
            )
        self._snapshot_exposure(db, old, new, base, gaps, deadline)

    def _snapshot_exposure(
        self,
        db: ImpactView,
        old: Authority,
        new: Authority,
        base: dict[str, Any],
        gaps: set[str],
        deadline: Deadline,
    ) -> None:
        authorities: dict[Side, Authority] = {"old": old, "new": new}
        api_properties = {
            "api_route_declaration",
            "api_query_contract",
            "api_body_contract",
            "api_response_contract",
        }
        eligible = [
            row
            for row in base["changes"]
            if row["property"]
            in {
                "signature",
                "binding_signature",
                "declaration",
                "return_field",
                "integer_guard",
                "invocations",
                "explicit_exceptions",
                "orm_column_declaration",
            }
            | api_properties
        ]
        if len(eligible) > MAX_ORIGINS:
            gaps.add("ORIGIN_LIMIT")
            base["frontier"].extend(row["id"] for row in eligible[MAX_ORIGINS:])
        roots: list[CellKey] = []
        endpoints: dict[str, dict[Side, EndpointObservation]] = {}
        identity: dict[tuple[Side, str, str], str | None] = {}
        mounts: dict[tuple[Side, str, str, str], EndpointObservation | None] = {}
        db_calls: dict[
            tuple[Side, str], tuple[list[tuple[str, str, CallObservation]], set[str]]
        ] = {}
        for row in eligible[:MAX_ORIGINS]:
            for side, authority in authorities.items():
                if deadline.near():
                    gaps.add("DEADLINE")
                    base["frontier"].append(row["id"])
                    return
                if not authority.flow_id:
                    gaps.add("SIDE_FLOW_AUTHORITY_UNAVAILABLE")
                    continue
                if row["property"] not in api_properties:
                    if (row["comparison"] == "introduced" and side == "old") or (
                        row["comparison"] == "removed" and side == "new"
                    ):
                        continue
                if row["property"] == "orm_column_declaration":
                    entity = row["old_entity"] if side == "old" else row["entity"]
                    table = entity.partition(".")[0]
                    try:
                        root = db.graph_root(authority, f"db_table:{table}")
                    except ImpactError as error:
                        if error.code not in {
                            "IMPACT_ENTITY_NOT_FOUND",
                            "IMPACT_FLOW_NODE_MISSING",
                        }:
                            raise
                        gaps.add("ORM_TABLE_FLOW_LINK_UNAVAILABLE")
                    else:
                        roots.append(
                            CellKey(row["id"], side, root, "shared_state", "table_exposure")
                        )
                        if not root.startswith("catalog:"):
                            database_key = side, root
                            if database_key not in db_calls:
                                db_calls[database_key] = db.database_calls(authority, root)
                            calls, limitations = db_calls[database_key]
                            gaps.update(limitations)
                            self._database_column_consumers(
                                db, authority, side, row, calls, base, gaps, deadline
                            )
                        matched = any(
                            finding["origin"] == row["id"]
                            and finding.get("side") == side
                            and finding["consequence"]
                            in {
                                "matched_column_read",
                                "matched_column_write_input",
                                "mapped_column_read",
                                "mapped_column_write_input",
                            }
                            for finding in base["findings"]
                        )
                        gaps.add(
                            "COLUMN_CONSUMER_LINEAGE_PARTIAL"
                            if matched
                            else "COLUMN_CONSUMER_LINEAGE_UNAVAILABLE"
                        )
                    continue
                key = (side, row["locator"], row["entity"])
                if key not in identity:
                    identity[key] = self._symbol_root(db, authority, row["locator"], row["entity"])
                if target := identity[key]:
                    if row["property"] in api_properties:
                        route = (
                            row["before" if side == "old" else "after"]
                            if row["property"] == "api_route_declaration"
                            else row[f"{side}_route"]
                        )
                        mount_key = side, target, route["path"], route["method"]
                        if mount_key not in mounts:
                            mounts[mount_key] = db.endpoint(
                                authority, target, route["path"], route["method"]
                            )
                        endpoint = mounts[mount_key]
                        if endpoint is None:
                            gaps.add("API_MOUNT_OR_ENDPOINT_ALIGNMENT_UNAVAILABLE")
                            continue
                        gaps.update(endpoint.gaps)
                        target = endpoint.node
                        endpoints.setdefault(row["id"], {})[side] = endpoint
                    roots.append(CellKey(row["id"], side, target, "structural", row["property"]))
                else:
                    gaps.add("SIDE_DECLARATION_FLOW_LINK_UNAVAILABLE")
        self._api_compatibility(db, authorities, endpoints, base, gaps, deadline)
        if roots:
            self._cross_file_signatures(db, authorities, roots, base, gaps, deadline)
            # Retain time for verified data-use witnesses before enumerating structural exposure.
            self._cross_file_returns(db, authorities, roots, base, gaps, deadline)
            if not deadline.near():
                self._propagate(db, authorities, roots, base, gaps, deadline)

    def _database_column_consumers(
        self,
        db: ImpactView,
        authority: Authority,
        side: Side,
        change: dict[str, Any],
        calls: list[tuple[str, str, CallObservation]],
        base: dict[str, Any],
        gaps: set[str],
        deadline: Deadline,
    ) -> None:
        prepared: dict[str, Preparation | None] = {}
        entity = change["old_entity"] if side == "old" else change["entity"]
        table, _, member = entity.partition(".")
        mapping_prefix = f"maps_table|module:{change['locator']}|db_table:{table}|"
        for table_edge, mapping, call in calls:
            if (
                not mapping.startswith(mapping_prefix)
                or mapping[len(mapping_prefix) :].rsplit(".", 1)[-1] != change["model"]
            ):
                continue
            if call.locator == change["locator"]:
                continue
            if deadline.near(0.25):
                gaps.add("DEADLINE")
                base["frontier"].append(change["id"])
                return
            if call.locator not in prepared:
                artifact = db.artifact(authority, call.locator)
                prepared[call.locator] = (
                    self._prepared(db, artifact, base)
                    if artifact and artifact.admitted and artifact.fingerprint == call.fingerprint
                    else None
                )
            facts = prepared[call.locator]
            if not facts:
                gaps.add("DATABASE_CONSUMER_PREPARATION_UNAVAILABLE")
                continue
            name = change["model"]
            for access in facts.data.get("database_accesses", []):
                if (
                    access["member"] != member
                    or access["span"] != list(call.span)
                    or not access["model_reference"]
                    or access["model_reference"].rsplit(".", 1)[-1] != name
                ):
                    continue
                steps = db.witness(authority, (table_edge, call.edge, call.contains))
                conditions = sorted(
                    {
                        *call.conditions,
                        *steps[0]["conditions"],
                        "DATABASE_EXECUTION_AND_DURABILITY_UNMEASURED",
                    }
                )
                base["findings"].append(
                    {
                        "origin": change["id"],
                        "entity": access["function"],
                        "locator": call.locator,
                        "side": side,
                        "mechanism": "shared_state",
                        "distance": "direct",
                        "relationship_tier": "inferred",
                        "consequence": "matched_column_read"
                        if access["direction"] == "read"
                        else "matched_column_write_input",
                        "witnesses": [
                            {
                                "conditions": conditions,
                                "steps": steps,
                                "local": {
                                    "field": entity,
                                    "line": access["line"],
                                    "preparation": facts.id,
                                    "preparation_digest": facts.digest,
                                },
                            }
                        ],
                    }
                )

    @staticmethod
    def _api_compatibility(
        db: ImpactView,
        authorities: dict[Side, Authority],
        endpoints: dict[str, dict[Side, EndpointObservation]],
        base: dict[str, Any],
        gaps: set[str],
        deadline: Deadline,
    ) -> None:
        requests: dict[str, tuple[list[str], set[str]]] = {}
        for change in base["changes"]:
            aligned = endpoints.get(change["id"], {})
            before_endpoint = aligned.get("old")
            if before_endpoint is None:
                continue
            before, after = change["before"], change["after"]
            family = change["property"]
            new_endpoint = aligned.get("new")
            if family == "api_route_declaration":
                old_route, new_route = before, after
                consequence = "old_consumer_exposed_to_route_shape_change"
                condition = "CONSUMER_STILL_SENDS_OLD_REQUEST"
            else:
                old_route, new_route = change["old_route"], change["new_route"]
                if old_route != new_route or new_endpoint is None:
                    gaps.add("CONTINUING_API_WIRE_CONTRACT_UNAVAILABLE")
                    continue
                if (
                    family == "api_query_contract"
                    and after
                    and after["required"]
                    and (before is None or not before["required"])
                ):
                    consequence = "old_request_contract_allows_omitting_new_required_query"
                    condition = "CONSUMER_OMITS_QUERY_FIELD"
                elif (
                    family == "api_body_contract"
                    and after
                    and after["required"]
                    and (before is None or not before["required"])
                ):
                    consequence = "old_request_contract_allows_omitting_new_required_body_field"
                    condition = "CONSUMER_OMITS_BODY_FIELD"
                elif family == "api_response_contract" and before and after is None:
                    consequence = "new_response_declaration_omits_old_field"
                    condition = "CONSUMER_RELIES_ON_OLD_RESPONSE_FIELD"
                else:
                    gaps.add("API_FIELD_COMPATIBILITY_UNRESOLVED")
                    continue
            old_wire = (old_route["method"], old_route["path"])
            new_wire = (new_route["method"], new_route["path"])
            if family == "api_route_declaration" and old_wire == new_wire:
                continue
            if any("{" in path or "}" in path for path in (old_wire[1], new_wire[1])):
                gaps.add("API_ROUTE_TEMPLATE_COMPARISON_UNAVAILABLE")
                continue
            if deadline.near(0.4):
                gaps.add("DEADLINE")
                break
            if before_endpoint.node not in requests:
                requests[before_endpoint.node] = db.requests_to(
                    authorities["old"], before_endpoint.node
                )
            keys, issues = requests[before_endpoint.node]
            gaps.update(issues)
            new_proof = (
                db.witness(authorities["new"], (new_endpoint.handler_edge,))
                if keys and new_endpoint is not None
                else []
            )
            if family != "api_route_declaration":
                gaps.add("HTTP_CLIENT_FIELD_BEHAVIOR_UNVERIFIED")
            for key in keys:
                if deadline.near(0.25):
                    gaps.add("DEADLINE")
                    break
                steps = db.witness(authorities["old"], (before_endpoint.handler_edge, key))
                endpoint_edge = steps[-1]
                qualifiers = sorted(
                    {
                        *(code for step in (*steps, *new_proof) for code in step["conditions"]),
                        *before_endpoint.gaps,
                        *(new_endpoint.gaps if new_endpoint else ()),
                        *issues,
                        "OLD_CONSUMER_TO_NEW_PRODUCER",
                        condition,
                        "REWRITES_AND_ALTERNATE_ROUTES_UNKNOWN",
                        *(
                            ("RUNTIME_SCHEMA_AND_CLIENT_FIELD_BEHAVIOR_UNKNOWN",)
                            if family != "api_route_declaration"
                            else ()
                        ),
                    }
                )
                base["findings"].append(
                    {
                        "origin": change["id"],
                        "side": "old",
                        "entity": endpoint_edge["source"],
                        "locator": endpoint_edge["locator"],
                        "mechanism": "compatibility",
                        "distance": "direct",
                        "consequence": consequence,
                        "relationship_tier": "inferred",
                        "witnesses": [
                            {
                                "conditions": qualifiers,
                                "side": "old",
                                "steps": steps,
                                "old_request_shape": list(old_wire),
                                "new_route_shape": list(new_wire),
                                **({"field": change["field"]} if "field" in change else {}),
                            },
                            *(
                                [{"side": "new", "steps": new_proof, "conditions": qualifiers}]
                                if new_proof
                                else []
                            ),
                        ],
                    }
                )

    @staticmethod
    def _call_in_projection(
        prepared: Preparation,
        call: CallObservation,
    ) -> tuple[dict[str, Any], int] | None:
        matches = [
            (function, index)
            for function in prepared.data.get("functions", [])
            for index, observed in enumerate(function.get("calls", []))
            if not observed.get("unpacking")
            and tuple(observed.get(name) for name in ("line", "column", "end_line", "end_column"))
            == call.span
        ]
        return matches[0] if len(matches) == 1 else None

    def _cross_file_signatures(
        self,
        db: ImpactView,
        authorities: dict[Side, Authority],
        roots: list[CellKey],
        base: dict[str, Any],
        gaps: set[str],
        deadline: Deadline,
    ) -> None:
        changes = {item["id"]: item for item in base["changes"] if item["property"] == "signature"}
        subjects = {
            (root.origin, root.side): root.subject
            for root in roots
            if not root.subject.startswith("catalog:")
        }
        examined = 0
        for origin, change in changes.items():
            old_scope, new_scope = subjects.get((origin, "old")), subjects.get((origin, "new"))
            if not old_scope or not new_scope:
                continue
            if deadline.near(0.45):
                gaps.add("DEADLINE")
                break
            before, old_gaps = db.callers(authorities["old"], old_scope)
            after, new_gaps = db.callers(authorities["new"], new_scope)
            gaps.update(old_gaps | new_gaps)
            old_calls = {(item.locator, item.fingerprint, item.span): item for item in before}
            for current in after:
                if current.locator == change["locator"]:
                    continue
                if deadline.near(0.35) or examined >= 192:
                    gaps.add("DEADLINE" if deadline.near(0.35) else "SIGNATURE_TRANSFER_LIMIT")
                    return
                examined += 1
                previous = old_calls.get((current.locator, current.fingerprint, current.span))
                if previous is None:
                    gaps.add("CONTINUING_CALLSITE_IDENTITY_UNAVAILABLE")
                    continue
                artifact = db.artifact(authorities["new"], current.locator)
                prepared = (
                    self._prepared(db, artifact, base)
                    if artifact
                    and artifact.admitted
                    and artifact.fingerprint == current.fingerprint
                    else None
                )
                if not prepared or prepared.data.get("language") != "Python":
                    gaps.add("CALLER_PROPERTY_FACTS_UNAVAILABLE")
                    continue
                selected = self._call_in_projection(prepared, current)
                if not selected:
                    gaps.add("PREPARED_CALLSITE_NOT_UNIQUELY_ALIGNED")
                    continue
                function, index = selected
                invocation = function["calls"][index]
                mismatch = argument_problem(invocation, change["after"])
                if mismatch is None or argument_problem(invocation, change["before"]):
                    continue
                observations: tuple[tuple[Side, CallObservation], ...] = (
                    ("old", previous),
                    ("new", current),
                )
                witnesses = [
                    {
                        "side": side,
                        "conditions": list(observation.conditions),
                        "steps": db.witness(
                            authorities[side], (observation.edge, observation.contains)
                        ),
                        "local": {
                            "caller": function["name"],
                            "callsite": list(observation.span),
                            "preparation": prepared.id,
                            "preparation_digest": prepared.digest,
                        },
                    }
                    for side, observation in observations
                ]
                base["findings"].append(
                    {
                        "origin": origin,
                        "side": "new",
                        "entity": function["name"],
                        "locator": current.locator,
                        "mechanism": "compatibility",
                        "distance": "direct",
                        "consequence": mismatch,
                        "relationship_tier": "inferred",
                        "witnesses": witnesses,
                        "uncertainty": [
                            "MODELED_CALL_TARGET",
                            "CONTINUING_CALLSITE_BYTE_IDENTICAL",
                        ],
                    }
                )

    def _cross_file_returns(
        self,
        db: ImpactView,
        authorities: dict[Side, Authority],
        roots: list[CellKey],
        base: dict[str, Any],
        gaps: set[str],
        deadline: Deadline,
    ) -> None:
        from collections import deque

        changes = {row["id"]: row for row in base["changes"] if row["property"] == "return_field"}
        if not changes:
            return
        signatures = {
            (row["locator"], row["entity"]): row
            for row in base["changes"]
            if row["property"] == "signature"
        }
        cache: dict[tuple[Side, str], tuple[list[CallObservation], set[str]]] = {}
        syntax: dict[tuple[Side, str, str], Preparation | None] = {}
        examined = 0
        for root in roots:
            if root.origin not in changes or root.subject.startswith("catalog:"):
                continue
            change = changes[root.origin]
            authority = authorities[root.side]
            queue: deque[tuple[str, tuple[str, ...], tuple[str, ...], frozenset[str], bool]] = (
                deque([(root.subject, (), (), frozenset({root.subject}), False)])
            )
            while queue:
                if deadline.near(0.35) or examined >= 384:
                    gaps.add(
                        "RETURN_TRANSFER_DEADLINE"
                        if deadline.near(0.35)
                        else "RETURN_TRANSFER_LIMIT"
                    )
                    base["frontier"].append(asdict(root))
                    return
                scope, trail, conditions, active, crossed = queue.popleft()
                key = root.side, scope
                if key not in cache:
                    cache[key] = db.callers(authority, scope)
                candidates, limitations = cache[key]
                gaps.update(limitations)
                for call in candidates:
                    if deadline.near(0.3) or examined >= 384:
                        gaps.add(
                            "RETURN_TRANSFER_DEADLINE"
                            if deadline.near(0.3)
                            else "RETURN_TRANSFER_LIMIT"
                        )
                        base["frontier"].append(asdict(root))
                        return
                    examined += 1
                    binding = root.side, call.locator, call.fingerprint
                    if binding not in syntax:
                        artifact = db.artifact(authority, call.locator)
                        syntax[binding] = (
                            self._prepared(db, artifact, base)
                            if artifact
                            and artifact.admitted
                            and artifact.fingerprint == call.fingerprint
                            else None
                        )
                    prepared = syntax[binding]
                    if not prepared:
                        gaps.add("CALLER_PROPERTY_FACTS_UNAVAILABLE")
                        continue
                    matched = self._call_in_projection(prepared, call)
                    if not matched:
                        gaps.add("PREPARED_CALLSITE_NOT_UNIQUELY_ALIGNED")
                        continue
                    function, index = matched
                    signature = signatures.get((change["locator"], change["entity"]))
                    if (
                        not trail
                        and signature
                        and argument_problem(
                            function["calls"][index],
                            signature["before" if root.side == "old" else "after"],
                        )
                    ):
                        gaps.add("RETURN_TRANSFER_INCOMPATIBLE_CALL")
                        continue
                    cross_file = crossed or call.locator != change["locator"]
                    path = (*trail, call.edge)
                    qualifiers = tuple(sorted(set((*conditions, *call.conditions))))
                    for read in function.get("reads", []):
                        if (
                            not cross_file
                            or read["call"] != index
                            or read["field"] != change["field"]
                        ):
                            continue
                        steps = db.witness(authority, (*path, call.contains))
                        local = {
                            "callsite": list(call.span),
                            "field": read["field"],
                            "line": read["line"],
                            "caller": function["name"],
                            "preparation": prepared.id,
                            "preparation_digest": prepared.digest,
                        }
                        base["findings"].append(
                            {
                                "origin": root.origin,
                                "side": root.side,
                                "entity": function["name"],
                                "locator": call.locator,
                                "mechanism": "data",
                                "distance": "indirect" if trail else "direct",
                                "consequence": "matched_cross_file_field_consumer",
                                "relationship_tier": "inferred",
                                "witnesses": [
                                    {"conditions": list(qualifiers), "steps": steps, "local": local}
                                ],
                                "uncertainty": ["MODELED_NORMAL_RETURN_ONLY"],
                                "test_assertions": [
                                    assertion
                                    for assertion in function.get("assertions", [])
                                    if function["name"].startswith("test_")
                                    and assertion["call"] == index
                                    and assertion["line"] == read["line"]
                                    and assertion["field"] == change["field"]
                                ],
                            }
                        )
                    returned = function.get("returns", [])
                    if len(returned) != 1 or returned[0].get("forward") != index:
                        continue
                    if call.scope in active or len(path) >= 7:
                        gaps.add("RETURN_TRANSFER_CONTEXT_LIMIT")
                        continue
                    queue.append(
                        (
                            call.scope,
                            (*path, call.contains),
                            qualifiers,
                            active | {call.scope},
                            cross_file,
                        )
                    )

    @staticmethod
    def _retained_declarations(
        db: ImpactView,
        old: Authority,
        new: Authority,
        locator: str,
        base: dict[str, Any],
        gaps: set[str],
    ) -> None:
        first, second = db.declarations(old, locator), db.declarations(new, locator)
        if not first["contract"] or first["contract"] != second["contract"]:
            gaps.add("DECLARATION_PRODUCERS_INCOMPARABLE")
            return
        for key in sorted(first["symbols"].keys() & second["symbols"].keys()):
            left, right = first["symbols"][key], second["symbols"][key]
            for prop in ("binding_signature", "declaration_state"):
                if left[prop] == right[prop] or left[prop] is None or right[prop] is None:
                    continue
                change = {
                    "entity": right["name"],
                    "locator": locator,
                    "property": prop,
                    "before": left[prop],
                    "after": right[prop],
                    "comparison": "modified",
                    "authority": "retained_structural_observation",
                    "old_line": left["line"],
                    "new_line": right["line"],
                    "old_evidence": left["id"],
                    "new_evidence": right["id"],
                    "producer": first["contract"],
                    "old_proof": left["proof"],
                    "new_proof": right["proof"],
                }
                base["changes"].append({**change, "id": digest(change)[:24]})

    def _prepared(
        self, db: ImpactView, artifact: Artifact, base: dict[str, Any]
    ) -> Preparation | None:
        if not artifact.admitted or not artifact.fingerprint:
            return None
        result = db.preparation(artifact.fingerprint or "", self.syntax_contract, artifact.locator)
        if result:
            proof = {"id": result.id, "digest": result.digest, "input_seal": result.input_seal}
            if proof not in base["preparations"]:
                base["preparations"].append(proof)
        return result

    def _hypothesis(
        self,
        db: ImpactView,
        authority: Authority,
        request: AnalysisRequest,
        base: dict[str, Any],
        gaps: set[str],
    ) -> None:
        artifact = db.artifact(authority, request.locators[0])
        if artifact is None or not artifact.admitted:
            raise ImpactError("IMPACT_ARTIFACT_UNAVAILABLE", 404)
        ready = self._prepared(db, artifact, base)
        if ready is None:
            raise ImpactError("IMPACT_PREPARATION_REQUIRED")
        candidate = hypothetical(
            ready.data, [operation.model_dump() for operation in request.operations]
        )
        if candidate is None:
            raise ImpactError("IMPACT_HYPOTHESIS_UNSUPPORTED", 422)
        base["candidate_digest"] = digest(candidate)
        self._properties(
            ready.data,
            candidate,
            artifact.locator,
            "hypothesis",
            base,
            gaps,
            {"old_preparation": ready.id, "new_facts": candidate},
        )
        gaps.add("HYPOTHESIS_CANDIDATE_LOCAL_RELATIONSHIPS_ONLY")

    def _patch(
        self,
        db: ImpactView,
        old: Authority,
        request: AnalysisRequest,
        base: dict[str, Any],
        gaps: set[str],
        deadline: Deadline,
    ) -> None:
        text = request.patch or ""
        worker = self.source()
        entries, projections = [], []
        try:
            screen_content(text)
            files = parse_patch(text)
            if set(request.baseline_bytes) - {item.locator for item in files}:
                raise PatchRejected("PATCH_UNUSED_BASELINE")
            for file in sorted(files, key=lambda item: item.locator):
                artifact = db.artifact(old, file.locator)
                if (
                    not artifact
                    or not artifact.admitted
                    or not artifact.fingerprint
                    or artifact.fingerprint_scheme != "artifact-bytes.v1"
                    or exclusion_reason(file.locator, False)
                ):
                    raise PatchRejected("PATCH_BASELINE_UNAVAILABLE")
                supplied = request.baseline_bytes.get(file.locator)
                if supplied is not None:
                    if len(supplied) > (self.max_source + 2) // 3 * 4:
                        raise PatchRejected("PATCH_BASELINE_SIZE_LIMIT")
                    raw = base64.b64decode(supplied, validate=True)
                else:
                    raw = self._read(db, old, artifact, worker, deadline)
                if len(raw) > self.max_source:
                    raise PatchRejected("PATCH_BASELINE_SIZE_LIMIT")
                value = apply_patch(file, raw, artifact.fingerprint)
                if len(value) > self.max_source:
                    raise PatchRejected("PATCH_POSTIMAGE_LIMIT")
                before = worker.extract_impact(file.locator, raw, deadline.remaining())
                after = worker.extract_impact(file.locator, value, deadline.remaining())
                entries.append(
                    {
                        "locator": file.locator,
                        "before": artifact.fingerprint,
                        "after": fingerprint(value),
                        "old_facts": digest(before),
                        "candidate_facts": digest(after),
                    }
                )
                projections.append((file.locator, before, after))
        except ImpactError:
            raise
        except (ContentRejectedError, SourceReadError, PatchRejected) as error:
            raise ImpactError(error.code, 422) from None
        except (ValueError, UnicodeError, binascii.Error):
            raise ImpactError("PATCH_BASELINE_ENCODING", 422) from None
        finally:
            worker.close()
        overlay = {
            "baseline": asdict(old),
            "patch_digest": fingerprint(text.encode()),
            "files": entries,
            "contract": self.syntax_contract,
        }
        base["overlay"] = {**overlay, "id": digest(overlay)}
        for locator, before, after in projections:
            self._properties(
                before,
                after,
                locator,
                "candidate",
                base,
                gaps,
                {"old_facts": before, "new_facts": after, "overlay": base["overlay"]["id"]},
            )
        gaps.add("OVERLAY_CROSS_FILE_RELINK_UNAVAILABLE")

    @staticmethod
    def _properties(
        before: dict[str, Any],
        after: dict[str, Any],
        locator: str,
        authority: str,
        base: dict[str, Any],
        gaps: set[str],
        proof: dict[str, Any] | None = None,
    ) -> None:
        changes, findings, reasons = compare_syntax(before, after, locator, authority)
        comparison = {
            "locator": locator,
            "authority": authority,
            "old_digest": digest(before),
            "new_digest": digest(after),
            **(proof or {}),
        }
        comparison_id = digest(comparison)
        base.setdefault("comparisons", []).append({**comparison, "id": comparison_id})
        for finding in findings:
            finding["comparison_id"] = comparison_id
        for change in changes:
            change["comparison_id"] = comparison_id
        base["changes"].extend(changes)
        base["findings"].extend(findings)
        gaps.update(reasons)
        if changes and authority in {"hypothesis", "candidate"}:
            gaps.add("CROSS_FILE_PROPERTY_PROPAGATION_UNAVAILABLE")

    def show(
        self,
        project: UUID,
        result_id: str,
        *,
        verify: bool = False,
        deadline: Deadline | None = None,
    ) -> dict[str, Any]:
        with self.store.open(project, deadline or Deadline(INTERACTIVE_SECONDS)) as db:
            result = db.report(result_id)
            if not result:
                raise ImpactError("IMPACT_REPORT_NOT_FOUND", 404)
            expected = digest(
                {key: value for key, value in result.items() if key != "result_digest"}
            )
            if result_id != expected:
                raise ImpactError("IMPACT_REPORT_INTEGRITY_FAILED")
            if verify:
                reasons = db.verify_report(result, self.syntax_contract)
                if result.get("contract") != CONTRACT:
                    reasons = sorted(set(reasons) | {"IMPACT_CONTRACT_CHANGED"})
                if result.get("syntax_contract") != self.syntax_contract:
                    reasons = sorted(set(reasons) | {"SYNTAX_CONTRACT_CHANGED"})
                return {
                    "result_digest": result_id,
                    "status": "qualified" if reasons else "verified",
                    "reasons": reasons,
                    "integrity": "unverified_legacy"
                    if any(reason.startswith("LEGACY_") for reason in reasons)
                    else "partial"
                    if any("PROVENANCE_UNVERIFIED" in reason for reason in reasons)
                    else "verified",
                    "freshness": "stale"
                    if any("AUTHORITY_HISTORICAL" in reason for reason in reasons)
                    else "current",
                    "compatibility": "changed"
                    if any("CONTRACT_CHANGED" in reason for reason in reasons)
                    else "current",
                    "evidence_verification": "partial"
                    if any(
                        "UNVERIFIED" in reason or "REQUIRES_ORIGINAL" in reason
                        for reason in reasons
                    )
                    else "complete",
                    "analysis_completeness": result.get("status", "unknown"),
                }
            return result

    def history(self, project: UUID) -> list[dict[str, Any]]:
        with self.store.open(project, Deadline(INTERACTIVE_SECONDS)) as db:
            return db.history()


def _status(gaps: set[str]) -> str:
    if gaps & {"DEADLINE", "RETURN_TRANSFER_DEADLINE"}:
        return "deadline"
    if gaps & {
        "ARTIFACT_LIMIT",
        "STATE_LIMIT",
        "RELATIONSHIP_LIMIT",
        "CONTEXT_LIMIT",
        "CONDITION_ATOMS_LIMIT",
        "CONDITION_ALTERNATIVES_LIMIT",
        "RELATIONSHIP_PAGE_LIMIT",
        "ORIGIN_LIMIT",
        "VERIFICATION_LIMIT",
        "RETURN_TRANSFER_LIMIT",
        "RETURN_TRANSFER_CONTEXT_LIMIT",
        "SIGNATURE_TRANSFER_LIMIT",
        "ARCHITECTURE_EDGE_LIMIT",
        "ARCHITECTURE_RULE_LIMIT",
        "DATABASE_CALLSITE_LIMIT",
    }:
        return "budget_limited"
    return "evidence_incomplete" if gaps else "complete_for_scope"
