"""Synthetic evaluator cases only; benchmark payloads live in the excluded directory."""

from __future__ import annotations

import ast
import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from typer.testing import CliRunner

from buildlens.application.ask import AskService, GenerationOutputInvalidError
from buildlens.application.context import ContextService, RerankerRuntime
from buildlens.application.evaluation import Collector, EvaluationRunner, error_code
from buildlens.application.evaluation_binding import (
    bind_case,
    check_corpus,
    chunk_metadata,
    definition_digest,
    validate_definitions,
)
from buildlens.application.evaluation_metrics import (
    coverage,
    ranking_metrics,
    score_result,
    semantic_metrics,
)
from buildlens.application.observation import observing
from buildlens.application.retrieval import RetrievalService
from buildlens.cli.main import app
from buildlens.domain.eligibility import POLICY_VERSION, exclusion_reason
from buildlens.domain.embeddings import ChunkEmbedding, EmbeddingContract, EmbeddingRun
from buildlens.domain.entities import Artifact, ProjectVersion
from buildlens.domain.evaluation import (
    CATEGORIES,
    RUBRIC_VERSION,
    SCHEMA_VERSION,
    ChunkSelector,
    CorpusEvidence,
    EvaluationCase,
    EvaluationDataset,
    EvaluationError,
    EvidenceRequirement,
    EvidenceTarget,
    RelevanceJudgment,
    digest,
    excerpt_hash,
    primitive,
)
from buildlens.domain.fingerprints import ContentFingerprint
from buildlens.domain.repository_snapshot import ArtifactMetadata
from buildlens.domain.retrieval import RetrievalMode
from buildlens.infrastructure.evaluation.local import (
    LocalRunStore,
    atomic_write,
    boundary_path,
    dataset_from,
    validate_review,
)
from buildlens.infrastructure.evaluation.report import compare, load_run, report
from buildlens.infrastructure.structural.reader import PosixSecureSourceReader, SourceReadError
from tests.unit.test_ask import FakeProvider
from tests.unit.test_context import FakeEvidenceStore, FakeReranker, chunk, retrieval, run, service


@pytest.fixture
def corpus(tmp_path: Path) -> CorpusEvidence:
    chunks = (
        chunk(1, "abcde"),
        chunk(2, "fghij", start=5, part=1),
        chunk(3, "klmno", start=10, part=2),
    )
    fingerprint = ContentFingerprint("sha256", "artifact-bytes.v1", excerpt_hash("abcdefghijklmno"))
    now = datetime.now(UTC)
    artifact = Artifact(
        UUID(int=200),
        UUID(int=400),
        "file",
        "src/example.py",
        now,
        ArtifactMetadata("included", 15, "python", (), "text", "not_ignored", (), False),
        content_fingerprint=fingerprint,
        fingerprint_status="fingerprinted",
    )
    snapshot = ProjectVersion(
        UUID(int=400),
        UUID(int=500),
        "synthetic",
        now,
        canonical_root=str(tmp_path),
        scan_status="complete",
        policy_version=POLICY_VERSION,
        eligibility_provenance={"policy_version": POLICY_VERSION},
        content_manifest=fingerprint,
    )
    contract = EmbeddingContract(
        "fake",
        "1",
        "model",
        "a" * 40,
        2,
        "fake",
        "cpu",
        "float32",
        "",
        "",
        "plain",
        "plain",
        512,
        "window",
        "mean",
        "unit",
        "0",
        "0",
        "0",
    )
    embedding = EmbeddingRun(
        UUID(int=600),
        UUID(int=500),
        UUID(int=400),
        UUID(int=300),
        2,
        {},
        contract,
        1,
        now,
        now,
        "complete",
        False,
        False,
        None,
        0,
        "membership",
        {},
        {},
        (),
    )
    rows = tuple(
        ChunkEmbedding(
            UUID(int=700 + i),
            embedding.id,
            UUID(int=500),
            UUID(int=400),
            UUID(int=300),
            c.artifact_processing_id,
            c.id,
            c.stable_key,
            c.content_hash,
            "key",
            contract.digest,
            2,
            1,
            "generated",
        )
        for i, c in enumerate(chunks)
    )
    manifest = {
        "version": "stage11.corpus.v1",
        "searchable_artifacts": [{"locator": artifact.locator, "fingerprint": fingerprint.digest}],
        "chunks": [{**chunk_metadata_stub(c), "language": "python"} for c in chunks],
    }
    return CorpusEvidence(snapshot, (artifact,), run(), embedding, chunks, rows, manifest)


def chunk_metadata_stub(c: Any) -> dict[str, Any]:
    return {
        "chunk_id": str(c.id),
        "locator": c.locator,
        "stable_key": c.stable_key,
        "content_hash": c.content_hash,
        "span": primitive(c.span),
    }


def case(
    corpus: CorpusEvidence, *, category: str = "identifier", id: str = "case"
) -> EvaluationCase:
    assert corpus.artifacts[0].content_fingerprint is not None
    target = EvidenceTarget(
        "target",
        "src/example.py",
        corpus.artifacts[0].content_fingerprint.digest,
        0,
        10,
        excerpt_hash("abcdefghij"),
    )
    judgments = tuple(
        RelevanceJudgment(
            ChunkSelector(
                cast(str, c.locator),
                target.artifact_fingerprint,
                c.stable_key,
                c.content_hash,
                c.span.start_offset,
                c.span.end_offset,
            ),
            i < 2,
            "synthetic reviewer",
            "synthetic rationale",
            "2026-10-01",
        )
        for i, c in enumerate(corpus.chunks)
    )
    from buildlens.domain.retrieval import RetrievalFilters

    answerable = category != "unsupported"
    return EvaluationCase(
        id,
        "Synthetic question?",
        category,
        RetrievalFilters(),
        answerable,
        ("Synthetic facet",) if answerable else (),
        ("Invented fact",),
        (target,),
        (EvidenceRequirement("group", "Synthetic fact", (("target",),)),) if answerable else (),
        judgments,
    )


def dataset(corpus: CorpusEvidence) -> EvaluationDataset:
    cases = tuple(
        case(corpus, category=category, id=f"{category}-{i}")
        for category in CATEGORIES
        for i in range(6)
    )
    return EvaluationDataset(
        SCHEMA_VERSION,
        "synthetic",
        "1",
        digest(corpus.manifest),
        corpus.manifest,
        "draft",
        None,
        RUBRIC_VERSION,
        cases,
    )


def test_metrics_hand_calculated_short_ranking_and_errors(corpus: CorpusEvidence) -> None:
    c = case(corpus)
    binding = bind_case(c, corpus)
    rows = [chunk_metadata(ch, corpus) for ch in corpus.chunks]
    metrics = ranking_metrics(c, binding, [rows[2], rows[0], rows[1]])
    assert metrics["rr@32"] == 0.5
    assert metrics["1"]["recall"] == 0
    assert metrics["5"]["recall"] == 1
    assert metrics["5"]["precision"] == 2 / 5
    assert metrics["5"]["required_evidence"]["complete"] is True
    assert ranking_metrics(c, binding, [])["rr@32"] == 0
    assert score_result(c, binding, {}, "error") == {"eligible": False, "outcome": "error"}
    with pytest.raises(EvaluationError, match="DUPLICATE_RANKING"):
        ranking_metrics(c, binding, [rows[0], rows[0]])


def test_unjudged_and_empty_ground_truth_withhold(corpus: CorpusEvidence) -> None:
    c = case(corpus)
    binding = bind_case(c, corpus)
    rows = [chunk_metadata(ch, corpus) for ch in corpus.chunks]
    missing = replace(binding, judged={str(corpus.chunks[0].id): True})
    values = ranking_metrics(c, missing, rows)
    assert values["5"]["precision"] is None
    assert values["5"]["judgment_coverage"] == 1 / 3
    assert values["provisional"] is True
    assert ranking_metrics(c, replace(binding, judged={}), rows)["rr@32"] is None
    unsupported = replace(c, answerable=False, category="unsupported", requirements=())
    assert ranking_metrics(unsupported, binding, rows)["5"]["recall"] is None
    assert coverage(unsupported, binding, rows)["coverage"] is None


def test_gap_free_union_alternatives_and_filtered_binding(corpus: CorpusEvidence) -> None:
    c = case(corpus)
    binding = bind_case(c, corpus)
    rows = [chunk_metadata(ch, corpus) for ch in corpus.chunks]
    assert coverage(c, binding, rows[:1])["coverage"] == 0
    assert coverage(c, binding, rows[:2])["coverage"] == 1
    alternate = replace(
        c.targets[0],
        id="alternate",
        start_offset=10,
        end_offset=15,
        excerpt_hash=excerpt_hash("klmno"),
    )
    c = replace(
        c,
        targets=c.targets + (alternate,),
        requirements=(EvidenceRequirement("group", "fact", (("target",), ("alternate",))),),
    )
    assert coverage(c, bind_case(c, corpus), rows[2:])["complete"] is True
    c = replace(c, filters=replace(c.filters, locator_prefix="excluded/"))
    with pytest.raises(EvaluationError, match="FILTERED"):
        bind_case(c, corpus)
    changed = replace(
        corpus,
        artifacts=(
            replace(
                corpus.artifacts[0],
                content_fingerprint=ContentFingerprint("sha256", "artifact-bytes.v1", "0" * 64),
            ),
        ),
    )
    with pytest.raises(EvaluationError, match="CHANGED"):
        bind_case(case(corpus), changed)


def test_boundary_and_contamination(corpus: CorpusEvidence, tmp_path: Path) -> None:
    for locator in (
        ".buildlens-eval/datasets/tracked.json",
        ".buildlens-eval/runs/r/case.json",
        "sub/.buildlens-eval/reviews/r.json",
    ):
        assert exclusion_reason(locator, False) == "evaluation_data"
        with pytest.raises(SourceReadError, match="Artifact") as error:
            PosixSecureSourceReader().read(str(tmp_path), locator, 100)
        assert error.value.code == "SOURCE_EXCLUDED"
    with pytest.raises(EvaluationError, match="PROVENANCE"):
        check_corpus(
            dataset(corpus),
            replace(corpus, snapshot=replace(corpus.snapshot, policy_version="stage2.v2")),
        )
    contaminated = replace(
        corpus,
        chunks=(replace(corpus.chunks[0], locator=".buildlens-eval/data.json"),)
        + corpus.chunks[1:],
    )
    with pytest.raises(EvaluationError, match="CONTAMINATED"):
        check_corpus(dataset(corpus), contaminated)
    with pytest.raises(EvaluationError, match="NOT_PINNED"):
        check_corpus(replace(dataset(corpus), corpus_manifest=None, corpus_digest=None), corpus)
    root = tmp_path / "repo"
    root.mkdir()
    assert boundary_path(root / ".buildlens-eval/runs/run", root, "runs").is_relative_to(root)
    with pytest.raises(EvaluationError, match="OUTSIDE"):
        boundary_path(tmp_path / "outside", root, "runs")
    (root / ".buildlens-eval").symlink_to(tmp_path)
    with pytest.raises(EvaluationError, match="SYMLINK"):
        boundary_path(root / ".buildlens-eval/runs/run", root, "runs")


def test_approval_requires_concrete_provenance_and_digest(corpus: CorpusEvidence) -> None:
    d = dataset(corpus)
    validate_definitions(d)
    with pytest.raises(EvaluationError, match="APPROVAL"):
        validate_definitions(replace(d, review_status="approved"))
    approved = replace(
        d,
        review_status="approved",
        approval={
            "reviewer": "synthetic human",
            "reviewed_at": "2026-10-01",
            "rationale": "synthetic approval",
            "definition_digest": definition_digest(d),
            "judgments_digest": d.judgments_digest,
        },
    )
    validate_definitions(approved)
    assert dataset_from(primitive(d)) == d
    with pytest.raises(EvaluationError, match="DUPLICATE_JSON"):
        from buildlens.infrastructure.evaluation.local import read_json

        target = Path(cast(str, corpus.snapshot.canonical_root)) / "duplicate.json"
        target.write_text('{"x":1,"x":2}')
        read_json(target)


def test_passive_parity_and_failed_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    values = [chunk(i, f"value-{i}", start=i * 20) for i in range(1, 35)]
    reranker = FakeReranker({v.id: float(i) for i, v in enumerate(values)})
    context = service(values, reranker)
    baseline = context.build(
        UUID(int=500), UUID(int=400), "Synthetic?", "lexical", "local_cross_encoder"
    )
    collector = Collector()
    with observing(collector):
        observed = context.build(
            UUID(int=500), UUID(int=400), "Synthetic?", "lexical", "local_cross_encoder"
        )
    assert baseline == observed
    assert baseline.ranking_observation is not None
    assert baseline.ranking_observation.reranker_admission_limit == 32
    assert sum(c.selection_order is not None for c in baseline.ranking_observation.candidates) == 32
    from buildlens.api.context_routes import response

    assert response(baseline).model_dump() == response(observed).model_dump()
    monkeypatch.setattr(
        ContextService,
        "_observe_ranking",
        lambda *args: (_ for _ in ()).throw(RuntimeError("synthetic")),
    )
    with observing(collector):
        failed_observation = context.build(
            UUID(int=500), UUID(int=400), "Synthetic?", "lexical", "local_cross_encoder"
        )
    assert failed_observation == baseline
    assert failed_observation.ranking_observation is None
    assert "ranking" in collector.failures


def test_ask_prompt_parity_and_failure_checkpoint() -> None:
    from tests.unit.test_ask import question

    provider = FakeProvider()
    baseline = question(provider, ["Evidence."])
    collector = Collector()
    with observing(collector):
        observed = question(provider, ["Evidence."])
    assert baseline.original_context_digest == observed.original_context_digest
    assert baseline.evidence_digest == observed.evidence_digest
    assert provider.counts[:3] == provider.counts[3:]
    assert provider.generations[0] == provider.generations[1]
    with observing(collector), pytest.raises(GenerationOutputInvalidError):
        question(FakeProvider("rejected raw provider data"), ["Evidence."])
    assert "generation_evidence" in collector.values
    assert "ask_context" in collector.values
    assert "rejected raw provider data" not in repr(collector.values)
    assert error_code(RuntimeError("postgresql://user:secret@example/db")) == "OPERATION_FAILED"


class SyntheticStore:
    def __init__(self, value: CorpusEvidence, *, fail_at: int | None = None) -> None:
        self.value, self.calls, self.fail_at = value, 0, fail_at

    def load(self, *args: Any) -> CorpusEvidence:
        return self.value

    def verify(self, *args: Any) -> None:
        self.calls += 1
        if self.fail_at is not None and self.calls >= self.fail_at:
            raise EvaluationError("RUN_AUTHORITY_CHANGED")


def native_services(corpus: CorpusEvidence) -> tuple[ContextService, AskService]:
    class SyntheticRetrieval:
        def context_candidates(
            self, project: UUID, snapshot: UUID, mode: RetrievalMode, *args: Any
        ) -> Any:
            return replace(
                retrieval(list(corpus.chunks)),
                requested_mode=mode,
                executed_mode=mode,
                embedding_run=corpus.embedding,
            )

    context = ContextService(
        cast(RetrievalService, SyntheticRetrieval()),
        RerankerRuntime(FakeReranker({c.id: float(i) for i, c in enumerate(corpus.chunks)})),
        FakeEvidenceStore(list(corpus.chunks)),
    )
    return context, AskService(context, FakeProvider(), enabled=True)


def execute(corpus: CorpusEvidence, *, answers: bool = False, fail_at: int | None = None) -> Path:
    context, ask = native_services(corpus)
    path = (
        Path(cast(str, corpus.snapshot.canonical_root))
        / ".buildlens-eval/runs"
        / ("answers" if answers else "context")
    )
    runner = EvaluationRunner(
        SyntheticStore(corpus, fail_at=fail_at), context, ask, LocalRunStore(path)
    )
    asyncio.run(
        runner.run(
            dataset(corpus),
            UUID(int=500),
            UUID(int=400),
            answers=answers,
            implementation={"synthetic": True},
            selected_cases=("identifier-0", "unsupported-0"),
        )
    )
    return path


def test_runner_artifacts_rescore_and_pairing(corpus: CorpusEvidence) -> None:
    path = execute(corpus)
    first = report(path)
    original = (path / "scores.json").read_bytes()
    assert report(path) == first
    assert (path / "scores.json").read_bytes() == original
    assert len(first["records"]) == 6
    assert first["score_record_version"] == "stage11.score-reference.v1"
    assert all("observations" not in r and "expected_excerpts" not in r for r in first["records"])
    assert {r["result_digest"] for r in first["records"]} == {
        r["result_digest"] for r in load_run(path)[2]
    }
    assert "right minus left" in compare(path, path)
    assert "32 candidates" in (path / "summary.md").read_text()
    assert (path / "relevance-templates/identifier-0.vector.json").exists()
    with pytest.raises(FileExistsError):
        execute(corpus)
    (path / "cases/identifier-0.vector.json").write_text("{}")
    with pytest.raises((KeyError, EvaluationError)):
        report(path)


def test_authority_change_retains_diagnostics_and_invalidates_comparison(
    corpus: CorpusEvidence,
) -> None:
    path = execute(corpus, fail_at=2)
    report(path)
    manifest = json.loads((path / "manifest.json").read_text())
    assert manifest["status"] == "authority_changed"
    assert manifest["results_written"] == 1
    result = json.loads(next((path / "cases").glob("*.json")).read_text())
    assert result["observations"]["ranking"]
    assert result["metrics"]["eligible"] is False
    with pytest.raises(EvaluationError, match="AUTHORITY"):
        compare(path, path)


def test_human_semantics_are_separate_from_structure(corpus: CorpusEvidence) -> None:
    path = execute(corpus, answers=True)
    scores = report(path)
    result = load_run(path)[2][0]
    assert scores["records"][0]["result_digest"] == result["result_digest"]
    assert result["metrics"]["structural_answer"]["citation_membership"] is True
    review = {
        "result_digest": result["result_digest"],
        "reviewer": "synthetic reviewer",
        "reviewed_at": "2026-10-01",
        "rubric_version": RUBRIC_VERSION,
        "rationale": "synthetic judgment",
        "facets": [],
        "statements": [
            {
                "index": 0,
                "judgment": "unsupported",
                "substantive": True,
                "sufficient_cited_support": False,
                "support_kind": "inappropriate",
                "rationale": "unsupported",
            }
        ],
        "links": [
            {
                "statement_index": 0,
                "evidence_id": "E1",
                "judgment": "unsupported",
                "rationale": "unrelated",
            }
        ],
        "correctness": "incorrect",
        "abstention": "not_applicable",
    }
    validate_review(review, result, case(corpus))
    semantic = semantic_metrics(review, result["observations"]["answer"])
    assert semantic["citation_precision"] == 0
    assert semantic["citation_completeness"] == 0
    assert semantic["reviewed_statements"] == 1
    with pytest.raises(EvaluationError, match="PROVENANCE"):
        validate_review({**review, "result_digest": "changed"}, result, case(corpus))


def test_cli_help_and_atomic_nonoverwrite(tmp_path: Path) -> None:
    cli = CliRunner()
    for command in ("validate", "run", "report", "compare"):
        result = cli.invoke(app, ["eval", command, "--help"])
        assert result.exit_code == 0, result.output
    result = cli.invoke(app, ["eval", "run"])
    assert result.exit_code == 2
    path = tmp_path / "result.json"
    atomic_write(path, {"retained": True})
    with pytest.raises(FileExistsError):
        atomic_write(path, {"retained": False})
    assert json.loads(path.read_text()) == {"retained": True}
    assert not list(tmp_path.glob(".pending-*"))


def test_production_does_not_read_observation_record() -> None:
    root = Path(__file__).parents[2] / "src/buildlens"
    for area in ("application", "api", "infrastructure/generation"):
        for source in (root / area).glob("*.py"):
            if source.name.startswith("evaluation"):
                continue
            tree = ast.parse(source.read_text())
            assert not any(
                isinstance(node, ast.Attribute) and node.attr == "ranking_observation"
                for node in ast.walk(tree)
            ), source


def test_inventory_prunes_tracked_benchmark_assets(tmp_path: Path) -> None:
    import subprocess

    from buildlens.infrastructure.discovery.filesystem import LocalRepositoryInspector

    (tmp_path / ".buildlens-eval/datasets").mkdir(parents=True)
    (tmp_path / ".buildlens-eval/datasets/synthetic.json").write_text("{}")
    (tmp_path / "ordinary.py").write_text("pass\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Synthetic Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    result = LocalRepositoryInspector().inspect(str(tmp_path))
    assert result.policy_version == POLICY_VERSION
    boundary = next(a for a in result.artifacts if a.locator == ".buildlens-eval")
    assert boundary.metadata.pruned
    assert "evaluation_data" in boundary.metadata.exclusion_reasons
    assert not any(a.locator.endswith("synthetic.json") for a in result.artifacts)
    assert (
        next(a for a in result.artifacts if a.locator == "ordinary.py").metadata.inventory_status
        == "included"
    )


def test_interruption_leaves_atomic_case_and_manifest(corpus: CorpusEvidence) -> None:
    context, ask = native_services(corpus)
    path = Path(cast(str, corpus.snapshot.canonical_root)) / ".buildlens-eval/runs/interrupted"
    calls = 0

    def progress(value: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt

    runner = EvaluationRunner(
        SyntheticStore(corpus), context, ask, LocalRunStore(path), progress=progress
    )
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(
            runner.run(
                dataset(corpus),
                UUID(int=500),
                UUID(int=400),
                answers=False,
                implementation={},
                selected_cases=("identifier-0",),
            )
        )
    manifest = json.loads((path / "manifest.json").read_text())
    assert manifest["status"] == "interrupted"
    assert manifest["results_written"] == 1
    assert len(report(path)["records"]) == 1
    assert not list(path.rglob(".pending-*"))


def test_revised_judgments_rescore_without_retrieval(corpus: CorpusEvidence) -> None:
    path = execute(corpus)
    original = report(path)
    d = dataset(corpus)
    changed_case = replace(
        d.cases[0], judgments=tuple(replace(j, relevant=False) for j in d.cases[0].judgments)
    )
    revised = replace(d, version="2", cases=(changed_case,) + d.cases[1:])
    rescored = report(path, revised=revised)
    assert rescored["judgments_digest"] != original["judgments_digest"]
    assert (
        next(r for r in rescored["records"] if r["case_id"] == changed_case.id)["metrics"][
            "ranking"
        ]["rr@32"]
        is None
    )
    assert compare(path, path)
    assert json.loads((path / "dataset.json").read_text())["version"] == "1"


def test_failed_collector_cannot_change_production_answer() -> None:
    from tests.unit.test_ask import question

    class BrokenCollector:
        def record(self, *args: Any) -> None:
            raise RuntimeError("collector failure")

        def unavailable(self, *args: Any) -> None:
            raise RuntimeError("collector failure")

    baseline = question(FakeProvider(), ["Evidence."])
    with observing(BrokenCollector()):
        observed = question(FakeProvider(), ["Evidence."])
    assert baseline.answer_text == observed.answer_text
    assert baseline.evidence_digest == observed.evidence_digest
    assert baseline.original_context_digest == observed.original_context_digest


def test_corpus_adapter_uses_published_membership_order_and_checks_processing(
    corpus: CorpusEvidence,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from buildlens.domain.embeddings import membership_digest
    from buildlens.domain.processing import ArtifactProcessing
    from buildlens.domain.source import processing_contract
    from buildlens.infrastructure.evaluation.corpus import SqlAlchemyEvaluationCorpus

    contract: dict[str, object] = dict(processing_contract())
    assert corpus.artifacts[0].content_fingerprint is not None
    processing = replace(corpus.processing, processing_contract=contract)
    embedding = replace(
        corpus.embedding,
        processing_contract=contract,
        membership_digest=membership_digest(list(corpus.embeddings)),
    )
    processed = ArtifactProcessing(
        UUID(int=100),
        processing.id,
        corpus.snapshot.id,
        corpus.artifacts[0].id,
        corpus.artifacts[0].locator,
        "processed",
        None,
        corpus.artifacts[0].content_fingerprint.digest,
        "utf-8",
        stored_chunk_count=len(corpus.chunks),
    )
    adapter = SqlAlchemyEvaluationCorpus(None)
    fake_processing = SimpleNamespace(
        get_snapshot=lambda *a: corpus.snapshot,
        active=lambda *a: processing,
        artifacts=lambda *a: list(corpus.artifacts),
        page_artifacts=lambda *a: ([processed], 1),
        content_stats=lambda *a: (len(corpus.chunks), 15),
        page_chunk_content=lambda *a: list(corpus.chunks),
    )
    fake_embedding = SimpleNamespace(
        active=lambda *a: embedding,
        page_rows=lambda *a: (list(reversed(corpus.embeddings)), len(corpus.embeddings)),
    )
    monkeypatch.setattr(adapter, "processing", fake_processing)
    monkeypatch.setattr(adapter, "embedding", fake_embedding)
    monkeypatch.setattr(adapter, "authority", FakeEvidenceStore(list(corpus.chunks)))
    observed = adapter.load(UUID(int=500), UUID(int=400))
    assert observed.manifest["embedding_membership_digest"] == embedding.membership_digest
    assert len(observed.manifest["embedding_observations"]) == 3
    assert "values" not in observed.manifest["embedding_observations"][0]
    fake_processing.page_artifacts = lambda *a: (
        [replace(processed, locator=".buildlens-eval/data.json")],
        1,
    )
    with pytest.raises(EvaluationError, match="CONTAMINATED"):
        adapter.load(UUID(int=500), UUID(int=400))


def test_ignore_control_access_cannot_bypass_benchmark_exclusion(tmp_path: Path) -> None:
    control = tmp_path / ".git/info/exclude"
    control.parent.mkdir(parents=True)
    control.write_text("ignored.txt\n")
    reader = PosixSecureSourceReader()
    assert (
        reader.read_ignore_control(str(tmp_path), ".git/info/exclude", 100).data == b"ignored.txt\n"
    )
    with pytest.raises(SourceReadError):
        reader.read(str(tmp_path), ".git/info/exclude", 100)
    with pytest.raises(SourceReadError):
        reader.read_ignore_control(str(tmp_path), ".buildlens-eval/datasets/data.json", 100)


def test_provider_reported_settings_do_not_infer_sdk_defaults() -> None:
    from types import SimpleNamespace

    from buildlens.application.observation import provider_reported_settings

    collector = Collector()
    response = SimpleNamespace(
        temperature=0, max_output_tokens=2048, model_fields_set={"max_output_tokens"}
    )
    with observing(collector):
        provider_reported_settings(response, "synthetic.response")
    assert collector.values["provider_reported_settings"] == {
        "max_output_tokens": {"value": 2048, "provenance": "synthetic.response.max_output_tokens"}
    }


def test_shared_runtime_cleans_every_owner_when_one_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from buildlens.infrastructure.runtime import LocalRuntime

    closed = []

    def embedding_close() -> None:
        closed.append("embedding")
        raise RuntimeError("synthetic close failure")

    async def provider_close() -> None:
        closed.append("provider")

    runtime = LocalRuntime.__new__(LocalRuntime)
    monkeypatch.setattr(
        runtime, "embedding_runtime", SimpleNamespace(close=embedding_close), raising=False
    )
    monkeypatch.setattr(
        runtime,
        "reranker_runtime",
        SimpleNamespace(close=lambda: closed.append("reranker")),
        raising=False,
    )
    monkeypatch.setattr(
        runtime, "generation_provider", SimpleNamespace(close=provider_close), raising=False
    )
    runtime.engine = SimpleNamespace(dispose=lambda: closed.append("engine"))
    with pytest.raises(RuntimeError, match="synthetic close failure"):
        asyncio.run(runtime.close())
    assert closed == ["embedding", "reranker", "provider", "engine"]


def test_independent_verification_record_is_explicit_and_digest_checked(
    corpus: CorpusEvidence,
) -> None:
    path = execute(corpus)
    initial = report(path)
    verification = path.parent / "verification.json"
    record = {
        "version": "stage11.verification.v1",
        "infrastructure_status": "passed",
        "evidence": "synthetic fixture only",
    }
    record["record_digest"] = digest(record)
    atomic_write(verification, record)
    attached = report(path, verification=verification)
    assert attached["verification_record"]["status"] == "passed"
    assert attached["verification_record"]["digest"] == record["record_digest"]
    assert [r["metrics"] for r in initial["records"]] == [r["metrics"] for r in attached["records"]]
    assert attached["review_count"] == 0
    assert "Recorded passed (independent checks" in (path / "summary.md").read_text()
    assert report(path, verification=verification) == attached
    atomic_write(verification, {**record, "infrastructure_status": "failed"}, replace=True)
    with pytest.raises(EvaluationError, match="VERIFICATION_RECORD_INVALID"):
        report(path, verification=verification)
