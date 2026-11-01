"""Stage 15 Milestone E: plan-gated explanations of pinned flow traces (D3, INV-8).

Lifecycle, as in Stage 13: **plan** (no provider traffic; the exact outbound bytes, their
digest and size, the provider, model and retention note are recorded) → **confirm** (the caller
repeats the plan digest with ``confirm=True``) → **run** (the plan is used exactly once; one
single-use gate ticket is minted for the planned bytes and redeemed by the generator). The
reply is validated by ``flow-validator.v1`` against the exact package that was sent.

The Stage 13 ``OutboundGate`` and generators are reused unchanged; flow requests carry their
own outbound version (``buildlens.flow.outbound.v1``) through a ``CanonicalOutbound``
subclass, so a flow ticket can never authorise semantic-generation bytes or the reverse.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from buildlens.application.flow_package import (
    INSTRUCTIONS,
    SCHEMA,
    Excerpt,
    excerpt_text,
    package_data,
    package_digest,
    package_of,
)
from buildlens.application.flow_trace_service import FlowTraceService
from buildlens.application.flow_validation import validate
from buildlens.application.outbound_gate import (
    CanonicalOutbound,
    OutboundGate,
    ProviderCallError,
    StructuredGenerator,
    canonical_json,
)
from buildlens.application.semantic_generation import RETENTION_NOTES, GenerationSettings
from buildlens.domain.flow_explanation import (
    FLOW_OUTBOUND_VERSION,
    FLOW_VALIDATOR_VERSION,
    PACKAGE_EXCERPTS,
    PLAN_TTL_SECONDS,
    ExplanationPlan,
    ExplanationRun,
    explanation_contract,
)
from buildlens.domain.source import digest
from buildlens.infrastructure.persistence.flow_explanation import (
    ExplanationPlanError,
    SqlAlchemyFlowExplanationStore,
)


class FlowExplanationDisabledError(Exception):
    pass


class FlowExplanationNotFoundError(Exception):
    pass


class FlowExplanationPlanInvalidError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class FlowCanonicalOutbound(CanonicalOutbound):
    """The planned flow request; its digest is bound to the flow outbound version."""

    @property
    def data(self) -> bytes:
        return canonical_json(
            {
                "version": FLOW_OUTBOUND_VERSION,
                "provider": self.provider,
                "model": self.model,
                "body": self.body,
                "settings": self.settings,
            }
        )

    @property
    def digest(self) -> str:
        return hashlib.sha256(FLOW_OUTBOUND_VERSION.encode() + b"\0" + self.data).hexdigest()


class FlowExplanationService:
    def __init__(
        self,
        traces: FlowTraceService,
        catalog: Any,
        store: SqlAlchemyFlowExplanationStore,
        factory: Callable[[], StructuredGenerator],
        settings: GenerationSettings,
    ) -> None:
        self._traces, self._catalog, self._store = traces, catalog, store
        self._factory, self._settings = factory, settings

    # ----------------------------------------------------------------- configuration

    def configuration(self) -> dict[str, Any]:
        generator = self._factory()
        try:
            config = generator.config
            return {
                "enabled": self._settings.enabled,
                "provider": config.provider,
                "model": config.model,
                "sharing_class": config.sharing_class,
                "retention_note": RETENTION_NOTES[config.provider],
                "status": generator.status(),
                "contract": explanation_contract(),
            }
        finally:
            _close(generator)

    # ----------------------------------------------------------------- plan (no traffic)

    def plan(self, project_id: UUID, snapshot_id: UUID, trace_digest: str) -> dict[str, Any]:
        pinned = self._traces.pinned(project_id, snapshot_id, trace_digest)
        if pinned["status"] != "current":
            raise FlowExplanationPlanInvalidError("TRACE_NOT_CURRENT")
        trace = pinned["trace"]
        package = package_of(trace, self._excerpts(project_id, snapshot_id, trace))
        package_hash = package_digest(package)
        generator = self._factory()
        try:
            config = generator.config
            canonical = self._canonical(generator, package)
        finally:
            _close(generator)
        if canonical.size > self._settings.request_byte_ceiling:
            raise FlowExplanationPlanInvalidError("REQUEST_TOO_LARGE")
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=min(self._settings.plan_ttl_seconds, PLAN_TTL_SECONDS))
        contract = explanation_contract()
        plan_digest = str(
            digest(
                {
                    "trace_digest": trace_digest,
                    "package_digest": package_hash,
                    "request_digest": canonical.digest,
                    "config_digest": config.digest,
                    "expires_at": expires.isoformat(),
                    "contract": contract,
                }
            )
        )
        plan = ExplanationPlan(
            uuid4(),
            project_id,
            snapshot_id,
            UUID(str(pinned["run_id"])),
            trace_digest,
            package_hash,
            canonical.digest,
            canonical.size,
            config.digest,
            config.provider,
            config.model,
            RETENTION_NOTES[config.provider],
            plan_digest,
            now,
            expires,
            "planned",
        )
        self._store.insert_plan(plan, package, contract)
        return {
            **_plan_json(plan),
            "package": {
                "steps": len(package["steps"]),
                "excerpts": len(package["excerpts"]),
                "truncated": package["truncated"],
                "caveats": package["caveats"],
            },
            "enabled": self._settings.enabled,
            "sent": 0,
        }

    # ----------------------------------------------------------------- confirm and run

    def execute(
        self, project_id: UUID, snapshot_id: UUID, plan_id: UUID, plan_digest: str, confirm: bool
    ) -> ExplanationRun:
        if confirm is not True:
            raise ValueError("An explanation requires explicit confirmation of the plan digest.")
        if not self._settings.enabled:
            raise FlowExplanationDisabledError("Generation is not enabled.")
        found = self._store.plan(project_id, snapshot_id, plan_id)
        if found is None:
            raise FlowExplanationNotFoundError("Explanation plan not found.")
        plan, package = found
        if package_digest(package) != plan.package_digest:
            raise FlowExplanationPlanInvalidError("PACKAGE_CHANGED")
        pinned = self._traces.pinned(project_id, snapshot_id, plan.trace_digest)
        if pinned["status"] != "current":
            raise FlowExplanationPlanInvalidError("TRACE_NOT_CURRENT")
        generator = self._factory()
        try:
            if generator.config.digest != plan.config_digest:
                raise FlowExplanationPlanInvalidError("PLAN_CONFIG_CHANGED")
            canonical = self._canonical(generator, package)
            if canonical.digest != plan.request_digest:
                raise FlowExplanationPlanInvalidError("PLAN_CHANGED")
            try:
                run = self._store.admit(plan_id, plan_digest, self._settings.lease_seconds)
            except ExplanationPlanError as error:
                raise FlowExplanationPlanInvalidError(error.code) from error
            gate = OutboundGate(run.id, self._store.lease_token(run.id))
            attempt = self._store.begin_attempt(run.id, canonical.digest)
            ticket = gate.mint(attempt, canonical.digest)
            started = time.monotonic()
            try:
                result = asyncio.run(
                    gate.send(generator, canonical, ticket, self._settings.request_timeout_seconds)
                )
            except ProviderCallError as error:
                self._store.record(
                    run.id,
                    attempt,
                    _outcome("provider_error", diagnostics=[error.code, *error.diagnostics]),
                    (),
                    "failed",
                )
            else:
                latency = int((time.monotonic() - started) * 1000)
                common = {
                    "returned_model": result.returned_model,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "latency_ms": latency,
                    "response_digest": result.response_digest,
                }
                if result.state != "completed":
                    outcome = "refused" if result.state == "refused" else "incomplete"
                    self._store.record(run.id, attempt, _outcome(outcome, **common), (), "failed")
                else:
                    checked = validate(result.raw_text, package)
                    self._store.record(
                        run.id,
                        attempt,
                        _outcome(
                            checked.status,
                            rejection_codes=list(checked.rejection_codes),
                            unknowns=list(checked.unknowns),
                            **common,
                        ),
                        checked.claims,
                        "complete" if checked.claims else "failed",
                    )
        finally:
            _close(generator)
        value = self._store.run(project_id, snapshot_id, run.id)
        assert value is not None
        return value

    # ----------------------------------------------------------------- reads

    def run(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> ExplanationRun:
        value = self._store.run(project_id, snapshot_id, run_id)
        if value is None:
            raise FlowExplanationNotFoundError("Explanation run not found.")
        return value

    def runs(self, project_id: UUID, snapshot_id: UUID, trace_digest: str) -> list[ExplanationRun]:
        return self._store.runs(project_id, snapshot_id, trace_digest)

    # ----------------------------------------------------------------- helpers

    def _canonical(
        self, generator: StructuredGenerator, package: dict[str, Any]
    ) -> FlowCanonicalOutbound:
        rendered = generator.render(
            INSTRUCTIONS, package_data(package), SCHEMA, self._settings.max_output_tokens
        )
        return FlowCanonicalOutbound(
            rendered.provider, rendered.model, rendered.body, rendered.settings
        )

    def _excerpts(
        self, project_id: UUID, snapshot_id: UUID, trace: dict[str, Any]
    ) -> list[Excerpt]:
        """Hash-verified stored-chunk excerpts for the scopes the trace visits (no disk reads);
        a scope whose catalog entity or chunks do not verify contributes nothing."""
        lines: dict[str, list[int]] = {}
        for step in trace["steps"]:
            if isinstance(step.get("line"), int):
                lines.setdefault(step["from"], []).append(step["line"])
        found: list[Excerpt] = []
        seen: set[str] = set()
        for hop in trace["hops"]:
            identity = hop.get("identity")
            if not identity or identity in seen or len(found) >= PACKAGE_EXCERPTS:
                continue
            seen.add(identity)
            try:
                detail = self._catalog.entity(project_id, snapshot_id, f"symbol:{identity}")
            except Exception:  # noqa: BLE001 - an unavailable excerpt is simply omitted
                continue
            if detail.integrity_errors:
                continue
            wanted = sorted(lines.get(hop["id"], []))
            chunks = [c for c in detail.entity.evidence if c.verified and c.content is not None]
            chunk = next(
                (c for c in chunks if any(c.start_line <= w <= c.end_line for w in wanted)),
                chunks[0] if chunks else None,
            )
            if chunk is None:
                continue
            start, text = excerpt_text(chunk.content, chunk.start_line, wanted)
            found.append(
                Excerpt(
                    f"E{len(found) + 1}",
                    hop["id"],
                    detail.entity.locator,
                    start,
                    text,
                    chunk.stable_key,
                    chunk.content_hash,
                )
            )
        return found


def _outcome(kind: str, **fields: Any) -> dict[str, Any]:
    return {
        "outcome": kind,
        "validator": FLOW_VALIDATOR_VERSION,
        "returned_model": fields.get("returned_model"),
        "input_tokens": fields.get("input_tokens"),
        "output_tokens": fields.get("output_tokens"),
        "latency_ms": fields.get("latency_ms"),
        "response_digest": fields.get("response_digest"),
        "rejection_codes": fields.get("rejection_codes", []),
        "unknowns": fields.get("unknowns", []),
        "diagnostics": fields.get("diagnostics", []),
    }


def _plan_json(plan: ExplanationPlan) -> dict[str, Any]:
    return {
        "plan_id": str(plan.id),
        "trace_digest": plan.trace_digest,
        "package_digest": plan.package_digest,
        "request_digest": plan.request_digest,
        "request_bytes": plan.request_bytes,
        "provider": plan.provider,
        "model": plan.model,
        "retention_note": plan.retention_note,
        "plan_digest": plan.plan_digest,
        "expires_at": plan.expires_at.isoformat(),
    }


def _close(generator: StructuredGenerator) -> None:
    try:
        asyncio.run(generator.close())
    except RuntimeError:  # an event loop is already running: the client was never opened
        pass
