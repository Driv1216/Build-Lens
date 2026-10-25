# BuildLens

BuildLens is a local engineering-intelligence application, implemented through **Phase 2,
Stage 12 — LangChain / LlamaIndex Laboratory**. It uses a modular Python backend, React/Vite UI,
and PostgreSQL. **Stage 11 is closed with an inconclusive/partial real-provider benchmark;
answer quality is not passed. Stage 12 adds an offline framework laboratory; no framework was
adopted into production.** Operations start only when you
explicitly request them.

The foundation repair preserves projects, snapshots, and historical records. Pre-repair
structural and processing runs are quarantined: their content remains stored, but normal and
historical content endpoints cannot expose it. Regeneration is explicit.

Embedding runs and immutable per-run vector observations are persisted in PostgreSQL/pgvector.
Exact, PostgreSQL full-text lexical, exact-cosine semantic, and deterministic RRF hybrid retrieval
operate over current evidence. Semantic/hybrid modes are explicitly prepared; exact/lexical modes
need no ML packages or model cache. Stage 9 turns at most 100 current retrieval observations into
a structured, exact-byte-bounded evidence package. Stage 10 adds optional single-turn answers with
validated statement citations. Interactive Ask data stays in memory; explicitly invoked Stage 11
evaluation runs retain local observations and review artifacts. Stage 11 adds a CLI evaluation
layer over the production services, with no new HTTP routes, frontend features, database tables,
or migrations. Retained history endpoints are metadata-only. No ANN index, import resolution,
call graph, agents, authentication, background job platform, or automatic history deletion are
included. See
[current verification](CURRENT_STATE.md) and [architecture](docs/ARCHITECTURE.md).

## Local setup

Install Python 3.11 or 3.14, uv 0.12.3, Node.js 22, and Docker. From the repository root:

```sh
cp .env.example .env
```

Choose a local `POSTGRES_PASSWORD` and set the corresponding URL-encoded password in
`BUILDLENS_DATABASE_URL`. The application requires a PostgreSQL URL; it has no credential
fallback. Compose binds PostgreSQL to `127.0.0.1:5432`.

**Changing Compose variables does not rotate the password of an existing PostgreSQL volume.**
Rotate an existing role's password using PostgreSQL administration, then update the URL.

```sh
docker compose up -d db
cd backend
uv sync --frozen --extra dev --no-editable
uv run --no-sync alembic upgrade head
PYTHONPATH=src uv run --no-sync uvicorn buildlens.api.main:app --host 127.0.0.1 --port 8000
```

The base install can inspect persisted status/history and run exact/lexical retrieval without
importing ML libraries. To prepare new vectors or use semantic/hybrid retrieval, install the frozen
optional extra and explicitly acquire the pinned,
checksum-recorded model. API startup, preparation, and search never download it:

```sh
cd backend
uv sync --frozen --extra dev --extra embeddings --no-editable
uv run --no-sync buildlens embeddings download-model
```

Learned context ranking is a separate optional capability. Its Python 3.11 runtime uses a pinned
cross-encoder acquired explicitly; startup and requests never download weights. Retrieval-order
context remains available from the base install:

```sh
cd backend
uv sync --frozen --extra dev --extra embeddings --extra reranking --no-editable
uv run --no-sync buildlens reranking download-model
```

Ask requires the optional generation SDK and a server-side API key. Set
`BUILDLENS_ASK_ENABLED=true`, `BUILDLENS_GENERATION_PROVIDER=gemini`, and
`BUILDLENS_GEMINI_API_KEY` in the backend environment. The retained OpenAI adapter is selected with
`BUILDLENS_GENERATION_PROVIDER=openai` and `BUILDLENS_OPENAI_API_KEY`. Gemini uses
`gemini-3.8-flash`; OpenAI uses `gpt-6-luna`. There is no provider fallback. The default
Ask policy uses hybrid retrieval and the local cross-encoder, so prepare embeddings and acquire
the reranker as shown above. Exact or lexical retrieval with retrieval-order ranking is an explicit
alternative in the Ask panel. Both token counting and generation send the question and selected
evidence to the selected provider. OpenAI uses `store=false`; this does not imply zero provider retention.

```sh
cd backend
uv sync --frozen --extra dev --extra generation --extra embeddings --extra reranking --no-editable
```

The paid real-provider gate uses synthetic source and requires a disposable PostgreSQL admin URL,
the Gemini key, both locally acquired model caches, and explicit opt-in:

```sh
cd backend
BUILDLENS_REAL_PROVIDER_TEST=1 PYTHONPATH=src uv run --no-sync pytest -q \
  -m real_provider tests/real_provider
```

Set `BUILDLENS_TEST_ADMIN_URL` and the other backend variables in the environment before running it.
The gate is excluded from ordinary tests and CI.

For an existing installation, follow the backup procedure below before upgrading. In another
terminal:

```sh
cd frontend
npm ci
npm run dev -- --host 127.0.0.1
```

Open `http://127.0.0.1:5173`. The default API URL is `http://127.0.0.1:8000/api/v1`.
The UI registers a path, takes a snapshot, analyses Python, processes artifacts, explicitly
prepares embeddings when needed, and searches current chunks in four modes. Each evidence panel identifies the latest
attempt separately from usable, stale, historical, or quarantined evidence.

`PYTHONPATH=src` keeps development execution pointed at current source while the frozen
installation also verifies the package and existing `buildlens` CLI entry point.

## API

All paths below are under `/api/v1`. Mutations use JSON, including `{}` for explicit operations.

| Scope | Endpoints |
|---|---|
| Health | `GET /health` |
| Projects | `GET, POST /projects`; `GET /projects/{project_id}` |
| Snapshots | `POST /projects/{project_id}/snapshots`; `GET .../snapshots/latest`; `GET .../snapshots/{snapshot_id}` |
| Inventory | `GET .../{snapshot_id}/artifacts`; `GET .../{snapshot_id}/changes` |
| Structure | `POST .../{snapshot_id}/structure`; `GET .../structure`; `GET .../structure/status` |
| Structural evidence | `GET .../structure/artifacts`, `/symbols`, `/symbols/{symbol_id}`, `/imports` |
| Structural history | `GET .../structure/runs`; `GET .../structure/runs/{run_id}` |
| Processing | `POST .../{snapshot_id}/processing`; `GET .../processing` |
| Processing evidence | `GET .../processing/artifacts`, `/chunks`, `/chunks/{chunk_id}` |
| Processing history | `GET .../processing/runs`; `GET .../processing/runs/{run_id}` |
| Embeddings | `GET, POST .../{snapshot_id}/embeddings` |
| Embedding history | `GET .../{snapshot_id}/embeddings/runs`; `GET .../embeddings/runs/{run_id}`; `GET .../embeddings/runs/{run_id}/rows` |
| Retrieval | `POST .../{snapshot_id}/search` with explicit `exact`, `lexical`, `semantic`, or `hybrid` mode |
| Context | `POST .../{snapshot_id}/context` with explicit retrieval and ranking policies |
| Ask | `POST .../{snapshot_id}/ask` with a question, optional retrieval/ranking policies, and filters |
| Generation status | `GET /generation` (configuration inspection; no paid call) |
| Reranker status | `GET /reranker` (cache/dependency inspection only; does not load the model) |
| Semantic compatibility | `POST .../{snapshot_id}/semantic-search` |

Pages accept `limit` and `offset`. Evidence endpoints accept an explicit `run_id` for compatible
retained history. Default chunk access requires fresh, compatible active evidence. Chunk lists
contain metadata; only chunk detail retrieves source content. Responses include structured error
codes: missing parents or run IDs return 404, unavailable current evidence/conflicts return 409,
and infrastructure failures return sanitized 503 responses.

The common search endpoint has no pagination and accepts `top_k` from 1–50. Candidate lists are
fixed at 100 and report bounded counts and truncation. For example:

```sh
curl -sS -X POST "$API/projects/$PROJECT/snapshots/$SNAPSHOT/search" \
  -H 'Content-Type: application/json' \
  -d '{"mode":"exact","query":"Client.get_user","top_k":10}'
curl -sS -X POST "$API/projects/$PROJECT/snapshots/$SNAPSHOT/search" \
  -H 'Content-Type: application/json' \
  -d '{"mode":"lexical","query":"HTTP server","filters":{"language":"python"}}'
curl -sS -X POST "$API/projects/$PROJECT/snapshots/$SNAPSHOT/search" \
  -H 'Content-Type: application/json' \
  -d '{"mode":"semantic","query":"where are users loaded?","top_k":5}'
curl -sS -X POST "$API/projects/$PROJECT/snapshots/$SNAPSHOT/search" \
  -H 'Content-Type: application/json' \
  -d '{"mode":"hybrid","query":"load user","top_k":10}'
curl -sS -X POST "$API/projects/$PROJECT/snapshots/$SNAPSHOT/context" \
  -H 'Content-Type: application/json' \
  -d '{"retrieval_mode":"lexical","ranking_policy":"retrieval_order","query":"load user"}'
```

Context policy is fixed and returned in every response: at most 100 candidates, 32 learned-reranked
candidates, eight seeds, one adjacent part in either direction, 16 final observations, and 32,000
UTF-8 bytes. Sizing covers a versioned canonical JSON evidence projection including the full query,
citation fields, JSON escaping/framing, and complete source. Bytes are not generation tokens;
Ask separately counts its complete provider request and reserves output tokens. `local_cross_encoder` and `retrieval_order` are never
silently substituted.

Snapshots record names, policy outcomes, Git metadata, and exact-byte SHA-256 fingerprints.
They are observations, not filesystem checkpoints or live-disk freshness claims. Git's executable
hooks, filesystem monitor, configured content filters, global/system configuration, and optional
index writes are disabled. Ignore controls use descriptor-relative reads and Git-compatible
matching for root and nested `.gitignore` files. Ignore rules above the registered root are
unavailable; that scope fails closed rather than reading outside it.

## Upgrade and retained history

Migrations `0001`–`0005` retain their upgrade semantics. Forward migrations `0006` and `0007`
quarantine old derived runs and repair ownership. Migration `0008` requires pgvector 0.8.6 in
`public` and creates persistent embedding evidence. Migration `0009` adds the immutable lexical
normalizer and GIN expression indexes without regenerating evidence. Test upgrades in disposable databases first.
For a real installation, stop the API and **take and verify a backup before migration**:

```sh
docker compose exec -T db pg_dump -U buildlens -Fc buildlens > buildlens-before-repair.dump
cd backend
uv run --no-sync alembic upgrade head
uv run --no-sync buildlens diagnostics
```

The Alpine-to-pgvector image transition must use logical backup/restore into a new volume; never
attach the old Alpine volume to the Debian image. Follow the complete
[Stage 7 rollout runbook](docs/STAGE7_ROLLOUT.md).

Quarantined legacy violations are preserved under PostgreSQL `NOT VALID` constraints. New
writes must satisfy those constraints; diagnostics explicitly lists pending validation. Review
legacy violations before validating them. The repair never deletes historical rows to make a
constraint pass. Repair downgrades refuse unsafe rollback; restore a compatible backup instead.
Stage 4 downgrade also refuses when multiple retained structural runs violate the older model.

Run counts and stored-content sizes grow intentionally. `buildlens diagnostics` reports counts,
chunk bytes, vector storage, embedding outcomes, and unvalidated constraints without exposing
content. `buildlens doctor` verifies connectivity, extension version/schema, and a cosine probe.
There is no automatic deletion policy.

Regenerate through the UI/API only when the named snapshot's bytes are available or compatible
verified evidence can be reused. If old snapshot bytes no longer exist, preserve that snapshot
and create a new one. Never relabel current bytes as an old snapshot.

## Verification

From `backend/`, use a disposable PostgreSQL service whose administrative role may create and
drop test databases. Fixtures generate a unique database per test and apply actual migrations;
they never drop the supplied administrative database or substitute SQLite.

```sh
export BUILDLENS_TEST_ADMIN_URL=postgresql+psycopg://postgres@127.0.0.1:55439/postgres
PYTHONPATH=src uv run --no-sync pytest
BUILDLENS_EMBEDDINGS_CACHE_DIR=/path/to/embedding-cache \
BUILDLENS_RERANKER_CACHE_DIR=/path/to/reranker-cache \
  uv run --no-sync pytest -m real_model
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy
```

Missing PostgreSQL configuration is an explicit skip locally and a hard failure in CI. The CI
matrix requires PostgreSQL tests on Linux with Python 3.11 and 3.14, frozen uv dependencies,
and `npm ci` followed by frontend tests, lint, type checking, and build.

The `real_model` gate requires both pinned models to have been acquired explicitly and fails rather
than skipping when either cache is missing. See [CURRENT_STATE.md](CURRENT_STATE.md) for actual
verification evidence and remaining operational gates.

**Stage 11 verification, 2026-10-02:** 143 deterministic backend tests passed on each of Python
3.11.15 and 3.14.6 against disposable PostgreSQL 16.15/pgvector 0.8.6 databases with actual
migrations. Full Ruff, formatting and mypy checks passed, as did three pinned CPU real-model
tests and 16 frontend tests, lint, TypeScript and production build. Integration checks cover
native corpus binding, benchmark exclusion, contamination rejection, deterministic rescoring
and mid-run authority invalidation. This infrastructure verification is separate from human
ground-truth approval and real-provider answer evaluation.

## Native RAG evaluation

Stage 11 adds `buildlens eval validate`, `eval run`, `eval report`, and `eval compare` over the
production services. Context-only evaluation is the default; provider answers require explicit
`--answers` and existing Ask/provider configuration. The human authorized sharing scope R1 and
the 90 production Ask evaluations. Definitions live under the shared excluded
`.buildlens-eval/datasets/` boundary; local runs and working reviews are Git-ignored. The shared
inventory policy `stage2.v3` and source reader `posix-secure-reader.v3` keep these assets out of
searchable evidence.

| Stage 11 dimension | Current state |
| --- | --- |
| Infrastructure verification | Passed: PostgreSQL integration, backend/static/frontend regressions and real local-model tests |
| Retrieval baseline | Executed: 90/90 successful production evaluations; rescored with approved curated relevance at reported cutoffs |
| Human ground-truth approval | Approved R1: 30 cases, 798 judgments (98 relevant / 700 irrelevant); 30/30 native bindings valid |
| Real-provider answer evaluation | Executed; inconclusive/partial: 88/90 operational failures, 2 valid abstentions, zero answered responses; answer quality not passed |
| Semantic answer review | Approved A1: 2/2 valid responses; safe abstentions, task-incorrect and missing sole facets; 88 errors ineligible |

The 2026-10-02 corpus was explicitly prepared through normal snapshot, structure, processing
and real embedding operations. It contains **2,099 embedded chunks from 157 searchable artifacts**
among 270 inventory entries. Reviewed dataset `4-r1-reviewed` has six cases in each of five
categories; all **30 exact evidence bindings passed**. The user explicitly approved packet R1,
including the definitions, recorded source-span refinements and relevance proposals. The original
draft and approval provenance are preserved.

The real semantic / hybrid / hybrid-plus-reranker run
`7e93df61-1fe5-4ba4-adda-d6c33518960a` is retained locally under
`.buildlens-eval/runs/retrieval-20261002/`. All **90 evaluations succeeded** in **160.42 seconds**,
with zero errors, invalid cases or unavailable observations and stable corpus authority.
The following measurements use the **24 answerable approved cases**; coverage averages the fraction
of required evidence groups satisfied per case, while complete evidence counts cases satisfying
every group.

| Measurement | Semantic | Hybrid | Hybrid + reranker |
| --- | --- | --- | --- |
| Recall@5 | 20.64% | 20.64% | 42.94% |
| Precision@5 | 15.00% | 15.00% | 33.33% |
| Recall@32 | 64.34% | 64.34% | 64.34% |
| Precision@32 | 8.85% | 8.85% | 8.85% |
| MRR@32 | 0.4560 | 0.4560 | 0.5972 |
| Required-evidence coverage@5 | 16.67% | 16.67% | 27.08% |
| Complete evidence@5 | 3/24 | 3/24 | 6/24 |
| Required-evidence coverage@32 | 44.44% | 44.44% | 44.44% |
| Complete evidence@32 | 9/24 | 9/24 | 9/24 |
| Packed-context coverage | 27.08% | 27.08% | 33.33% |
| Complete packed context | 5/24 | 5/24 | 7/24 |
| Mean evaluation duration | 1.290 s | 1.294 s | 2.719 s |

Durations include evaluator corpus verification and first-use model loading, rather than only
production request latency. Semantic and hybrid ranking orders were identical for all 30
questions: the lexical branch executed but returned zero candidates for these full-sentence
queries. This baseline does not establish a hybrid advantage. Reranking improved MRR in eight
cases, worsened it in four and left twelve unchanged. Packed-context coverage improved in three
cases, declined in two and was unchanged in 19.

Processing is partial: two Python test fixtures containing recognizable synthetic GitHub-token
patterns were rejected structurally, and 28 entries use unsupported formats, including TS/TSX.
These gaps supply no benchmark targets; embeddings cover every retained chunk. Recall/Precision/MRR
are case macro means over 24 answerable cases. All reported cutoff positions at 1/5/10/32 have
approved labels; 30 known relevant sources outside rank 32 remain in Recall denominators. Recall
uses curated relevance, not exhaustive corpus relevance. Deeper unjudged positions retain native
whole-ranking provisional flags. Six unsupported cases have no Recall/Precision/MRR or
requirement-coverage denominator. Approved source-span coverage does not establish semantic answer
or citation quality. The R1 workflow reduced 3,000 raw entries to 30 concise case cards;
`reviewed-results-r1.json` in the retained run records the exact rescored results.

After explicit R1 external-sharing authorization, the production Gemini corpus run retained
all 90 Ask evaluations: **2 valid abstentions, 69 rate-limit failures and 19 generation-unavailable
failures; zero answered responses**. All 270 token counts succeeded, authority stayed stable,
and no retries or fallback occurred. The two hybrid responses returned `gemini-3.8-flash`
(`identifier-06`: 8,363 input / 537 output tokens; `semantic-02`: 4,479 / 317). Failed-request
usage/billing and unavailable-error causes are unknown. The historical synthetic Gemini gate
is separate from this partial corpus verification.

The human explicitly approved A1, finalizing all six correctness/completeness/abstention
decisions for the two valid responses. Both are **appropriate safe abstentions**, **incorrect
for completing the answerable task**, with the **sole required facet missing**. Coverage is
2/2 eligible responses; the 88 failures have no answer and are excluded from semantic review.
Zero statements/citations make grounding and citation quality N/A with zero denominators.
No LLM judge was used. A complete three-way answer-quality comparison remains unavailable;
this real-provider result is **inconclusive/partial**, and answer quality is **not passed**.
The approved frozen builder location is `backend/src/buildlens/domain/source.py`
(`processing_contract()`); the compact summary was corrected without changing bindings.

Approved native reviews were validated and the retained run rescored. Three finalization tests
and immutable-artifact, zero-denominator, score-integrity and deterministic-report checks passed;
no full gate or new provider request was run. Stage 11 is **closed with these limitations**.
Final gate statuses and results are retained under
`.buildlens-eval/runs/stage11-closure-20261002/`; no required approval remains.

Offline reports retain compact score references to canonical case artifacts, support explicit
independent verification records through `eval report --verification`, and reproduce byte-identical
output when rescored with the same inputs. Actual offline self-comparison passed; it is a mechanics
check, not a second live benchmark. See [evaluation usage, results and limitations](docs/EVALUATION.md)
and [current verification](CURRENT_STATE.md). **Stage 12** (framework laboratory) is described below.


## Stage 12 framework laboratory

Optional extras add LangChain and LlamaIndex. Framework code runs only in supervised worker
processes; the base install and API work without them (`/api/v1/lab/status` reports what is
missing). Everything is offline by default: no model downloads and no external provider calls.

```sh
cd backend
uv sync --frozen --extra dev --extra embeddings --extra reranking \
  --extra lab-langchain --extra lab-llamaindex --extra lab-pgvector --extra lab-code
uv run --no-sync buildlens lab status
uv run --no-sync buildlens lab env sync langchain-postgres   # optional isolated environment
```

The UI is at `http://127.0.0.1:5173/lab`. HTTP routes: `GET /lab/status`, `GET|POST
/lab/experiments`, `POST /lab/validate`, `GET /lab/indexes[/{id}[/boundaries]]`, `POST
…/snapshots/{s}/lab/indexes`, `POST …/snapshots/{s}/lab/probe`, `GET /lab/runs…`, `POST
/lab/loader-diff`. CLI: `buildlens lab status|save|build|probe|bench|report|parity|loader-diff|
engineering|env sync|db provision`. pgvector store experiments need a **separate** lab database
(`BUILDLENS_LAB_DATABASE_URL`, provisioned with `buildlens lab db provision`). Real generation was
not authorized for Stage 12: the lab's `generate` callback is a gated stub (it refuses without
`BUILDLENS_LAB_GENERATION_ENABLED` and is not implemented beyond that), and the `lab-providers`
framework integrations are installed but not wired. Answer-related outputs come from deterministic
offline models and demonstrate mechanics only. Design, isolation model
and measured findings: [docs/FRAMEWORK_LAB.md](docs/FRAMEWORK_LAB.md); verification:
[CURRENT_STATE.md](CURRENT_STATE.md).

## Stage 13 (in progress): multi-language structure

Milestone A of the Semantic Project Model is implemented. "Analyse source files" now extracts
parse-only declarations for Python (frozen `ast` producer plus an additive facts pass) and for
TypeScript/TSX and JavaScript/JSX (`tree-sitter`, base install, nothing executed or resolved).
TS/JS files are then chunked and searchable through the existing retrieval modes.

API changes:
- Structural artifact responses add `language` and `extractions` (with `contract_current`).
- Symbols add TS declaration fields.
- Imports add ES/CommonJS forms.

Apply migration `0010` with `alembic upgrade head`. It is additive; take a backup first as usual.
Existing structural and processing evidence stays current, and re-running structure on a new
snapshot adds the new passes while preserving Python identities. Semantic catalog, AI
interpretations and the Explore view are later milestones. See
[docs/SEMANTIC_MODEL.md](docs/SEMANTIC_MODEL.md) for scope, deviations and verification.

