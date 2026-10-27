# Native RAG evaluation

Stage 11 evaluates the production retrieval, context and Ask services from the local CLI.
It adds no HTTP routes, frontend, database tables, migrations, LLM judge or external evaluation
framework. All three variants execute sequentially with the existing worker ownership and locks:
semantic / retrieval order, hybrid / retrieval order, and hybrid / local cross-encoder.

**Stage 11 closed, 2026-10-02.** All scoped implementation, verification, approval and
reporting work is finalized. Provider execution is **inconclusive/partial**; answer quality
is **not passed**. Stage 12 has not started.

## Current verification states

| Dimension | Current state |
| --- | --- |
| Infrastructure verification | Passed: migrated PostgreSQL integration, backend regressions on Python 3.11/3.14, full static/frontend checks, and real local-model tests |
| Retrieval baseline execution | Executed: 90/90 successful production evaluations; rescored with approved curated relevance at 1/5/10/32; zero invalid/error/unavailable observations |
| Human ground-truth approval | Approved R1: 30 cases and 798 judgments (98 relevant / 700 irrelevant); 30/30 native bindings valid |
| Real-provider answer evaluation | Executed; inconclusive/partial: 88/90 operational failures, 2 valid abstentions, zero answered responses; answer quality not passed |
| Semantic answer review | Approved A1: 2/2 valid responses reviewed; safe abstentions, task-incorrect and missing sole facets; 88 errors ineligible |

The 2026-10-02 closure passed 143 deterministic backend tests on each Python 3.11.15/3.14.6,
full Ruff/format/mypy checks, three real local-model tests with PostgreSQL, and 16 frontend tests,
lint, TypeScript and production build. The retained verification record lives at
`.buildlens-eval/runs/preparation-20261002/verification.json`. Two existing Starlette/httpx/anyio
deprecation warnings remain. Earlier historical Stage 9/10 evidence is independent.

## Actual real-corpus measurements, 2026-10-02

Normal preparation created project `641a63cd-b382-438f-b34a-8d6015e353c0`, snapshot
`0360b2ca-ca58-4f6c-9544-c3d3870997a8`, and 2,099 real MiniLM embeddings. The isolated retained
PostgreSQL 16.15/pgvector 0.8.6 container is `buildlens-stage11-postgres`, port 55411, database
`buildlens_stage11`, volume `buildlens-stage11-postgres-data`; existing databases were untouched.
Python 3.11.15 is required for this corpus's processing/runtime contract. The temporary environment
is `/private/tmp/buildlens-stage11-py311`, with verified model caches under
`/private/tmp/buildlens-stage10-models/{embeddings,reranking}`. Temporary paths may later be removed;
model acquisition must be explicit if the caches become unavailable.

The corpus manifest/digest, full contracts and native membership are pinned in
`.buildlens-eval/datasets/corpus-20261002.json`, the retained `buildlens-v3.bound-draft.json`,
and the approved `buildlens-v4.r1-reviewed.json` scoring revision (`4-r1-reviewed`).
Corpus SHA-256: `03c559c4fc0b585ae6da7256711e5219e07f60469d0c2d89bfd13e4c9e655ce3`.
There are 270 inventory entries, 157 searchable artifacts, 3 empty successful artifacts,
80 excluded entries, 28 unsupported entries, and 2 failed processing artifacts. Structure rejected
two existing test fixtures for `RECOGNIZABLE_GITHUB_TOKEN`; processing records
`STRUCTURAL_PREREQUISITE_MISSING`. TS/TSX and other unsupported formats are not searched.
No failed artifact supplies a benchmark target. Partial processing is an explicitly recorded
coverage limit; embeddings cover every retained chunk. All 30 exact source bindings passed.

Run `7e93df61-1fe5-4ba4-adda-d6c33518960a` is retained under
`.buildlens-eval/runs/retrieval-20261002/`, including native results, source excerpts, scores,
summary, and compact `measured-results.json`. It ran all 30 cases sequentially through all three
production variants: **90 success, 0 error, 0 invalid, 0 unavailable observations**, with stable
authority throughout, in **160.42 seconds**. The requirement table below averages coverage per case
over the **24 answerable approved cases**; complete evidence counts cases satisfying all required groups.

The user explicitly approved packet R1 with `Approve Stage 11 relevance packet R1` on
2026-10-02. The new dataset revision records that human acceptance, its proposal digest and sealed
definition/judgment digests. The original draft and all 90 native case artifacts are preserved.
Native validation passed all 30 reviewed cases against the same retained corpus and model caches.
Offline rescoring produced these **case macro means**, each with **24 eligible answerable cases**:

| Ranking metric | Semantic | Hybrid | Hybrid + reranker |
| --- | --- | --- | --- |
| Recall@1 | 11.04% | 11.04% | 15.35% |
| Precision@1 | 33.33% | 33.33% | 45.83% |
| Recall@5 | 20.64% | 20.64% | 42.94% |
| Precision@5 | 15.00% | 15.00% | 33.33% |
| Recall@10 | 39.71% | 39.71% | 51.18% |
| Precision@10 | 15.42% | 15.42% | 20.42% |
| Recall@32 | 64.34% | 64.34% | 64.34% |
| Precision@32 | 8.85% | 8.85% | 8.85% |
| MRR@32 | 0.4560 | 0.4560 | 0.5972 |

Every returned answerable position through each reported cutoff has an approved judgment
(100% coverage). The curated reference set includes all relevant cutoff candidates and 30 known
relevant sources outside rank 32. Recall is not exhaustive corpus relevance. Six unsupported cases
have Recall/Precision/MRR marked inapplicable. The Stage 8 retrieval-stage lists are identical
across variants; their cutoff metrics equal the semantic ranking column. Only the actual pre-seed
ranking order changes with reranking. Required-source-span coverage remains a separate metric:

| Stage/cutoff | Semantic coverage; complete | Hybrid coverage; complete | Hybrid + reranker coverage; complete |
| --- | --- | --- | --- |
| Ranking@1 | 8.33%; 2/24 | 8.33%; 2/24 | 14.58%; 3/24 |
| Ranking@5 | 16.67%; 3/24 | 16.67%; 3/24 | 27.08%; 6/24 |
| Ranking@10 | 31.25%; 6/24 | 31.25%; 6/24 | 32.64%; 7/24 |
| Ranking@32 | 44.44%; 9/24 | 44.44%; 9/24 | 44.44%; 9/24 |
| Packed context | 27.08%; 5/24 | 27.08%; 5/24 | 33.33%; 7/24 |

Mean evaluation duration was 1.290 s / 1.294 s / 2.719 s, respectively. It includes full corpus
verification before/after each call and first-use model loading; it is not pure request latency.
The pinned CPU models are MiniLM embedding revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`
and cross-encoder revision `233902d25c440f23af6f7d6e94d2946bac0bee0a`.

**Limitations:** semantic and hybrid produced identical ranking orders on all 30 questions. The
lexical component actually executed but returned zero candidates for these full-sentence queries;
its production all-term matching rule is unchanged. These results cannot demonstrate a hybrid
advantage. Evidence targets originated as agent proposals, including 11 explicit refinements in
`buildlens-v3.revision.json`, and were explicitly accepted in packet R1. Original definitions are
unchanged in the reviewed revision. Positions 33–100 remain unjudged except curated supporting
sources; native whole-list provisional flags therefore remain, while the reported-cutoff metrics
have complete approved labels. The two valid provider responses have approved A1 reviews;
88 operational failures prevent a complete answer-quality comparison.
Unsupported cases have no requirement-coverage denominator. There is no aggregate quality score.
Paired reranker MRR improved in 8 cases, worsened in 4 and remained equal in 12 (mean +0.1413).
Recall/Precision@5 improved in 13 cases, worsened in none and stayed equal in 11.
In paired packed-context coverage, reranking improved 3
cases, reduced coverage in 2, and left 19 unchanged. Complete evidence improved in 3 cases and
regressed in 1 (net +2/24); its benefit was not uniform. These paired native measurements are
retained in `paired-variant-differences.json`.

The original raw relevance packets under `.buildlens-eval/reviews/relevance-20261002/` contain
3,000 deduplicated case–candidate pairs and remain preserved. The approved compact R1 workflow
under `.buildlens-eval/reviews/closure-proposal/` presents 30 cards and 798 necessary judgments:
768 shared top-32 pairs plus 30 relevant reference chunks outside the cutoff. `proposal.json`
preserves the agent proposals; `human-approval.json` records the user's actual explicit message,
without inventing a personal reviewer identity. The reviewed dataset is
`.buildlens-eval/datasets/buildlens-v4.r1-reviewed.json`; its revision record retains approval
scope and digests. Actual results, paired differences and deterministic/immutable-record checks
are in the retained run's `reviewed-results-r1.json`, `reviewed-retrieval-r1.md` and
`reviewed-rescoring-verification-r1.json`. Repeated reviewed reporting was byte-identical,
all 90 native case files remained unchanged, and offline self-comparison passed.

The earlier three-call preflight was rejected by automatic approval review and sent no provider
request. The human subsequently explicitly authorized external-sharing scope R1 and the 90
production Ask evaluations. The exact scope digest is
`46a0febdb7c0ae9ff93012ebba5acaec208af4a0e6d45123531611919df78911`;
`.buildlens-eval/reviews/gemini-authorization-r1/human-authorization.json` records the actual
message separately from the original proposal. The 2026-09-28 synthetic Gemini fixture remains
historical evidence, distinct from this repository-corpus run.

## Actual Gemini execution and finalized answer review

Run `e31f32f9-cc7a-4671-8988-a86e1690630b`, retained under
`.buildlens-eval/runs/answers-gemini-r1-20261002`, completed all 90 production Ask invocations
with stable corpus authority. Native execution completed; the CLI exited 1 because 88 requests
failed. The real-provider benchmark is **inconclusive/partial**, and answer quality is **not
passed**. Operational completion is not a successful quality result.

| Variant | Valid responses | Rate limited | Generation unavailable | Answered |
| --- | --- | --- | --- | --- |
| Semantic | 0/30 | 23 | 7 | 0 |
| Hybrid | 2/30 (both abstentions) | 23 | 5 | 0 |
| Hybrid + reranker | 0/30 | 23 | 7 | 0 |

All 270 token-count calls returned successful exact measurements. All 90 generation stages were
reached with full prompts measuring 4,286–10,417 input tokens; no retries, fallback or additional
generation request occurred. The two valid hybrid responses both returned `gemini-3.8-flash`:

| Result | Status | Reported input/output tokens | Total Ask duration |
| --- | --- | --- | --- |
| `identifier-06.hybrid` | `insufficient_evidence` | 8,363 / 537 | 26.58 s |
| `semantic-02.hybrid` | `insufficient_evidence` | 4,479 / 317 | 13.77 s |

Reported input tokens matched exact pre-generation counts for both responses. Output usage
includes reported thought tokens according to the existing adapter. Failed generation usage,
billing and the underlying causes of sanitized unavailable errors are unknown. A metadata-only
lookup confirmed the configured model advertises counting/generation; an offline transport-blocked
SDK construction check passed. Neither diagnostic generated an answer or sent repository context.
The requested Gemini thinking level remains `medium`, unsent and effective-unknown; no default
is inferred.

Scope verification matched every question and Stage 9 package to the authorized frozen scope.
Generation sources stayed within those packages, and every retrieval/ranking observation matched
the earlier baseline. Repeated offline reporting was byte-identical and preserved all 90 case
files. Four small tests passed for SDK prompt parity, Ask failure capture, error denominators
and separation of human semantics from structural checks; no full gate was repeated. Exact
measurements are in `measured-provider-results-r1.json`; packet/integrity checks are in
`.buildlens-eval/reviews/answer-proposal-a1/verification.json`.

**Human approval finalized:** the explicit message `Approve Stage 11 answer packet A1.`
accepted the six judgments for the only two valid responses. Actual provenance is retained in
`.buildlens-eval/reviews/answer-proposal-a1/human-approval.json`; native validated review files
are in its `reviewed/` directory. The original sealed agent proposal remains preserved as
presented, with blank human fields describing that historical proposal, not current status.
No LLM judge was used and no personal reviewer identity or exact message timestamp was invented.

| Human-reviewed result (hybrid only) | Task correctness | Required facet | Abstention safety |
| --- | --- | --- | --- |
| `identifier-06.hybrid` | Incorrect: requested builder not identified | Missing | Appropriate against supplied incomplete evidence |
| `semantic-02.hybrid` | Incorrect: empty-context behavior not explained | Missing | Appropriate against supplied incomplete evidence |

Conditional on **2 valid responses**, task-correct responses are **0/2**, fully met facets
are **0/2**, and safe abstentions are **2/2**. These counts are not corpus answer-quality
estimates or a provider pass. Review coverage is **2/2 eligible responses**, also **2/90
retained evaluations**; the other 88 are operationally ineligible. All 18 unsupported-question
evaluations failed, so this run establishes no live unsupported-question abstention result.
Grounding, citation precision/completeness and direct/inference support are **N/A**, with zero
statements, substantive claims or citation links. Errors have no answer to judge and are not
classified as abstentions or semantic incorrectness. A complete three-way quality comparison
is unavailable. The earlier approved retrieval scores remain unchanged.

The user also corrected the compact expected-answer summary: `processing_contract()` is in
`backend/src/buildlens/domain/source.py`, as already bound in the frozen approved target. This
changes only presentation; definitions, evidence selectors, approvals and semantic judgments
are unchanged. `APPROVAL.md` and the final summaries now use that exact locator.

The retained run was rescored with the two approved native reviews and independent verification.
`reviewed-answer-results-a1.json` contains the final counts, denominators and review/result
digests. The native summary says `partial: 2 result reviews` across the full run; all eligible
valid responses are reviewed, with no outstanding review for failed requests. Repeated scoring
was byte-identical; all 180 case files across the two runs, both execution manifests, the
reviewed dataset, authorized scope and retrieval scores remained unchanged. Historical
execution-time manifests and as-presented proposals retain their original states; current
closure/status records refer to the explicit approvals.

Three focused finalization tests passed for semantic/structural separation, approval provenance
and offline rescoring. Native review validation, score-integrity and zero-denominator checks
passed. No full gate or provider request was repeated. Stage 11 is **closed with the
inconclusive/partial real-provider outcome**, retaining answer quality as **not passed**.
Final gate statuses, proof and limitations are in
`.buildlens-eval/runs/stage11-closure-20261002/{closure.json,REPORT.md,verification.json}`.
No required human decision remains. No Stage 12 work was started.

## Definitions and corpus preparation

`.buildlens-eval/datasets/` contains version-controlled definitions. `runs/` and `reviews/` contain
local generated artifacts and are Git-ignored. The shared eligibility policy (`stage2.v3`) excludes
the entire reserved directory with reason `evaluation_data`, independently of Git tracking.
The secure source reader rechecks this policy (`posix-secure-reader.v3`). Inventory's exact
`.git/info/exclude` control read remains available through a separate control-file entry point;
it cannot admit benchmark files or make VCS metadata searchable.

The initial dataset is `.buildlens-eval/datasets/buildlens-v1.draft.json`. Its companion evidence
file contains the exact proposed decoded source excerpts for human inspection. The authoring aid
is inside the same excluded directory and is never invoked by evaluation commands. These are
proposed ground truth: source targets may need to be narrowed or changed during human review.
There are six cases in each category and no invented reviewer identity, approval, or relevance.
The initial v1 corpus manifest remains intentionally unbound. The v3 draft is explicitly pinned
and validated; v2/v3 revision files retain reauthoring and source-span refinement provenance.

After the exclusion boundary exists, explicitly create a replacement snapshot through normal
onboarding, structural analysis, processing and embedding operations. Keep its compatible evidence
retained. Evaluation never performs these operations, downloads models, or repairs contamination.
Snapshots prepared with the old policy are rejected. The reader contract change also requires
compatible structural/processing evidence; do not relabel an old snapshot as new.

Use explicit paths under the registered project's reserved boundary:

```sh
buildlens eval validate \
  --dataset "$REPO/.buildlens-eval/datasets/buildlens-v1.draft.json" \
  --project "$PROJECT_ID" --snapshot "$SNAPSHOT_ID" \
  --corpus-manifest "$REPO/.buildlens-eval/datasets/corpus-v1.json"
```

`--corpus-manifest` explicitly exports inspected membership without modifying the dataset. An
unbound dataset still returns a nonzero validation status. Review the exported manifest, set the
new dataset revision's `corpus_manifest` to that object and `corpus_digest` to the reported digest,
and bind the proposed source targets to that frozen output. Changing source fingerprints or spans
requires an explicit reviewed definition revision, never fuzzy relocation. Repeat validation
without the export option to check the complete binding and local model prerequisites.

Evidence uses decoded Unicode character offsets and half-open spans. A target requires gap-free
union coverage within the same artifact fingerprint. Contradictory overlapping text, missing or
changed excerpts, ambiguous selectors and filtered targets invalidate binding. Each evidence
requirement is satisfied when every target in one accepted alternative is covered.

## Runs and reports

```sh
buildlens eval run \
  --dataset "$REPO/.buildlens-eval/datasets/reviewed-v1.json" \
  --project "$PROJECT_ID" --snapshot "$SNAPSHOT_ID" \
  --destination "$REPO/.buildlens-eval/runs/context-v1"

buildlens eval report --run "$REPO/.buildlens-eval/runs/context-v1"
```

The default run builds context only. `--case CASE_ID` may be repeated to select cases. The same
questions, filters and frozen authority feed all three variants. No variant substitution occurs.
Operational errors and invalid case bindings have separate outcomes. Draft runs remain provisional.
To invoke the existing configured Ask provider explicitly, use `eval run --answers` with a new
run destination and the existing Ask flag, provider key, optional SDK and local model configuration.
Expected answers and evidence are never supplied to production generation.

Each new run publishes a manifest, retained dataset/corpus copies, atomic case results, evidence
excerpts, summary Markdown, scores, answer-review packets and review/relevance templates. Existing
runs and case records are not overwritten. Interrupted runs remain inspectable and can be reported
again. The CLI releases workers, provider clients and the database engine on every exit. Live provider
outputs are not promised to be byte-identical; offline rescoring is deterministic.

The comparison ranking is the actual order before the eight-seed limit. Stage 8 candidates are
separate retrieval diagnostics. Expanded neighbours contribute to context/final-generation evidence
coverage only. The reranker admits at most 32 eligible candidates. Recall, precision, required-evidence
coverage and complete-evidence success use cutoffs 1, 5, 10 and 32; MRR uses rank 32. Precision uses K
as denominator, with empty result slots contributing zero, and is withheld if any returned position
through K is unjudged. Recall/MRR require a nonempty reviewed relevant set. Unsupported cases have
recall/MRR marked inapplicable. Requirements with no groups have no evidence-coverage denominator.
Errors never count as successful abstentions. Category means and paired differences show eligible
denominators; there is no aggregate RAG quality score or improvement threshold.

Authority changes stop the run, preserve completed diagnostics and invalidate aggregate comparisons.
Observation failure cannot turn a successful production call into a failure; dependent measurements
are withheld. Diagnostics never retain credentials, database URLs, environment dumps, exception
messages or rejected raw provider responses.

## Human review and revisions

Relevance templates contain portable selectors and blank labels/provenance. Inspect candidate
excerpts across all three variants, deduplicate selectors as needed, and record actual human judgments
in a new dataset revision. The evaluator never infers relevance from evidence targets. A judgment
records `relevant`, `reviewer`, `reviewed_at` and `rationale`. Approval is a separate human action:
set `review_status` to `approved` and record `approval.reviewer`, `reviewed_at`, `rationale`,
`definition_digest` and `judgments_digest` from validation. Approval hashes cover the concrete
normalized definitions and judgments; modifying them invalidates approval. This implementation
now records packet R1 only after the user's explicit approval; it never approves a dataset automatically.

Answer-review packets include the question, expected facets, forbidden claims, proposed evidence,
validated answer/statements, cited excerpts and final supplied evidence from the same Ask invocation.
Copy templates into `.buildlens-eval/reviews/REVIEW_SET/` and record:

- `facets`: zero-based `index`, judgment (`met`, `partial`, `missing`, `incorrect`) and rationale.
- `statements`: zero-based `index`, support judgment (`supported`, `partially_supported`, `unsupported`,
  `contradicted`), `substantive`, `sufficient_cited_support`, `support_kind` (`appropriate` or
  `inappropriate` for direct/inference), and rationale.
- `links`: `statement_index`, `evidence_id`, the same support judgment, and rationale.
- Overall `correctness` (`correct`, `partial`, `incorrect`), `abstention` (`appropriate`, `inappropriate`,
  `not_applicable`), reviewer, date, rationale, rubric version and unchanged result digest.

Partial review is allowed; coverage and individual judgments are retained. Semantic citation precision
counts fully supported links among reviewed links. Completeness counts reviewed substantive statements
with sufficient cited support. These human measurements are separate from structural citation membership,
provenance and coordinates. A structurally valid citation can be semantically unsupported.

```sh
buildlens eval report --run "$REPO/.buildlens-eval/runs/context-v1" \
  --dataset "$REPO/.buildlens-eval/datasets/reviewed-v2.json"
buildlens eval report --run "$REPO/.buildlens-eval/runs/context-v2" \
  --dataset "$REPO/.buildlens-eval/datasets/reviewed-v2.json"
buildlens eval compare --left "$REPO/.buildlens-eval/runs/context-v1" \
  --right "$REPO/.buildlens-eval/runs/context-v2"
```

Rescore both sides after judgments change. Case definitions, filters, selection, corpus authority,
rubric and metric versions must remain compatible; intended system/configuration differences are
shown separately. For answer runs, pass `--reviews "$REPO/.buildlens-eval/reviews/REVIEW_SET"` to
`eval report`. Scoring revisions and human reviews do not mutate the retained production observations.

Generation settings record requested values, explicit transmitted values and provider-confirmed
values separately. Gemini currently requests `thinking_level="medium"`, transmits no thinking
setting, and has an unknown effective value (`provider-default-unspecified`). No SDK argument is
added to resolve this discrepancy. Allowlisted settings explicitly reported in a provider response
are retained separately with response-field provenance; SDK/model/documentation defaults are not
used to infer effective values.

Reports store compact score references (`stage11.score-reference.v1`); native source evidence
remains in immutable per-case artifacts. This fixed the real-corpus 38 MiB score file exceeding
the 32 MiB input guard, reducing it to 2.7 MiB. Actual offline self-comparison passed with zero
paired differences, and repeated reporting is byte-identical. A self-comparison verifies mechanics,
not a second live model run.

## Attaching independent verification evidence

Run execution does not itself run the test suite. To display actual independent verification
alongside benchmark states, explicitly attach its versioned, digested record:

```sh
buildlens eval report --run "$REPO/.buildlens-eval/runs/retrieval-20261002" \
  --verification "$REPO/.buildlens-eval/runs/preparation-20261002/verification.json"
```

The record has version `stage11.verification.v1`, `infrastructure_status` (`passed`, `partial`,
or `failed`), concrete test evidence, and `record_digest` equal to canonical SHA-256 of the record
without that digest field. The report validates it and retains its relative path/digest/state.
This is a recorded independent test result; attaching it never changes production observations,
human approval, retrieval scores, provider execution, or semantic review. Saved run manifests
retain their original execution-time states. Repeating the same offline report is deterministic.
No Stage 12 retrieval or architecture change is part of this closure.


## Stage 12 laboratory scoring

`buildlens lab bench` reuses Stage 11 binding (`preflight`) and `score_result` unchanged, but labels
every metric by tier: **approved** (R1 judgments; only pipelines that return BuildLens chunks),
**span** (approved required-evidence span coverage for any anchored item, always read with
delivered context KiB and coverage per KiB), **derived** (`derived.r1-span-projection.v1`, τ = 0.5,
not human-approved) and **constructed** (synthetic LD-2 spans, not human-approved). Reports add a
**matched subset** of answerable cases every pipeline could fully see. Per-case records are
digested in the run manifest; `buildlens lab report` recomputes scores offline and is
byte-deterministic. Latency is pipeline wall time per query (no corpus verification), p50/p95.
`buildlens lab parity` re-runs the native pipeline against a retained Stage 11 run. See
[FRAMEWORK_LAB.md](FRAMEWORK_LAB.md).
