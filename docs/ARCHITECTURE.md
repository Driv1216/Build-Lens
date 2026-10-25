# BuildLens architecture

BuildLens is a modular monolith through **Phase 2, Stage 12 — LangChain / LlamaIndex Laboratory**.
React/Vite calls FastAPI; PostgreSQL is authoritative. Snapshots, structural analysis, and
processing are explicit user operations; embedding preparation is a separate persistent operation.
No repository modules are imported or executed.

## Boundaries

- `domain` owns immutable observations, stable identities, producer contracts, eligibility,
  shared decoding, source coordinates, and content screening.
- `application` owns use cases and typed store, reader, inspector, and analyzer ports. It has no
  SQLAlchemy, FastAPI, or infrastructure imports.
- `infrastructure` implements PostgreSQL stores, inventory, bounded Git invocation, secure POSIX
  traversal, supervised execution of trusted BuildLens readers/parsers/processors, and an optional
  offline Sentence Transformers worker.
- `api` wires concrete dependencies, calls public service methods, and converts domain/query DTOs
  to validated responses. `core` supplies validated environment configuration; `cli` supplies
  operational checks and retained-storage diagnostics.

Operations read and produce observations outside publication transactions. Trusted reusable
workers have parent-enforced deadlines and byte progress accounting; timeout/crash terminates
and replaces the worker. They are synchronous operation helpers, not a background job platform.

## Persistence and publication

Projects own snapshots (`project_versions`) and artifact path identities. Snapshot artifacts
record relative locators, inventory/ignore outcomes, root identity, and exact-byte fingerprints.
Structural runs own per-artifact outcomes, lexical symbols, and unresolved imports. Processing
runs own artifact outcomes and exact source chunks. Stable symbol identities belong to a
project's artifact identity; observation UUIDs belong to one retained run.

Publication locks the snapshot row and checks captured authority. Structural persistence flushes
analysis and artifact outcomes before identities and parent-first symbols, then imports. Processing
persists its run before outcomes and chunks. Active pointers change only inside a successful
transaction. Failed attempts remain separate from usable evidence. A competing attempt is retained
as superseded and cannot overwrite changed authority. Returned observations contain persisted
revisions and assigned identities.

Composite foreign keys enforce ownership across snapshots, identities, dependencies, and
chunk-symbol associations. Forward migrations `0006` and `0007` quarantine old derived runs and
clear usable active pointers without deleting history. Clean legacy constraints are validated;
legacy violations remain under `NOT VALID` constraints that still enforce new writes. The CLI
reports pending validation. Stage 4 downgrade refuses multiple retained runs before destructive
changes rather than deleting history to fit its earlier model.

## Incremental evidence

Reuse requires compatible versioned producer contracts, verified snapshot bytes, current
eligibility, and the same artifact identity. Contracts cover fingerprinting, reader policy,
parser/runtime, decoding, chunk boundaries, and screening. Python declarations reuse identical
observations' stable identities. Changed source continues only unique lexical declaration slots;
ambiguous slots receive new identities. Compatible current-snapshot evidence precedes the
predecessor baseline. Every reused observation is rebound to its current artifact, snapshot, and
run, with predecessor observation IDs retained solely as provenance.

Python chunks depend on equivalent required artifact structure, including stable declaration
identities and source coordinates. Reanalysis with equivalent evidence leaves processing fresh;
changed required evidence or incompatible contracts makes it stale. A run UUID change alone is
insufficient to make content stale. This is snapshot-qualified freshness, never a claim about
current filesystem bytes.

Chunk keys combine artifact identity, producer contract, distinct logical-unit anchor, part index,
and content hash. Absolute line offsets are excluded. Python residual anchors include neighboring
declaration identities and occurrence numbers. Markdown anchors distinguish repeated headings;
JSON paths retain member order and duplicate-member occurrences. TOML uses standard-library
validation and a quote-aware boundary scan. YAML uses bounded safe node composition without
constructing application objects. Unsupported units receive fallback diagnostics.

Shared decoding respects Python encoding declarations and BOMs. LF, CRLF, CR, and Unicode
coordinates refer to exact decoded source slices, without newline normalization. Generic splitting
tries paragraph, line, whitespace, then character boundaries. Empty sources have no empty chunks.

## Persistent vector evidence

Stage 7 reads only current content through Stage 5's shared compatibility, quarantine, and
structural-dependency freshness rule. Preparation streams bounded pages in locator/ordinal/UUID
order and verifies every exact UTF-8 slice. The logical key covers the full embedding contract,
stable chunk key, and input hash. Compatible retained observations are copied into new immutable
per-run rows with new observation UUIDs and explicit reuse provenance; only misses reach inference.

`embedding_runs` records processing authority, complete contracts, ordered membership, revisions,
coverage, diagnostics, and publication authority. `chunk_embeddings` binds each variable-dimension
pgvector value to its owning run, processing/artifact/chunk evidence, logical identity, and optional
reuse source. Composite foreign keys and checks enforce ownership, hashes, dimensions, unit norm,
and reuse provenance. Publication locks the snapshot and atomically inserts the run and all rows
before moving `active_embedding_run_id`. Failed and superseded attempts cannot replace active
evidence or supply reuse.

The pinned CPU MiniLM adapter embeds `chunk.content` verbatim. Long documents are tokenized without
truncation, split into consecutive token-ID payload windows, encoded in batches, normalized per
window, averaged by non-special-token count, and normalized again. Queries are trimmed only at the
edges and rejected rather than truncated when empty, over 4,096 UTF-8 bytes, or over the model
payload-token limit. PostgreSQL pgvector cosine distance implements exact search, with stable chunk
key and chunk ID as deterministic tie-breakers. Locator-prefix, language, and kind filters are
applied through authoritative joins before ranking and limit. Only ranked chunk IDs are hydrated
with source. Search holds a shared snapshot lock, uses deadline-bounded transaction-local
statement/lock timeouts, and rechecks both authorities before returning.

## Lexical and hybrid retrieval

The common snapshot-scoped `/search` contract requires `exact`, `lexical`, `semantic`, or
`hybrid`. A retrieval application service owns mode orchestration and exact-arithmetic RRF; a typed
retrieval session holds one shared snapshot lock while PostgreSQL selects candidate metadata and
hydrates final IDs. Every branch revalidates current processing authority, structural dependencies,
ownership, quarantine/supersession, filters, and source hashes before returning.

Exact matching bypasses normalization and compares declaration name, qualified name, and full
artifact-processing locator using literal case-sensitive `C` collation. Lexical matching applies
`buildlens_lexical_normalize_v1` identically to fields and plain queries, then uses explicit
`pg_catalog.simple` vectors. Content, symbol qualified name, path, and joined heading/JSON metadata
are independent groups: every query term must match within one group. The maximum matching group
rank is reported; exact matches precede lexical scores without receiving a fabricated score.

Migration `0009` adds GIN expression indexes for content, symbol qualified names, and locators.
Heading/JSON metadata stays scoped and unindexed because its array conversion is not immutable.
Each component fetches at most 100 distinct chunk candidates plus one truncation probe. Hybrid
unions the lexical and semantic lists by chunk UUID and computes `1 / (60 + rank)` per present
component. Exact matches sort before fused scores. Candidate queries do not project source bodies
or vectors; only the final at-most-50 IDs are hydrated in one batch.

## Bounded context construction

Stage 9 is a query-time application layer above the shared Stage 8 ranking path. Public search stays
capped at 50 hydrated hits; the internal context entry point uses the same exact, lexical, semantic,
and hybrid implementation and returns at most 100. A separate `ContextService`, `Reranker`,
`RerankerRuntime`, evidence store, and exact `ContextSizer` keep generation, ORM objects, and model
library objects outside domain results.

Candidates are authority-validated, identity-deduplicated, and checked against the current
zero-overlap producer contract before inference. Learned mode admits at most 32 candidates to the
pinned local cross-encoder and ranks finite raw logits descending with retrieval rank, stable key,
and UUID tie-breakers. Retrieval-order mode bypasses the worker entirely. Up to eight selected seeds
may discover only part indexes immediately before or after the same artifact-processing logical unit.
Expansion is one hop, preserves all parent edges, never crosses a missing part, and reapplies the
original filters.

Packing is rank-first, whole-item, and skip-if-does-not-fit: seeds precede neighbours, there is no
truncation or backfill beyond the selected seed set. The current allowance is 16 observations and
32,000 exact UTF-8 bytes for a versioned canonical JSON projection containing the query and complete
citation-bearing evidence. Diagnostic/ranking traces are excluded from that projection and separately
response-bounded. Ask separately counts its full provider request before generation.

Expansion and final hydration use short shared-lock sessions around PostgreSQL reads. No database
lock is held during model loading or inference. Final IDs are rehydrated and compared with the
observations used for construction before return. Context packages, raw reranker scores, and decisions
are not persisted.

The dedicated reranker process loads only the explicitly acquired pinned safetensors/tokenizer from a
verified cache with offline and telemetry-disabled settings. It uses CPU float32, one intra-op and one
inter-op thread, bounded non-overlapping passage windows, batches of eight, an absolute request
deadline, sampled RSS supervision, and terminate/kill/reap replacement after failure. The embedding
and reranking workers can be resident together; each 2 GiB threshold is per-worker, not a total
application memory ceiling.

## Grounded Ask mode

Stage 10 adds a single-turn `AskService` above `ContextService`. The context package is built once
using the caller's existing retrieval and ranking policies. Evidence is assigned request-local `E1`,
`E2`, ... IDs. `ContextService.verify_result` rechecks publication authority and exact stored chunks
before token counting, before generation, and after generation. PostgreSQL sessions do not span
provider calls. Empty evidence produces a deterministic `insufficient_evidence` result without
cloud traffic.

The selected Gemini adapter uses the official Google GenAI Python SDK with `gemini-3.8-flash`,
Gemini `count_tokens`, structured JSON output, and the same server-owned prompt and validation.
The identical contents supplied to counting and generation include the instructions, question,
evidence, and schema text. The Gemini Developer API token counter cannot accept generation config;
the output schema is also supplied as a structured-output generation setting. The retained OpenAI
Responses adapter is selected with `BUILDLENS_GENERATION_PROVIDER=openai` and uses `gpt-6-luna`,
strict JSON schema, server-owned prompt
instructions, no tools, no history, no streaming, zero SDK retries, `store=false`, and disabled
truncation. The provider counts the full model-visible request including the output schema. The
product input ceiling is 30,720 tokens after reserving 2,048 output tokens. If the Stage 9 package
does not fit, ContextService repacks only its existing whole items, retaining seed support and
original evidence IDs. The response is accepted only after strict JSON and citation-membership
validation. Source locations and excerpts come from authoritative records, never model text.

Ask is disabled by default. `BUILDLENS_GENERATION_PROVIDER` selects exactly one provider with no
fallback. The backend alone holds the selected provider's API key. `GET /generation` reports the
selected provider and model and inspects local configuration without contacting either provider.
No Ask database tables, stored conversations, prompt logs,
or answer caches are added. The UI invalidates pending results on question, policy, filter, snapshot,
or evidence-operation changes, and rechecks authority before rendering a completed answer. Browser
cancellation does not guarantee cancellation of provider processing or billing.

Schema and citation validation establish structure and membership; they do not prove statement
faithfulness. The synthetic Gemini real-provider and local reranker closure gates passed; their
execution evidence and limits are recorded in CURRENT_STATE.md. The OpenAI paid gate remains unrun.

## Security and resources

The API is a local, unauthenticated interface bound to loopback. Host/origin restrictions and JSON
mutation guards protect browser entry points; they are not an authentication system. PostgreSQL
Compose exposure is loopback-only and credentials must come from environment configuration.

Inventory, ignore controls, fingerprints, and processing use descriptor-relative no-follow
traversal and carry root device/inode identity. Final files are opened nonblocking before their
regular-file check. Symlinks, mount crossings, missing/unreadable files, observable substitutions,
and read failures produce source-free diagnostic codes. A parent deadline bounds potentially
blocking filesystem operations, processing, and worker communication.

Git invocation disables system/global configuration, executable fsmonitor and ordinary hooks,
configured content filters, and optional index writes. Duration and output are bounded; timeout
terminates its process group. Ignore controls outside the registered root fail closed.

Processing defaults are 1 MiB input per artifact, 64 MiB consumed bytes per operation, 5,000
eligible candidates, 4,096 characters per chunk, 1,024 chunks per artifact, and 20,000 chunks per
run. Excluded entries remain visible without consuming eligible-candidate slots. Failed reads count
actual consumed bytes. An artifact exceeding its chunk budget loses its whole partial output;
already completed artifacts can form a partial run. Positive, consistent configuration is required.

Embedding defaults are 20,000 chunks, 64 MiB source input, 128-row content pages, 16 inference
windows per model batch, 32 windows per chunk, 100,000 windows per preparation, and 64 MiB retained
vectors plus serialized provenance. One lock admits either preparation or search. Model load,
inference inactivity, preparation, and search deadlines are supervised. Worker RSS is sampled
against a configurable 2 GiB threshold; this is not an instantaneous operating-system memory cap.

Both extraction and chunking apply the same high-confidence secret screen before persistence.
Matching artifacts are rejected entirely; matching text is not logged or included in diagnostics.
The screen is deliberately narrow, not comprehensive secret detection. Permitted expression
fragments and chunks are sensitive source content stored in PostgreSQL.

## Queries and browser state

Status and history use summary projections; paginated lists use database counts and batched
queries. Chunk lists omit content, and symbol lists omit expression text. Only scoped detail
endpoints retrieve source. Default content access requires compatible active evidence (fresh for
processing). Explicit history can expose compatible retained content with run/freshness metadata.
Quarantined content is inaccessible through both normal and historical endpoints.

Missing parents/explicit run IDs return 404; unavailable current evidence and conflicts return 409;
infrastructure failures return sanitized 503 responses. The API never claims persistence after a
failed database operation. Responses carry structured error codes and relevant locators/provenance.

Frontend state is scoped to project, snapshot, evidence revision, mode, query, and filters. Scope
changes clear selected content; requests are aborted or late responses ignored. Structural and processing refreshes are
coordinated. Outcomes, symbols, imports, changes, chunks, and run history are paginated with server
totals. Semantic results are additionally scoped to the current run, query, filters, and newer searches.
Latest attempts, usable evidence, stale evidence, and quarantine have distinct displays.

## Retention and scope

There is no automatic history deletion. Operational diagnostics expose retained row counts,
stored chunk bytes, expression storage, and pending constraint validation. Real upgrades require a
verified backup; repair validation uses generated disposable databases and actual migrations.
Snapshots are observations, not content archives. If old bytes no longer exist and no compatible
evidence can be reused, create a new snapshot; never relabel current bytes as historical input.

Stage 10 deliberately adds no ANN index, fuzzy redundancy,
query rewriting, GraphRAG, agents, import resolution, call graphs, authentication, automatic
scanning, automatic regeneration, or durable context traces. See `CURRENT_STATE.md` for actual
verification evidence and remaining limitations.


## Stage 11 native evaluation

The CLI evaluation adapter composes the existing `RetrievalService`, `ContextService`, and
`AskService` through `infrastructure.runtime`. HTTP dependencies use the same constructors and
worker lifecycle. Application evaluation, binding and metrics modules depend on domain contracts
and ports, without FastAPI or SQLAlchemy. PostgreSQL evidence inspection and atomic local JSON/
Markdown persistence remain infrastructure adapters. No routes, tables, migrations or frontend
features were added.

The shared eligibility policy excludes `.buildlens-eval` with reason `evaluation_data`; the policy
is versioned `stage2.v3` and source reader `posix-secure-reader.v3`. Inventory pruning, processing
and direct source reads enforce the same rule. The inventory reader has an explicit exact-path
control read for `.git/info/exclude` that preserves existing ignore evaluation while leaving VCS
metadata and benchmark files excluded from source access.

`ContextResult.ranking_observation` contains bounded identities, coordinates, hashes, retrieval
component ranks/scores, exclusions, actual pre-seed selection order and reranker windows, without
vectors or duplicated source bodies. It is constructed from existing decisions and is excluded
from equality, repr, HTTP serialization, diagnostic admission, sizing, prompt projection, token
counts and request digests. Production never reads the record. A one-way, invocation-local
ContextVar collector retains completed stages for evaluation, including failures. It follows Ask's
`asyncio.to_thread`; collectors perform no IO or inference, and collector exceptions are swallowed.
An unavailable observation withholds dependent metrics without changing production success.

Stage 8 retrieval, pre-seed ranking, packed Stage 9 context and final Ask generation evidence are
separate observations. Only pre-seed ranking drives the three-way ranking comparison. Expanded
neighbours contribute to delivery coverage. Ground truth binds exact decoded Unicode half-open
source spans, fingerprinted artifacts and portable reviewed chunk selectors to retained frozen
processing output. Gap-free union coverage is permitted within the same bound artifact; filtering,
changes and ambiguity invalidate bindings rather than becoming retrieval misses. Corpus preflight
checks native ownership, freshness, compatibility, complete membership and exclusion provenance
through existing PostgreSQL gates. Mid-run authority changes invalidate comparison.

Local runs retain versioned dataset/corpus copies, implementation and lockfile digests (including
uncommitted/untracked source), explicit settings, execution order, sanitized errors, native evidence
and atomic per-case artifacts. Offline reports validate retained digests and support rescoring both
sides with a reviewed judgment revision. Human facet, statement and citation-link reviews are tied
to result digests; structural checks never imply semantic entailment. Requested/sent/unknown-effective
provider metadata is observed from existing adapter request construction; provider-reported fields
are recorded separately, without changing arguments or assuming defaults.

**Stage 11 is closed with an inconclusive/partial provider benchmark; answer quality is not
passed.** Infrastructure verification **passed** the migrated PostgreSQL integration,
full backend/static/frontend regression checks and pinned real local-model checks. A compatible
retained corpus contains 2,099 chunks from 157 searchable artifacts; its partial processing and
unsupported-language limits are explicit. All 30 reviewed cases bind and the real three-way retrieval
run completed 90/90 calls without errors or authority changes. At packed context, complete approved
evidence was delivered for 5/24 semantic, 5/24 hybrid and 7/24 reranked answerable cases. Lexical
returned no candidates for all 30 full-sentence queries, so semantic/hybrid orders were identical.
Source-span coverage and binary relevance are separate measurements, neither proving semantic
answer quality. Dataset revision `4-r1-reviewed` records the user's explicit R1 acceptance of
30 definitions and 798 judgments (98 relevant, 700 irrelevant). Offline rescoring of unchanged
native observations gives MRR@32 0.4560 / 0.4560 / 0.5972, Recall@5 20.64% / 20.64% / 42.94%,
and Precision@5 15.00% / 15.00% / 33.33% over the 24 answerable cases.

Human ground-truth approval is **approved** for the reported cutoffs 1/5/10/32 and MRR@32:
every returned answerable cutoff position is judged, and 30 known relevant sources outside rank
32 remain in Recall denominators. The reference set is curated, not exhaustive corpus relevance.
Deeper unjudged positions keep native whole-list provisional flags. The original draft and
proposal packet remain preserved; approval provenance and definition/judgment digests are separate
revision artifacts. Repeated rescoring is byte-identical, all 90 case files are unchanged, and
offline self-comparison passed. The human then explicitly authorized the exact R1 sharing scope
and 90 existing production Ask evaluations. The retained Gemini corpus run completed 90/90
invocations with stable authority and successful counts (270), but provider verification is
**inconclusive/partial**: 2 valid hybrid abstentions, 69 rate-limit failures and 19 generation-unavailable
failures; zero answered responses. No retries or fallback were used. All generation evidence
stayed within the authorized frozen packages, and observed retrieval/ranking matched the earlier
baseline. Failed-generation usage and underlying unavailable causes remain unknown.

The human explicitly **approved A1**, finalizing the six task-correctness, facet and abstention
judgments for both valid responses. Each is an appropriate safe abstention against supplied
incomplete evidence, incorrect for completing its answerable task, with the sole required
facet missing. All eligible responses are reviewed (2/2); 88 operational failures have no
answer and are ineligible. Zero statements/citations leave grounding, citation precision and
completeness, and direct/inference support N/A with zero denominators. No LLM judge or invented
reviewer identity was used. A complete three-way answer-quality comparison is unavailable;
answer quality is **not passed**.

The compact expected-answer summary was corrected to the already approved frozen locator
`backend/src/buildlens/domain/source.py` for `processing_contract()`. Definitions, bindings
and semantic judgments were unchanged. Actual approval and reviewed files are separate from
sealed as-presented proposals and original execution-time manifests. Native rescoring validates
the reviews against exact result digests; final closure records reconcile its generic
`partial: 2 result reviews` label with 2/2 eligible coverage and 88 ineligible failures.
Three small finalization tests and native review/zero-denominator/score-integrity checks passed;
repeat scoring was byte-identical, and all 180 case files plus manifests, approved dataset,
authorized scope and retrieval scores stayed unchanged. No new provider request or full gate
was run. Stage 11 closure records retain partial/inconclusive provider verification separately
from passed infrastructure and approved retrieval/human-review states. No Stage 12 work or
production retrieval/reranking policy change was made. See [evaluation workflow and actual
measurements](EVALUATION.md) and `CURRENT_STATE.md` for retained identities, results and limitations.


## Stage 12 framework laboratory

The laboratory is a separate bounded context; no production table, migration or Stage 5–11
behaviour changed. `domain/lab.py` holds framework-free contracts (strict experiment schemas,
canonical digests, pipeline catalogue, LabItem/LabStep, shared packing, anchoring);
`application/lab.py` and `application/lab_evaluation.py` orchestrate builds, queries and tiered
scoring through ports. **Framework code runs only in supervised worker processes**
(`infrastructure/lab/workers/{langchain,llamaindex}`, plus an isolated `langchain-postgres`
environment under `backend/lab_envs/`). An architecture test asserts no module outside the worker
packages imports a framework, and the API process never loads one.

```text
API / CLI process ── LabService ──► Corpus Gateway (authoritative chunks, verified vectors,
      │                               secure-reader artifacts with fingerprints + screen)
      │                         └──► NativePipeline (unchanged ContextService + observation hooks)
      │  stage12.worker.v1 (bounded frames, deadlines, callback allow-list + budget)
      ├──► LangChain worker   ─┐  callbacks: embed, rerank, native candidates, neighbours,
      ├──► LlamaIndex worker  ─┼─ read-only tools ──► CapabilityBridge (BuildLens locks,
      └──► langchain-postgres ─┘                     contracts, budgets, authority)
```

Workers receive a scrubbed environment (no production database URL or provider key; only the
database-scoped workers that serve LS-1/2/3 receive the separate lab database URL), private
HOME/TMPDIR and an audit-hook guard (network, process creation, writes, renames/links on both
paths, protected reads and listings, absolute-path-only opens because the `open` event omits
`dir_fd`, no symlink creation). Any failed request kills and replaces the worker. This is process
isolation plus defense in depth, **not** an OS sandbox: C extensions and ctypes bypass audit
hooks. The parent re-verifies every returned item against authoritative evidence, and accepts
precomputed-vector reuse only when zero document texts were embedded and the framework's own
persisted vectors match the stored digests. pgvector experiments use a separate server and
least-privilege role. Lab state lives under the already-excluded `.buildlens-eval/lab/`, so the
`stage2.v3` policy and the frozen Stage 11 corpus binding are unchanged. Design and measured
findings: [FRAMEWORK_LAB.md](FRAMEWORK_LAB.md).

## Stage 13 Milestone A — multi-language structure

**Status: Milestone A implemented, 2026-10-25; Milestones B–D not started.** Design, deviations
and verification: [SEMANTIC_MODEL.md](SEMANTIC_MODEL.md).

**Producers**
- Structural runs keep the frozen Python declaration producer. The run-level `producer_contract` still means *the Python declaration producer only*.
- Additive per-artifact passes are recorded in `artifact_extractions`, each with its own contract digest:
  - `python-facts.v1`, a second `ast` + `tokenize` pass beside the unchanged parser
  - `typescript-declarations.v1` and `javascript-declarations.v1`, parse-only `tree-sitter` in the same supervised worker, with a canary handshake and fail-closed grammar checks
- `domain/extractors.py` is the language registry.

**Authority**
- A parsed non-Python artifact must reference its declaration extraction (DB CHECK).
- It is current only while that contract is current (M4). Python evidence in the same run is unaffected.

**Processing**
- Processing admits `.ts/.tsx/.js/.jsx` (not `.min.js`) behind the same structural prerequisite.
- Chunks are cut from offset-precise declaration regions.
- `artifact_processings.fingerprint_scheme` tags TS/JS dependencies (`ts-declarations-dependency.v1`); `NULL` remains the legacy Python fingerprint.
- A TS extractor contract change makes processing runs that contain TS dependencies stale, because Stage 5 freshness is run-level. Runs without TS dependencies are unaffected.

**Migration and limits**
- Migration `0010` is additive, and its downgrade refuses while Stage 13 evidence exists.
- Nothing is executed or resolved: no Node, no tsconfig, no module resolution in production.
- Import resolution, the semantic catalog and AI interpretations belong to later milestones.

