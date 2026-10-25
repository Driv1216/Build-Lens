"""Stage 13 controlled semantic generation (Milestone C): plans, runs and the index.

Every outbound request is authorised by a persisted, single-use plan (§11.1), budgeted in a
T1 transaction that mints a single-use ticket, re-checked against current authority and the
exact planned bytes, sent only through ``OutboundGate`` (§11.2) and recorded under the run's
DB-time fencing predicate (§11.3). Nothing here runs automatically.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from buildlens.application.outbound_gate import (
    OutboundGate,
    ProviderCallError,
    StructuredGenerator,
)
from buildlens.application.semantic_catalog import (
    EntityFilters,
    SemanticCatalogService,
    SemanticCatalogStore,
)
from buildlens.application.semantic_prompt import (
    FULL_INSTRUCTIONS,
    OUTPUT_SCHEMA,
    Package,
    PackageIntegrityError,
    build_package,
    prompt_data,
)
from buildlens.application.semantic_validation import ValidationResult, validate
from buildlens.domain.semantic import SemanticCatalogRun, SemanticEntity
from buildlens.domain.semantic_generation import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    TEMPLATE_TOKEN_ALLOWANCE,
    VALIDATOR_VERSION,
    Attempt,
    EntryView,
    GenerationPlan,
    GenerationRun,
    IndexEntry,
    Interpretation,
    PlanItem,
    PlanMode,
    ProviderConfig,
    RunState,
    input_key,
)


class GenerationDisabledError(Exception):
    pass


class GenerationNotFoundError(Exception):
    pass


class PlanNotFoundError(Exception):
    pass


class PlanInvalidError(Exception):
    pass


class PlanExpiredError(Exception):
    pass


class PlanConfigChangedError(Exception):
    pass


class PlanChangedError(Exception):
    pass


class PlanAlreadyUsedError(Exception):
    pass


class GenerationConflictError(Exception):
    """Another run is executing on this snapshot (unique partial index)."""


class RunFencedError(Exception):
    """The fencing predicate failed: this process may send or write nothing more."""


class BudgetExhaustedError(Exception):
    pass


RETENTION_NOTES = {
    "local": "Loopback endpoint on this machine; BuildLens cannot prove the model is local.",
    "gemini": "Sent to the Gemini Developer API; retention follows Google's API terms.",
    "openai": "Sent to the OpenAI Responses API with store=false; retention follows OpenAI's "
    "API data policy.",
}


@dataclass(frozen=True, slots=True)
class GenerationSettings:
    enabled: bool = False
    max_output_tokens: int = 2048
    request_byte_ceiling: int = 64_000
    max_outbound_requests: int = 10
    max_attempts_per_entity: int = 2
    deadline_seconds: float = 300
    plan_ttl_seconds: float = 1800
    request_timeout_seconds: float = 60
    publication_margin_seconds: float = 10
    max_retry_wait_seconds: float = 60
    selection_limit: int = 200
    retain_rejected_output: bool = False

    @property
    def lease_seconds(self) -> float:
        """Per-request timeout plus the longest retry wait plus a margin (§4.4.5)."""
        return self.request_timeout_seconds + self.max_retry_wait_seconds + 30


@dataclass(frozen=True, slots=True)
class PlanSelection:
    entity_keys: tuple[str, ...] = ()
    locator_prefix: str | None = None
    kinds: tuple[str, ...] = ()
    stale_only: bool = False
    mode: PlanMode = "generate"
    accept_profiles: tuple[str, ...] | Literal["any"] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_keys": list(self.entity_keys),
            "locator_prefix": self.locator_prefix,
            "kinds": list(self.kinds),
            "stale_only": self.stale_only,
        }


@dataclass(frozen=True, slots=True)
class Reusable:
    interpretation_id: UUID
    profile_digest: str
    provider: str
    returned_model: str | None


@dataclass(frozen=True, slots=True)
class NewPlan:
    project_id: UUID
    snapshot_id: UUID
    selection: dict[str, Any]
    mode: PlanMode
    accept_profiles: list[str] | Literal["any"] | None
    config: ProviderConfig
    catalog_run_id: UUID
    structural_analysis_id: UUID
    processing_run_id: UUID
    index_run_id: UUID | None
    caps: dict[str, Any]
    totals: dict[str, Any]
    contributing_locators: list[dict[str, Any]]
    retention_note: str
    items: tuple[PlanItem, ...]
    ttl_seconds: float


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    outcome: str
    returned_model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    response_digest: str | None = None
    rejection_codes: tuple[str, ...] = ()
    retained_output: str | None = None


@dataclass(slots=True)
class RunResults:
    entries: dict[str, IndexEntry] = field(default_factory=dict)
    generated: int = 0
    reused: int = 0
    failed: int = 0
    not_attempted: int = 0
    stop_reason: str | None = None


class GenerationStore(Protocol):
    def active_index_run_id(self, project_id: UUID, snapshot_id: UUID) -> UUID | None: ...
    def reusable(
        self, project_id: UUID, input_keys: Sequence[str]
    ) -> dict[str, list[Reusable]]: ...
    def insert_plan(self, plan: NewPlan) -> GenerationPlan: ...
    def plan(self, project_id: UUID, snapshot_id: UUID, plan_id: UUID) -> GenerationPlan | None: ...
    def admit(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        plan_id: UUID,
        plan_digest: str,
        config_digest: str,
        lease_seconds: float,
    ) -> tuple[GenerationRun, datetime]: ...
    def begin_attempt(
        self, run: GenerationRun, item: PlanItem, lease_seconds: float
    ) -> Attempt: ...
    def authority_unchanged(self, run: GenerationRun) -> bool: ...
    def existing_candidate(
        self, project_id: UUID, input_key: str, profile_digest: str
    ) -> UUID | None: ...
    def record_outcome(
        self,
        run: GenerationRun,
        attempt: Attempt,
        outcome: OutcomeRecord,
        interpretation: Interpretation | None,
        lease_seconds: float,
    ) -> None: ...
    def finish(self, run: GenerationRun, state: RunState, results: RunResults) -> GenerationRun: ...
    def abandon_expired(self, project_id: UUID, snapshot_id: UUID) -> list[UUID]: ...
    def self_abandon(self, run: GenerationRun) -> bool: ...
    def run(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> GenerationRun | None: ...
    def runs(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[GenerationRun], int]: ...
    def attempts(self, run_id: UUID) -> list[Attempt]: ...
    def index_entries(self, index_run_id: UUID) -> dict[str, tuple[IndexEntry, Interpretation]]: ...
    def interpretation(
        self, project_id: UUID, interpretation_id: UUID
    ) -> Interpretation | None: ...
    def history(self, project_id: UUID, entity_key: str) -> list[Interpretation]: ...
    def resolve_citations(
        self, project_id: UUID, interpretation: Interpretation
    ) -> Interpretation: ...


def _manifest(package: Package, catalog_run_id: UUID) -> dict[str, Any]:
    entity = package.entity
    hashes = {chunk.stable_key: chunk.content_hash for chunk in entity.evidence}
    return {
        "package_digest": package.package_digest,
        "content_digest": entity.content_digest,
        "context_coverage": entity.context_coverage,
        "chunks": [[key, hashes.get(key)] for key in entity.package_chunks],
        "facts": sorted(entity.package_facts),
        "neighbors": list(entity.neighbor_facts),
        "omitted": [[item.entity_key, item.start_line, item.end_line] for item in entity.omitted],
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "catalog_run_id": str(catalog_run_id),
    }


def stale_reasons(manifest: dict[str, Any], entity: SemanticEntity | None) -> tuple[str, ...]:
    """Manifest diff against the current catalog entity (§5.3)."""
    if entity is None:
        return ("ENTITY_REMOVED",)
    if (
        manifest.get("prompt_version") != PROMPT_VERSION
        or manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("validator_version") != VALIDATOR_VERSION
    ):
        return ("CONTRACT_CHANGED",)
    reasons: list[str] = []
    if entity.context_coverage == "not_eligible":
        return ("CONTEXT_COVERAGE_CHANGED",)
    hashes = {chunk.stable_key: chunk.content_hash for chunk in entity.evidence}
    if manifest.get("chunks") != [[key, hashes.get(key)] for key in entity.package_chunks]:
        reasons.append("SOURCE_CHANGED")
    if manifest.get("facts") != sorted(entity.package_facts):
        reasons.append("FACTS_CHANGED")
    if manifest.get("neighbors") != list(entity.neighbor_facts):
        reasons.append("NEIGHBOR_FACTS_CHANGED")
    if manifest.get("context_coverage") != entity.context_coverage or manifest.get("omitted") != [
        [item.entity_key, item.start_line, item.end_line] for item in entity.omitted
    ]:
        reasons.append("CONTEXT_COVERAGE_CHANGED")
    return tuple(reasons) or ("CONTRACT_CHANGED",)


class SemanticGenerationService:
    def __init__(
        self,
        catalog: SemanticCatalogService,
        catalog_store: SemanticCatalogStore,
        store: GenerationStore,
        generator_factory: Callable[[], StructuredGenerator],
        settings: GenerationSettings,
    ) -> None:
        self._catalog, self._catalog_store, self._store = catalog, catalog_store, store
        self._factory, self._settings = generator_factory, settings

    # ----------------------------------------------------------------- configuration

    def configuration(self) -> dict[str, Any]:
        """Configuration only: constructing and inspecting the generator sends nothing."""
        generator = self._factory()
        config = generator.config
        return {
            "enabled": self._settings.enabled,
            "provider": config.provider,
            "model": config.model,
            "sharing_class": config.sharing_class,
            "provider_config_digest": config.digest,
            "generator_profile_digest": config.profile_digest,
            "adapter_version": config.adapter_version,
            "status": generator.status(),
            "retention_note": RETENTION_NOTES[config.provider],
            "caps": {
                **self._caps(0),
                "max_outbound_requests": self._settings.max_outbound_requests,
            },
            "request_byte_ceiling": self._settings.request_byte_ceiling,
            "plan_ttl_seconds": self._settings.plan_ttl_seconds,
        }

    def _caps(self, generate_count: int) -> dict[str, Any]:
        s = self._settings
        return {
            "max_attempts_per_entity": s.max_attempts_per_entity,
            "max_outbound_requests": min(
                s.max_outbound_requests, generate_count * s.max_attempts_per_entity
            ),
            "deadline_seconds": s.deadline_seconds,
            "max_output_tokens": s.max_output_tokens,
            "request_timeout_seconds": s.request_timeout_seconds,
        }

    # ----------------------------------------------------------------- packages

    def package(self, run: SemanticCatalogRun, entity: SemanticEntity) -> Package:
        store = self._catalog_store
        evidence = store.evidence(run, entity.evidence)
        own = store.facts(run.id, entity.package_facts)
        neighbors = store.facts(run.id, entity.neighbor_facts)
        verification = store.verify_facts([*own, *neighbors])
        own = [replace(fact, verification=verification.get(fact.fact_digest)) for fact in own]
        neighbors = [
            replace(fact, verification=verification.get(fact.fact_digest)) for fact in neighbors
        ]
        omitted_keys = [item.entity_key for item in entity.omitted]
        neighbor_subjects = sorted({fact.subject_entity_key for fact in neighbors})
        included_keys = sorted(
            {
                f"symbol:{chunk.symbol_identity_id}"
                for chunk in entity.evidence
                if chunk.symbol_identity_id and chunk.stable_key in set(entity.package_chunks)
            }
        )
        names = store.names(run.id, [*omitted_keys, *neighbor_subjects, *included_keys])
        included = {entity.name, entity.qualified_name or entity.name}
        for key in included_keys:
            for value in names.get(key, ()):
                included.add(value)
        return build_package(
            entity,
            evidence,
            own,
            neighbors,
            {key: names.get(key, ("",))[-1] for key in omitted_keys},
            [value for key in neighbor_subjects for value in names.get(key, ())],
            included,
            store.fact_rows(own),
            store.artifact_ranges(run.structural_analysis_id, entity.artifact_identity_id),
        )

    def _render(self, generator: StructuredGenerator, package: Package) -> Any:
        return generator.render(
            FULL_INSTRUCTIONS, prompt_data(package), OUTPUT_SCHEMA, self._settings.max_output_tokens
        )

    # ----------------------------------------------------------------- entry state (§5.3)

    def entry_views(
        self, project_id: UUID, snapshot_id: UUID, keys: Sequence[str] | None = None
    ) -> tuple[SemanticCatalogRun, dict[str, EntryView]]:
        run, _ = self._catalog.select(project_id, snapshot_id, None)
        index_run = self._store.active_index_run_id(project_id, snapshot_id)
        entries = self._store.index_entries(index_run) if index_run else {}
        entities = self._entities(run, keys)
        views: dict[str, EntryView] = {}
        for key, entity in entities.items():
            found = entries.get(key)
            if found is None:
                views[key] = EntryView(None, "absent", (), None, index_run)
                continue
            entry, interpretation = found
            current = (
                entity is not None
                and entity.content_digest is not None
                and interpretation.input_key == input_key(entity.content_digest)
            )
            views[key] = EntryView(
                entry,
                "current" if current else "stale",
                () if current else stale_reasons(interpretation.manifest, entity),
                interpretation,
                index_run,
            )
        return run, views

    def _entities(
        self, run: SemanticCatalogRun, keys: Sequence[str] | None
    ) -> dict[str, SemanticEntity | None]:
        if keys is not None:
            return {key: self._catalog_store.entity(run.id, key) for key in keys}
        values: dict[str, SemanticEntity | None] = {}
        offset = 0
        while True:
            page, total = self._catalog_store.entity_page(run.id, EntityFilters(), 500, offset)
            values.update((item.entity_key, item) for item in page)
            offset += len(page)
            if not page or offset >= total:
                return values

    def entry(self, project_id: UUID, snapshot_id: UUID, entity_key: str) -> EntryView:
        _, views = self.entry_views(project_id, snapshot_id, [entity_key])
        view = views[entity_key]
        if view.interpretation is not None:
            view = replace(
                view,
                interpretation=self._store.resolve_citations(project_id, view.interpretation),
            )
        return view

    def history(self, project_id: UUID, snapshot_id: UUID, entity_key: str) -> list[EntryView]:
        """Every interpretation for this entity across snapshots and profiles."""
        self._catalog.require_snapshot(project_id, snapshot_id)
        run, _ = self._catalog.select(project_id, snapshot_id, None)
        entity = self._catalog_store.entity(run.id, entity_key)
        values: list[EntryView] = []
        for interpretation in self._store.history(project_id, entity_key):
            current = bool(
                entity is not None
                and entity.content_digest
                and interpretation.input_key == input_key(entity.content_digest)
            )
            values.append(
                EntryView(
                    None,
                    "current" if current else "stale",
                    () if current else stale_reasons(interpretation.manifest, entity),
                    interpretation,
                    None,
                )
            )
        return values

    def interpretation(
        self, project_id: UUID, snapshot_id: UUID, interpretation_id: UUID
    ) -> Interpretation:
        self._catalog.require_snapshot(project_id, snapshot_id)
        value = self._store.interpretation(project_id, interpretation_id)
        if value is None:
            raise GenerationNotFoundError("Interpretation was not found.")
        return self._store.resolve_citations(project_id, value)

    # ----------------------------------------------------------------- plan (§4.4.1, §11.1)

    def plan(self, project_id: UUID, snapshot_id: UUID, selection: PlanSelection) -> GenerationPlan:
        if selection.mode == "generate" and not self._settings.enabled:
            raise GenerationDisabledError("Semantic generation is not enabled.")
        if selection.mode == "reuse_only" and not selection.accept_profiles:
            raise ValueError("reuse_only requires accept_profiles.")
        run, _ = self._catalog.select(project_id, snapshot_id, None)
        index_run = self._store.active_index_run_id(project_id, snapshot_id)
        generator = self._factory()
        config = generator.config
        entities = self._select(project_id, snapshot_id, run, selection)
        packages: dict[str, Package] = {}
        reasons: dict[str, str] = {}
        for entity in entities:
            if entity.context_coverage == "not_eligible":
                reasons[entity.entity_key] = entity.context_reason or "NOT_ELIGIBLE"
                continue
            try:
                built = self.package(run, entity)
            except PackageIntegrityError:
                reasons[entity.entity_key] = "PACKAGE_INTEGRITY"
                continue
            if not built.chunks and not any(not fact.neighbor for fact in built.facts):
                reasons[entity.entity_key] = "EMPTY_PACKAGE"
                continue
            packages[entity.entity_key] = built
        reusable = self._store.reusable(
            project_id, sorted({package.input_key for package in packages.values()})
        )
        items: list[PlanItem] = []
        locators: dict[tuple[str, str], int] = {}
        for entity in entities:
            key = entity.entity_key
            package = packages.get(key)
            if package is None:
                items.append(
                    PlanItem(
                        len(items),
                        key,
                        "ineligible",
                        reasons[key],
                        entity.context_coverage,
                        entity.package_digest,
                        None,
                        None,
                        0,
                    )  # fmt: skip
                )
                continue
            choice = self._reuse_choice(reusable.get(package.input_key, []), config, selection)
            if choice is not None:
                items.append(
                    PlanItem(
                        len(items),
                        key,
                        "reuse",
                        None,
                        entity.context_coverage,
                        package.package_digest,
                        package.input_key,
                        None,
                        0,
                        (),
                        choice.interpretation_id,
                        choice.profile_digest,
                        choice.provider,
                        choice.returned_model,
                    )  # fmt: skip
                )
                continue
            if selection.mode == "reuse_only":
                items.append(
                    PlanItem(
                        len(items),
                        key,
                        "ineligible",
                        "NO_REUSABLE_INTERPRETATION",
                        entity.context_coverage,
                        package.package_digest,
                        package.input_key,
                        None,
                        0,
                    )  # fmt: skip
                )
                continue
            canonical = self._render(generator, package)
            if canonical.size > self._settings.request_byte_ceiling:
                items.append(
                    PlanItem(
                        len(items),
                        key,
                        "ineligible",
                        "REQUEST_BYTES_EXCEEDED",
                        entity.context_coverage,
                        package.package_digest,
                        package.input_key,
                        None,
                        canonical.size,
                    )  # fmt: skip
                )
                continue
            contributions = package.contributions()
            for item in contributions:
                slot = (item.locator, item.role)
                locators[slot] = locators.get(slot, 0) + item.bytes
            items.append(
                PlanItem(
                    len(items),
                    key,
                    "generate",
                    None,
                    entity.context_coverage,
                    package.package_digest,
                    package.input_key,
                    canonical.digest,
                    canonical.size,
                    contributions,
                )  # fmt: skip
            )
        generate = [item for item in items if item.disposition == "generate"]
        totals = {
            "entities": len(items),
            "generate": len(generate),
            "reuse": sum(item.disposition == "reuse" for item in items),
            "ineligible": sum(item.disposition == "ineligible" for item in items),
            "outbound_bytes_per_attempt": sum(item.request_bytes for item in generate),
        }
        return self._store.insert_plan(
            NewPlan(
                project_id,
                snapshot_id,
                selection.as_dict(),
                selection.mode,
                list(selection.accept_profiles)
                if isinstance(selection.accept_profiles, tuple)
                else selection.accept_profiles,
                config,
                run.id,
                run.structural_analysis_id,
                run.processing_run_id,
                index_run,
                self._caps(len(generate)),
                totals,
                [
                    {"locator": locator, "role": role, "bytes": size}
                    for (locator, role), size in sorted(locators.items())
                ],
                RETENTION_NOTES[config.provider],
                tuple(items),
                self._settings.plan_ttl_seconds,
            )
        )

    @staticmethod
    def _reuse_choice(
        candidates: list[Reusable], config: ProviderConfig, selection: PlanSelection
    ) -> Reusable | None:
        """Source freshness is input-key equality; profile compatibility is explicit (§4.4.3)."""
        for candidate in candidates:
            if selection.mode == "generate":
                if candidate.profile_digest == config.profile_digest:
                    return candidate
            elif selection.accept_profiles == "any" or (
                selection.accept_profiles and candidate.profile_digest in selection.accept_profiles
            ):
                return candidate
        return None

    def _select(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        run: SemanticCatalogRun,
        selection: PlanSelection,
    ) -> list[SemanticEntity]:
        limit = self._settings.selection_limit
        if selection.entity_keys:
            if len(selection.entity_keys) > limit:
                raise ValueError("Too many entities were selected.")
            values = [self._catalog_store.entity(run.id, key) for key in selection.entity_keys]
            if any(value is None for value in values):
                raise GenerationNotFoundError("A selected entity is not in the active catalog.")
            selected = [value for value in values if value is not None]
        else:
            selected = []
            for kind in selection.kinds or (None,):
                offset = 0
                while len(selected) <= limit:
                    page, total = self._catalog_store.entity_page(
                        run.id, EntityFilters(kind=kind, locator_prefix=selection.locator_prefix),
                        500, offset,
                    )  # fmt: skip
                    selected.extend(page)
                    offset += len(page)
                    if not page or offset >= total:
                        break
        if selection.stale_only:
            _, views = self.entry_views(
                project_id, snapshot_id, [item.entity_key for item in selected]
            )
            selected = [item for item in selected if views[item.entity_key].state != "current"]
        if len(selected) > limit:
            raise ValueError("The selection exceeds the plan entity limit; narrow it.")
        return selected

    def get_plan(self, project_id: UUID, snapshot_id: UUID, plan_id: UUID) -> GenerationPlan:
        self._catalog.require_snapshot(project_id, snapshot_id)
        plan = self._store.plan(project_id, snapshot_id, plan_id)
        if plan is None:
            raise PlanNotFoundError("Plan was not found.")
        return plan

    # ----------------------------------------------------------------- execution (T0–T4)

    def execute(
        self, project_id: UUID, snapshot_id: UUID, plan_id: UUID, plan_digest: str, confirm: bool
    ) -> GenerationRun:
        if confirm is not True:
            raise ValueError("Generation requires explicit confirmation of the plan digest.")
        return asyncio.run(self._execute(project_id, snapshot_id, plan_id, plan_digest))

    async def _execute(
        self, project_id: UUID, snapshot_id: UUID, plan_id: UUID, plan_digest: str
    ) -> GenerationRun:
        self._catalog.require_snapshot(project_id, snapshot_id)
        plan = self._store.plan(project_id, snapshot_id, plan_id)
        if plan is None:
            raise PlanNotFoundError("Plan was not found.")
        if plan.mode == "generate" and any(i.disposition == "generate" for i in plan.items):
            if not self._settings.enabled:
                raise GenerationDisabledError("Semantic generation is not enabled.")
        try:
            catalog_run, _ = self._catalog.select(project_id, snapshot_id, None)
        except Exception as error:
            raise PlanChangedError("The catalog authority changed since the plan.") from error
        if catalog_run.id != plan.catalog_run_id:
            raise PlanChangedError("The catalog authority changed since the plan.")
        generator = self._factory()
        try:
            run, db_now = self._store.admit(
                project_id,
                snapshot_id,
                plan_id,
                plan_digest,
                generator.config.digest,
                self._settings.lease_seconds,
            )
            deadline = time.monotonic() + (run.deadline_at - db_now).total_seconds()
            results = RunResults()
            for item in plan.items:
                if item.disposition == "reuse" and item.reuse_interpretation_id is not None:
                    results.entries[item.entity_key] = IndexEntry(
                        item.entity_key, item.reuse_interpretation_id, "reused",
                        item.reuse_profile_digest,
                    )  # fmt: skip
                    results.reused += 1
            stop = False
            gate = OutboundGate(run.id, run.lease_token)
            for item in plan.items:
                if item.disposition != "generate":
                    continue
                if stop:
                    results.not_attempted += 1
                    continue
                stop = await self._generate(
                    run, plan, catalog_run, generator, gate, item, deadline, results
                )
            if results.stop_reason == "RUN_FENCED":
                self._store.self_abandon(run)
                refreshed = self._store.run(project_id, snapshot_id, run.id)
                return refreshed or run
            requested = sum(item.disposition in {"generate", "reuse"} for item in plan.items)
            done = len(results.entries)
            state: RunState = "complete" if done == requested else "partial" if done else "failed"
            if requested == 0:
                state = "complete"
            try:
                return self._store.finish(run, state, results)
            except RunFencedError:
                self._store.self_abandon(run)
                refreshed = self._store.run(project_id, snapshot_id, run.id)
                return refreshed or run
        finally:
            await generator.close()

    async def _generate(
        self,
        run: GenerationRun,
        plan: GenerationPlan,
        catalog_run: SemanticCatalogRun,
        generator: StructuredGenerator,
        gate: OutboundGate,
        item: PlanItem,
        deadline: float,
        results: RunResults,
    ) -> bool:
        """Attempt one entity. Returns True when the whole run must stop sending."""
        s = self._settings
        while True:
            if (
                deadline - time.monotonic()
                < s.request_timeout_seconds + s.publication_margin_seconds
            ):
                results.stop_reason = "DEADLINE"
                results.not_attempted += 1
                return True
            if not self._store.authority_unchanged(run):
                results.stop_reason = "AUTHORITY_CHANGED"
                results.not_attempted += 1
                return True
            try:
                attempt = self._store.begin_attempt(run, item, s.lease_seconds)  # T1
            except BudgetExhaustedError:
                results.stop_reason = "BUDGET_EXHAUSTED"
                results.not_attempted += 1
                return True
            except RunFencedError:
                results.stop_reason = "RUN_FENCED"
                return True
            ticket = gate.mint(attempt.id, item.request_digest or "")
            # Pre-send recheck: authority, rehydrated package and exact planned bytes.
            entity = self._catalog_store.entity(catalog_run.id, item.entity_key)
            canonical = None
            package = None
            codes: tuple[str, ...] = ()
            if not self._store.authority_unchanged(run):
                codes = ("AUTHORITY_CHANGED",)
            elif entity is None:
                codes = ("ENTITY_REMOVED",)
            else:
                try:
                    package = self.package(catalog_run, entity)
                    canonical = self._render(generator, package)
                    if canonical.digest != item.request_digest:
                        codes = ("REQUEST_DIGEST_CHANGED",)
                except PackageIntegrityError:
                    codes = ("PACKAGE_INTEGRITY",)
            if codes:
                return self._record(
                    run, attempt, OutcomeRecord("not_sent", rejection_codes=codes), None, results,
                    stop=True, reason=codes[0],
                )  # fmt: skip
            assert canonical is not None and package is not None
            existing = self._store.existing_candidate(
                run.project_id, package.input_key, generator.config.profile_digest
            )
            if existing is not None:
                stop = self._record(
                    run, attempt, OutcomeRecord("reused_concurrently"), None, results
                )
                if results.stop_reason != "RUN_FENCED":
                    results.entries[item.entity_key] = IndexEntry(
                        item.entity_key, existing, "reused", generator.config.profile_digest
                    )
                    results.reused += 1
                return stop
            started = time.monotonic()
            try:
                remaining = max(0.001, min(s.request_timeout_seconds, deadline - time.monotonic()))
                result = await gate.send(generator, canonical, ticket, remaining)
            except ProviderCallError as error:
                latency = int((time.monotonic() - started) * 1000)
                code = (
                    "provider_unavailable" if error.code == "configuration_invalid" else error.code
                )
                stop = self._record(
                    run, attempt, OutcomeRecord(code, latency_ms=latency,
                                                rejection_codes=(error.code.upper(),)),
                    None, results,
                )  # fmt: skip
                if stop:
                    return True
                if error.code == "configuration_invalid":
                    results.failed += 1
                    results.stop_reason = "CONFIGURATION_INVALID"
                    return True
                wait = error.retry_after
                if (
                    error.code in {"rate_limited", "provider_unavailable"}
                    and wait is not None
                    and wait <= s.max_retry_wait_seconds
                    and deadline - time.monotonic() - wait
                    >= s.request_timeout_seconds + s.publication_margin_seconds
                    and attempt.attempt_no < int(plan.caps["max_attempts_per_entity"])
                ):
                    await asyncio.sleep(wait)
                    continue  # explicit retry: a new T1, a new ticket, the same digest
                results.failed += 1
                return False
            latency = int((time.monotonic() - started) * 1000)
            base = OutcomeRecord(
                "rejected",
                result.returned_model,
                result.input_tokens,
                result.output_tokens,
                latency,
                result.response_digest,
            )
            if (
                result.input_tokens is not None
                and result.input_tokens > canonical.size + TEMPLATE_TOKEN_ALLOWANCE
            ):
                self._record(
                    run, attempt, replace(base, outcome="token_bound_violated",
                                          rejection_codes=("TOKEN_BOUND_VIOLATED",)),
                    None, results, stop=True, reason="TOKEN_BOUND_VIOLATED",
                )  # fmt: skip
                results.failed += 1
                return True
            if result.state != "completed":
                outcome = "refused" if result.state == "refused" else "rejected"
                stop = self._record(
                    run, attempt, replace(base, outcome=outcome,
                                          rejection_codes=(f"OUTPUT_{result.state.upper()}",)),
                    None, results,
                )  # fmt: skip
                results.failed += 1
                return stop
            validation = validate(result.raw_text, package)
            if validation.status == "rejected":
                retained = (
                    result.raw_text[:65_536]
                    if s.retain_rejected_output and _screened(result.raw_text)
                    else None
                )
                stop = self._record(
                    run, attempt, replace(base, rejection_codes=validation.rejection_codes,
                                          retained_output=retained),
                    None, results,
                )  # fmt: skip
                results.failed += 1
                return stop
            interpretation = self._interpretation(
                run, attempt, package, catalog_run, generator.config, result.returned_model,
                validation,
            )  # fmt: skip
            stop = self._record(
                run, attempt, replace(base, outcome=validation.status,
                                      rejection_codes=validation.rejection_codes),
                interpretation, results,
            )  # fmt: skip
            if results.stop_reason != "RUN_FENCED":
                results.entries[item.entity_key] = IndexEntry(
                    item.entity_key, interpretation.id, "generated", None
                )
                results.generated += 1
            return stop

    def _record(
        self,
        run: GenerationRun,
        attempt: Attempt,
        outcome: OutcomeRecord,
        interpretation: Interpretation | None,
        results: RunResults,
        stop: bool = False,
        reason: str | None = None,
    ) -> bool:
        """T2: outcome (and candidate) under the fencing predicate."""
        try:
            self._store.record_outcome(
                run, attempt, outcome, interpretation, self._settings.lease_seconds
            )
        except RunFencedError:
            results.stop_reason = "RUN_FENCED"
            return True
        if stop:
            results.stop_reason = reason
        return stop

    @staticmethod
    def _interpretation(
        run: GenerationRun,
        attempt: Attempt,
        package: Package,
        catalog_run: SemanticCatalogRun,
        config: ProviderConfig,
        returned_model: str | None,
        validation: ValidationResult,
    ) -> Interpretation:
        return Interpretation(
            uuid4(),
            run.project_id,
            run.snapshot_id,
            run.id,
            attempt.id,
            package.entity.entity_key,
            package.input_key,
            _manifest(package, catalog_run.id),
            config.profile_digest,
            config.provider,
            config.model,
            returned_model,
            PROMPT_VERSION,
            SCHEMA_VERSION,
            VALIDATOR_VERSION,
            validation.status,
            validation.unknowns,
            datetime.now().astimezone(),
            validation.claims,
        )

    # ----------------------------------------------------------------- recovery and reads

    def recover(self, project_id: UUID, snapshot_id: UUID) -> list[UUID]:
        self._catalog.require_snapshot(project_id, snapshot_id)
        return self._store.abandon_expired(project_id, snapshot_id)

    def runs(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[GenerationRun], int]:
        self._catalog.require_snapshot(project_id, snapshot_id)
        return self._store.runs(project_id, snapshot_id, limit, offset)

    def run(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID
    ) -> tuple[GenerationRun, list[Attempt]]:
        self._catalog.require_snapshot(project_id, snapshot_id)
        run = self._store.run(project_id, snapshot_id, run_id)
        if run is None:
            raise GenerationNotFoundError("Generation run was not found.")
        return run, self._store.attempts(run.id)


def _screened(text: str) -> bool:
    from buildlens.domain.source import ContentRejectedError, screen_content

    try:
        screen_content(text)
    except ContentRejectedError:
        return False
    return True
