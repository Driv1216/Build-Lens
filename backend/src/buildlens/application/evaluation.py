"""Sequential native evaluation over ContextService and the same Ask invocation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from buildlens.application.ask import AskService
from buildlens.application.context import ContextService
from buildlens.application.evaluation_binding import (
    bind_case,
    check_corpus,
    chunk_metadata,
    definition_digest,
    target_excerpt,
    validate_definitions,
)
from buildlens.application.evaluation_metrics import coverage, score_result
from buildlens.application.observation import observing
from buildlens.domain.ask import AskRequest, GroundedAskResult, GroundedPrompt
from buildlens.domain.context import ContextResult, RankingObservation
from buildlens.domain.evaluation import (
    ARTIFACT_VERSION,
    METRIC_VERSION,
    RUBRIC_VERSION,
    VARIANTS,
    CaseBinding,
    CorpusEvidence,
    EvaluationDataset,
    EvaluationError,
    digest,
    primitive,
)
from buildlens.domain.retrieval import (
    CANDIDATE_DEPTH,
    RETRIEVAL_POLICY_VERSION,
    RRF_CONSTANT,
    RetrievalResult,
)


class CorpusStore(Protocol):
    def load(self, project_id: UUID, snapshot_id: UUID) -> CorpusEvidence: ...
    def verify(self, corpus: CorpusEvidence) -> None: ...


class RunStore(Protocol):
    def create(
        self, manifest: dict[str, Any], dataset: EvaluationDataset, corpus: dict[str, Any]
    ) -> None: ...
    def write(self, name: str, value: Any, *, replace: bool = False) -> None: ...
    def finish(self, manifest: dict[str, Any]) -> None: ...


class Collector:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.failures: set[str] = set()

    def record(self, stage: str, value: object) -> None:
        if stage.endswith("_unavailable"):
            self.unavailable(stage.removesuffix("_unavailable"))
        elif stage == "count_measurement":
            measurements = cast(list[object], self.values.setdefault("count_measurements", []))
            measurements.append(value)
        else:
            self.values[stage] = value

    def unavailable(self, stage: str) -> None:
        self.values.pop(stage, None)
        self.failures.add(stage)


def error_code(error: Exception) -> str:
    """Allowlisted diagnostics only; never str(error), URLs, or provider payloads."""
    if isinstance(error, EvaluationError):
        return error.code
    name = type(error).__name__
    codes = {
        "ContextConflictError": "CONTEXT_AUTHORITY_OR_INTEGRITY_CHANGED",
        "EmbeddingConflictError": "EMBEDDING_AUTHORITY_CHANGED",
        "ContextUnavailableError": "CONTEXT_UNAVAILABLE",
        "ContextBusyError": "CONTEXT_BUSY",
        "ContextLimitError": "CONTEXT_LIMIT",
        "ContextInputError": "CONTEXT_INPUT_INVALID",
        "EmbeddingUnavailableError": "EMBEDDING_UNAVAILABLE",
        "EmbeddingBusyError": "EMBEDDING_BUSY",
        "EmbeddingTimeoutError": "EMBEDDING_TIMEOUT",
        "GenerationNotConfiguredError": "GENERATION_NOT_CONFIGURED",
        "GenerationConfigurationInvalidError": "GENERATION_CONFIGURATION_INVALID",
        "GenerationUnavailableError": "GENERATION_UNAVAILABLE",
        "GenerationRateLimitedError": "GENERATION_RATE_LIMITED",
        "AskTimeoutError": "ASK_TIMEOUT",
        "TokenCountUnavailableError": "TOKEN_COUNT_UNAVAILABLE",
        "TokenCountTimeoutError": "TOKEN_COUNT_TIMEOUT",
        "AskBudgetExceededError": "ASK_BUDGET_EXCEEDED",
        "GenerationContextRejectedError": "GENERATION_CONTEXT_REJECTED",
        "GenerationOutputInvalidError": "GENERATION_OUTPUT_INVALID",
        "GenerationCitationInvalidError": "GENERATION_CITATION_INVALID",
        "GenerationRefusedError": "GENERATION_REFUSED",
        "AskInputError": "ASK_INPUT_INVALID",
    }
    return codes.get(name, "OPERATION_FAILED")


def project_observations(
    collector: Collector, corpus: CorpusEvidence, answer: GroundedAskResult | None
) -> dict[str, Any]:
    chunks = {chunk.id: chunk for chunk in corpus.chunks}
    excerpts: dict[str, str] = {}

    def row(chunk: Any) -> dict[str, Any]:
        if chunks.get(chunk.id) != chunk:
            raise EvaluationError("OBSERVED_EVIDENCE_OUTSIDE_FROZEN_CORPUS")
        excerpts[str(chunk.id)] = chunk.content
        return chunk_metadata(chunk, corpus)

    result: dict[str, Any] = {"unavailable": sorted(collector.failures)}
    retrieved = collector.values.get("retrieval")
    if isinstance(retrieved, RetrievalResult):
        if (
            retrieved.processing_run.id != corpus.processing.id
            or retrieved.embedding_run is None
            or retrieved.embedding_run.id != corpus.embedding.id
        ):
            raise EvaluationError("RUN_AUTHORITY_CHANGED")
        result["retrieval"] = [row(hit.chunk) for hit in retrieved.hits]
        result["retrieval_summary"] = {
            "requested_mode": retrieved.requested_mode,
            "executed_mode": retrieved.executed_mode,
            "exact": primitive(retrieved.exact),
            "lexical": primitive(retrieved.lexical),
            "semantic": primitive(retrieved.semantic),
        }
    ranking = collector.values.get("ranking")
    if isinstance(ranking, RankingObservation):
        result["ranking_observation"] = primitive(ranking)
        result["ranking"] = [
            row(chunks[candidate.chunk_id])
            for candidate in sorted(
                (
                    candidate
                    for candidate in ranking.candidates
                    if candidate.selection_order is not None
                ),
                key=lambda candidate: candidate.selection_order or 0,
            )
        ]
    context = collector.values.get("ask_context", collector.values.get("context"))
    if isinstance(context, ContextResult):
        if (
            context.processing_run.id != corpus.processing.id
            or context.embedding_run is None
            or context.embedding_run.id != corpus.embedding.id
        ):
            raise EvaluationError("RUN_AUTHORITY_CHANGED")
        result["context"] = [
            {
                **row(item.chunk),
                "selected_as_seed": item.selected_as_seed,
                "expanded_from": primitive(item.expanded_from),
            }
            for item in context.items
        ]
        result["context_decisions"] = primitive(context.decisions)
        result["context_budget"] = primitive(context.budget)
        result["context_policy"] = primitive(context.policy)
        result["processing_authority"] = {
            "id": str(context.processing_run.id),
            "revision": context.processing_run.revision,
            "contract": context.processing_run.processing_contract,
        }
        result["embedding_authority"] = (
            str(context.embedding_run.id) if context.embedding_run else None
        )
        result["reranker_contract"] = primitive(context.reranker_contract)
        result["reranker_state"] = context.reranker_state
    prompt = collector.values.get("generation_evidence")
    if isinstance(prompt, GroundedPrompt):
        result["generation_evidence"] = [
            {**row(item.chunk), "evidence_id": key} for key, item in prompt.evidence
        ]
        result["prompt_version"] = prompt.version
    for key in (
        "generation_settings",
        "provider_reported_settings",
        "generation_response_metadata",
        "count_measurements",
        "prompt_measurement",
        "generation_decisions",
    ):
        if key in collector.values:
            result[key] = primitive(collector.values[key])
    settings = result.get("generation_settings", {})
    for name, reported in result.get("provider_reported_settings", {}).items():
        if name in settings:
            settings[name]["effective"] = reported["value"]
            settings[name]["effective_provenance"] = reported["provenance"]
    validated = collector.values.get("validated_answer")
    if isinstance(validated, dict):
        result["validated_answer_before_final_verification"] = {
            key: primitive(validated[key]) for key in ("status", "answer_text", "statements")
        }
        result["cited_evidence"] = [
            {**row(c.item.chunk), "evidence_id": c.evidence_id} for c in validated["citations"]
        ]
    if answer is not None:
        result["generation_evidence"] = [
            {**row(c.item.chunk), "evidence_id": c.evidence_id} for c in answer.evidence
        ]
        result["cited_evidence"] = [
            {**row(c.item.chunk), "evidence_id": c.evidence_id} for c in answer.citations
        ]
        result["answer"] = {
            key: primitive(getattr(answer, key))
            for key in (
                "request_id",
                "status",
                "reason",
                "answer_text",
                "statements",
                "policy",
                "model",
                "returned_model",
                "generation_state",
                "original_context_digest",
                "evidence_digest",
                "fixed_tokens",
                "question_tokens",
                "prompt",
                "usage_input_tokens",
                "usage_output_tokens",
                "context_seconds",
                "counting_seconds",
                "generation_seconds",
                "total_seconds",
            )
        }
        result["generation_decisions"] = primitive(answer.generation_decisions)
    result["excerpts"] = excerpts
    return result


def preflight(
    dataset: EvaluationDataset, corpus: CorpusEvidence
) -> tuple[dict[str, CaseBinding], dict[str, str]]:
    validate_definitions(dataset)
    check_corpus(dataset, corpus)
    bindings, invalid = {}, {}
    for case in dataset.cases:
        try:
            bindings[case.id] = bind_case(case, corpus)
        except EvaluationError as error:
            invalid[case.id] = error.code
    return bindings, invalid


class EvaluationRunner:
    def __init__(
        self,
        corpus: CorpusStore,
        context: ContextService,
        ask: AskService,
        store: RunStore,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.corpus, self.context, self.ask, self.store = corpus, context, ask, store
        self.progress = progress

    async def run(
        self,
        dataset: EvaluationDataset,
        project_id: UUID,
        snapshot_id: UUID,
        *,
        answers: bool,
        implementation: dict[str, Any],
        selected_cases: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        corpus = self.corpus.load(project_id, snapshot_id)
        bindings, invalid = preflight(dataset, corpus)
        selection = selected_cases or tuple(case.id for case in dataset.cases)
        if len(selection) != len(set(selection)) or not set(selection) <= {
            c.id for c in dataset.cases
        }:
            raise EvaluationError("CASE_SELECTION_INVALID")
        if answers and not self.ask.status()["configured"]:
            raise EvaluationError("GENERATION_NOT_CONFIGURED")
        manifest: dict[str, Any] = {
            "id": str(uuid4()),
            "artifact_version": ARTIFACT_VERSION,
            "metric_version": METRIC_VERSION,
            "dataset_id": dataset.id,
            "dataset_version": dataset.version,
            "dataset_digest": digest(dataset),
            "definition_digest": definition_digest(dataset),
            "judgments_digest": dataset.judgments_digest,
            "rubric_version": RUBRIC_VERSION,
            "corpus_digest": digest(corpus.manifest),
            "project_id": str(project_id),
            "snapshot_id": str(snapshot_id),
            "answers": answers,
            "review_status": dataset.review_status,
            "provisional": dataset.review_status != "approved",
            "case_selection": selection,
            "variants": VARIANTS,
            "filters": {c.id: primitive(c.filters) for c in dataset.cases if c.id in selection},
            "retrieval_policy": {
                "version": RETRIEVAL_POLICY_VERSION,
                "rrf_constant": RRF_CONSTANT,
                "candidate_depth": CANDIDATE_DEPTH,
            },
            "implementation": implementation,
            "started_at": datetime.now(UTC).isoformat(),
            "status": "running",
            "comparison_valid": True,
            "execution_order": [],
            "results_written": 0,
            "result_digests": {},
            "verification_states": {
                "infrastructure": "independent checks; not attested by this run",
                "retrieval_baseline": "running",
                "human_ground_truth": "approved"
                if dataset.review_status == "approved"
                else "pending human approval",
                "real_provider_answers": "running" if answers else "not executed",
                "semantic_review": "unreviewed",
            },
        }
        self.store.create(manifest, dataset, corpus.manifest)
        counts = {"success": 0, "error": 0, "invalid": 0}
        try:
            for case in dataset.cases:
                if case.id not in selection:
                    continue
                for variant, (mode, policy) in VARIANTS.items():
                    collector = Collector()
                    outcome, code, answer = "success", None, None
                    started = time.monotonic()
                    if self.progress:
                        self.progress(f"{case.id}: {variant}")
                    if case.id in invalid:
                        outcome, code = "invalid", invalid[case.id]
                    else:
                        try:
                            self.corpus.verify(corpus)
                            with observing(collector):
                                if answers:
                                    answer = await self.ask.ask(
                                        AskRequest(
                                            project_id,
                                            snapshot_id,
                                            case.question,
                                            mode,
                                            policy,
                                            case.filters,
                                        )
                                    )
                                else:
                                    await asyncio.to_thread(
                                        self.context.build,
                                        project_id,
                                        snapshot_id,
                                        case.question,
                                        mode,
                                        policy,
                                        case.filters,
                                    )
                        except Exception as error:
                            outcome, code = "error", error_code(error)
                        try:
                            self.corpus.verify(corpus)
                        except Exception:
                            outcome, code = "error", "RUN_AUTHORITY_CHANGED"
                            manifest["comparison_valid"] = False
                    try:
                        observations = project_observations(collector, corpus, answer)
                    except EvaluationError as error:
                        outcome, code = "invalid", error.code
                        if error.code == "RUN_AUTHORITY_CHANGED":
                            manifest["comparison_valid"] = False
                        observations = {"unavailable": ["projection"]}
                    except Exception:
                        # Successful production remains successful; withhold dependent measurements.
                        observations = {"unavailable": ["projection"]}
                    observed_retrieval = observations.get("retrieval_summary")
                    if observed_retrieval and (
                        observed_retrieval["requested_mode"] != mode
                        or observed_retrieval["executed_mode"] != mode
                    ):
                        outcome, code = "invalid", "VARIANT_SUBSTITUTED"
                    binding = bindings.get(case.id)
                    metrics: dict[str, Any]
                    try:
                        metrics = (
                            score_result(case, binding, observations, outcome)
                            if binding
                            else {"eligible": False}
                        )
                    except EvaluationError as error:
                        outcome, code = "invalid", error.code
                        metrics = {"eligible": False}
                    metrics["provisional"] = dataset.review_status != "approved" or bool(
                        (metrics.get("ranking") or {}).get("provisional")
                    )
                    result = {
                        "artifact_version": ARTIFACT_VERSION,
                        "case_id": case.id,
                        "variant": variant,
                        "category": case.category,
                        "outcome": outcome,
                        "error_code": code,
                        "duration_seconds": time.monotonic() - started,
                        "question": case.question,
                        "expected": primitive(case),
                        "binding": primitive(binding),
                        "expected_excerpts": {
                            t.id: target_excerpt(
                                t, [chunk for chunk in corpus.chunks if chunk.locator == t.locator]
                            )
                            for t in case.targets
                        }
                        if binding
                        else {},
                        "diagnostic_evidence_coverage": {
                            stage: coverage(case, binding, observations[stage])
                            for stage in (
                                "retrieval",
                                "ranking",
                                "context",
                                "generation_evidence",
                                "cited_evidence",
                            )
                            if binding and stage in observations
                        },
                        "observations": observations,
                        "metrics": metrics,
                    }
                    result["result_digest"] = digest(
                        {k: v for k, v in result.items() if k != "metrics"}
                    )
                    self.store.write(f"cases/{case.id}.{variant}.json", result)
                    self.store.write(
                        f"review-templates/{case.id}.{variant}.json",
                        {
                            "result_digest": result["result_digest"],
                            "rubric_version": RUBRIC_VERSION,
                            "reviewer": "",
                            "reviewed_at": "",
                            "rationale": "",
                            "facets": [],
                            "statements": [],
                            "links": [],
                            "correctness": None,
                            "abstention": None,
                        },
                    )
                    candidates = observations.get("retrieval") or []
                    self.store.write(
                        f"relevance-templates/{case.id}.{variant}.json",
                        {
                            "case_id": case.id,
                            "pool_variant": variant,
                            "judgments": [
                                {
                                    "selector": {
                                        "locator": row["locator"],
                                        "artifact_fingerprint": row["artifact_fingerprint"],
                                        "stable_key": row["stable_key"],
                                        "content_hash": row["content_hash"],
                                        "start_offset": row["span"]["start_offset"],
                                        "end_offset": row["span"]["end_offset"],
                                    },
                                    "relevant": None,
                                    "reviewer": "",
                                    "rationale": "",
                                    "reviewed_at": "",
                                }
                                for row in candidates
                            ],
                        },
                    )
                    counts[outcome] += 1
                    manifest["result_digests"][f"{case.id}.{variant}"] = result["result_digest"]
                    manifest["execution_order"].append(f"{case.id}.{variant}")
                    manifest["results_written"] += 1
                    manifest["counts"] = counts
                    self.store.finish(manifest)
                    if not manifest["comparison_valid"]:
                        break
                if not manifest["comparison_valid"]:
                    break
            manifest["status"] = (
                "completed" if manifest["comparison_valid"] else "authority_changed"
            )
        except BaseException:
            manifest["status"] = "interrupted"
            raise
        finally:
            manifest["finished_at"] = datetime.now(UTC).isoformat()
            manifest["counts"] = counts
            state = (
                "failed"
                if manifest["status"] == "authority_changed"
                else "partial"
                if (
                    counts["error"]
                    or counts["invalid"]
                    or manifest["status"] == "interrupted"
                    or len(selection) != len(dataset.cases)
                )
                else "executed"
            )
            manifest["verification_states"]["retrieval_baseline"] = state
            if answers:
                manifest["verification_states"]["real_provider_answers"] = state
            self.store.finish(manifest)
        return manifest
