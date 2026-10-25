# Stage 13 — Semantic Project Model

**Status (2026-10-25): Milestones A (multi-language structural foundation), B (deterministic
semantic catalog) and C (controlled AI generation) implemented and verified locally.**
Milestone D (evaluation and hardening) is **not started**. Generation is off by default
(`BUILDLENS_SEMANTIC_GENERATION_ENABLED=false`). No real provider has been called: every
generation in verification used a loopback stub or mock transports.

The approved plan (revision 4) separates deterministic facts from AI interpretations. It keeps
every existing Stage 1–12 producer byte-identical and adds capabilities as separately contracted,
per-artifact passes. This document records what Milestones A–C actually built and verified.
The contracts for later milestones are design commitments, not implemented behaviour.

## Milestone A: what exists

| Capability | Implementation |
|---|---|
| Frozen Python producer | `PythonAstParser` (`python-ast.v2`), `structural_contract()`, `processing_contract()`, `contract_for("python")` and the Python `dependency_fingerprint` are unchanged. Golden parity is captured from `816b136` before any change (`backend/tests/golden/`). |
| Extractor registry | `domain/extractors.py`: language → declaration extractor (`None` = frozen Python) plus additive passes, each with its own contract and digest |
| Python facts | `python-facts.v1` (`infrastructure/structural/python_facts.py`). A second `ast` + `tokenize` pass attaches facts to parser declarations by exact position: bases, keywords, class assignments, return/yield/await flags, dynamic-feature calls, and call/decorator/base/raise references. It also records `__all__`, the `__main__` guard, and docstring, comment and string ranges as decoded offsets. |
| TS/TSX/JS declarations | `typescript-declarations.v1` and `javascript-declarations.v1` (`infrastructure/structural/typescript.py`). Parse-only `tree-sitter` 0.26.0 with `tree-sitter-typescript` 0.23.2 and `tree-sitter-javascript` 0.25.0, all in the base install. Grammar is chosen by extension (`.ts` vs `.tsx`). Covers functions, classes, methods, field arrows, interfaces, type aliases, enums, namespaces/ambient modules and variable-bound functions, plus every import and export form (including CommonJS), decorators, type parameters, JSX use and hook calls. |
| Identities | TS slot scheme `ts-declaration-slot.v1`, with declaration space recorded. Adjacent overloads form one `overload_group` symbol. Same-kind interface/namespace merges form one symbol with `additional_spans`. Cross-kind merges stay separate, with a `merges_with` fact. Anonymous defaults are named `default` (`name_source=export_default`). `memo`/`forwardRef` are unwrapped one level. React roles are **not** part of identity. |
| Partial parses | **G1–G3**: an ERROR/MISSING node drops its whole top-level statement, including everything nested in it. The extraction is `partial`, with dropped line ranges and `SYNTAX_RECOVERED` diagnostics. **G5**: dropped text remains module chunks. **G4** (catalog eligibility) belongs to Milestone B. |
| Grammar authority | Contracts record binding and grammar versions and a canary digest. The worker verifies the built-in canary before the first TS/JS extraction and fails closed (`GRAMMAR_CANARY_MISMATCH`, `GRAMMAR_ABI_INCOMPATIBLE`, `LANGUAGE_UNAVAILABLE`). |
| Per-language authority (M4) | Non-Python artifacts are current only while their own declaration extraction contract is current. They never inherit the run's Python `producer_contract`. A DB CHECK requires a declaration extraction for parsed non-Python artifacts. Current symbol/import reads filter stale TS evidence; explicit `run_id` history still reads it. |
| Processing | TS/JS declaration chunker with offset-precise regions: innermost-first claiming, merged regions, module residuals, and `SPAN_CONFLICT` on partial overlap. Dependency fingerprints are scheme-tagged: `NULL` = legacy Python, `ts-declarations-dependency.v1` for TS/JS. `processing_runs.processor_set` records the available processors. |
| Persistence | Migration `0010_multilanguage_structure`, **additive only**. New tables: `artifact_extractions`, `symbol_facts`, `symbol_references`, `export_references`. New nullable columns on `symbols`, `symbol_identities`, `artifact_analyses`, `artifact_processings`, `processing_runs`. Widened kind/form CHECKs. Downgrade refuses while Stage 13 evidence exists. |
| API/UI | Structural responses carry `language`, per-pass `extractions` with `contract_current`, TS declaration fields and ES import forms. The panel is now "Source structure", shows extractor coverage, and marks stale contracts "not current". |

### Upgrade concerns

The three concerns are kept separate:
1. **Schema migration:** additive; no backfill, no rewritten rows, no pointer changes.
2. **Compatibility:** legacy runs stay current for what they cover. Missing passes are coverage gaps, not staleness.
3. **Regeneration:** explicit only. A reused Python declaration gains `python-facts.v1` through a facts-only pass that re-reads and fingerprint-verifies the source, preserving identities. If the source is unavailable, that pass fails and active evidence is untouched.

### Deviations from the plan
- **Worker memory.** tree-sitter 0.26 has no in-parse cancellation. Containment is process isolation, the existing parent deadline/kill, and a peak-RSS check after each extraction that recycles the worker (`RESOURCE_LIMIT`, default 1 GiB). The plan's sampled-RSS supervision during a parse was not implemented.
- **Suffixes.** `.mjs/.cjs/.mts/.cts` are not supported: the Stage 2 inventory does not classify them, and changing classification was out of scope. `.min.js` is skipped as `GENERATED_ARTIFACT`.
- **Tooling location.** The TS differential tool is in `backend/scripts/` (lab-only), not `infrastructure/lab/`.
- **Coordinates.** `tree_sitter.Point` is never used (see the defect below). Lines follow the chunk convention (`\r\n`, `\r`, `\n`); columns are UTF-8 byte columns, as with `ast`.

### Defect found during verification
py-tree-sitter 0.26.0's `Point` row/column integers above CPython's small-int cache could be freed
with the `Point` while still referenced. This corrupted the heap and caused segmentation faults on
real files with lines over 256 bytes (10 of 31 BuildLens frontend files). The extractor now derives
positions from byte offsets only. A subprocess regression test reproduces the crash condition (exit
139 with the old `Point` path) and passes with the fix. A static test forbids `start_point`/`end_point`.

## Verification (2026-10-25)

All of this ran locally, against disposable databases or copies; nothing was published or run in
hosted CI.

**Golden parity (M3)**
- Pre-change outputs were captured on Python 3.14.6 and 3.11.15 for 24 fixture variants, including CRLF, CR, BOM, latin-1, astral characters and syntax errors.
- Declarations, chunk boundaries and stable chunk keys are unchanged on both runtimes.
- State and dependency fingerprints match on the captured patch versions.

**Backend suite**
- Python 3.14.6 and 3.11.15, each run alone: 240 passed, 1 skipped (the pre-existing lab-database test without `BUILDLENS_TEST_LAB_DATABASE_URL`) and 5 deselected (real model/provider), against disposable PostgreSQL 16 / pgvector 0.8.6.
- Concurrent runs on one server additionally skip the lab provisioning test by design, because it requires an otherwise empty server.
- Ruff, format and mypy are clean on both runtimes.

**Upgrade check (M1/M2)** — `backend/scripts/stage13_upgrade_check.py`
- A 0009 database was populated by the real `816b136` code and upgraded with the new code.
- All 14 pre-existing data tables are byte-identical over their original columns. Only `alembic_version` changed.
- Old structural and processing runs remain current and fresh.
- A new snapshot reuses Python declarations, with identities preserved, and adds facts.
- TS is extracted, and the TS chunks carry `ts-declarations-dependency.v1`.

**Stage 11/12 baseline (§8.6)**
- The retained Stage 11 volume was copied, with the original mounted read-only and never started, then upgraded to 0010. All 14 data tables are unchanged.
- `eval validate` on the corpus copy: corpus passed, 30/30 cases bound, 0 invalid. This must run on Python 3.11.15, the corpus's recorded runtime; Python 3.14 reports `PROCESSING_CONTRACT_INCOMPATIBLE` by the existing runtime pinning.
- `lab parity` on the corpus copy: 90/90 retrieval, ranking and packed context equal; corpus digest `03c559c4…` unchanged. The first attempt timed out loading the embedding worker in a freshly installed environment; the retry passed.

**TS differential**
- 35 files: 31 frontend sources plus 4 synthetic files with overloads, merging, wrappers, nested callbacks and CommonJS. The compiler-side parser saw 193 declarations.
- One disagreement: tree-sitter reports `ApiError.constructor` as a method, and the reference script does not model constructors. It is retained as reported.

**Frontend**
- 22 tests passed. Lint, TypeScript and production build passed.

Reports are retained under `.buildlens-eval/runs/stage13-milestone-a-20261025/` (git-ignored).

## Milestone B: deterministic semantic catalog

`POST …/semantic/catalog` builds an immutable, snapshot-owned catalog run from PostgreSQL only.
It reads the active current structural run, the active fresh processing run (chunk rows) and the
inventory. **It never reads repository files**: processing is consulted only for authority and
freshness, and no source reader is ever called. The run is
published with the existing pattern: snapshot `FOR UPDATE`, captured authority and pointer, a
`superseded` flag, and retained failures. Nothing in it is AI text (F4).

| Capability | Implementation |
|---|---|
| Builder | `application/semantic_catalog.py`: `build_catalog` is a pure function of the normalised rows. `SemanticCatalogService` adds authority, the in-process lock, freshness and reads. |
| Entities | Keys come from existing stable identities: `file:<artifact identity>`, `symbol:<symbol identity>`, `component:`/`hook:<symbol identity>`, `api_endpoint:<handler identity>:<recognizer>:<n>`, `db_table:<class identity>` or `db_table:<artifact identity>:<n>`. Role entities point to their `subject_entity_key`. |
| Stored-source coverage (§3.4) | Per entity: `complete`, `incomplete` (no chunk for the entity or for a declaration inside it), `unavailable` (not processed, processing failed, stale dependency, structure failed, non-current TS contract) or `coordinate_mismatch` (overlap, region start, outside region, chunk hash/length). Whitespace gaps are counted. |
| Generation-context coverage (§4.2) | `full` for symbols within the byte ceiling; `outline` for files and for containers that exceed it, with residual chunks, child signature facts and an explicit omitted-region list; otherwise `not_eligible` with a reason. G4: a partially parsed file entity is `not_eligible` (`PARTIAL_PARSE_ARTIFACT`). `package_digest` binds mode, chunk keys and hashes, fact digests, neighbour facts and omitted regions. |
| Facts (`F#`) | `application/fact_renderer.py` (`fact-renderer.v1`). Kinds: signature, import, export, reference, base, recognizer_match, relation, limitation. Each fact stores `source_refs` (table, row id, producer contract digest, locator), a `derived` record and `fact_digest = sha256(renderer, text, refs, derived)`. Rendered text never contains database ids. |
| Read-time verification (F2) | Entity detail re-loads every cited row with the same normalisers, checks contract and locator, re-renders and compares the digest (`verified`, `FACT_SOURCE_MISSING`, `FACT_PROVENANCE_CHANGED`, `FACT_DIGEST_MISMATCH`, `FACT_RENDERER_CHANGED`). Evidence chunks are hash- and length-verified; failing content is withheld. |
| Recognizers | `application/recognizers.py`: `fastapi-route.v1`, `flask-route.v1`, `sqlalchemy-declarative.v1`, `alembic-table.v1`, `react-component.v1`, `react-hook.v1` (labelled naming convention). Each match lists conditions met and unmet; strength is `pattern_with_local_evidence` or `pattern_only`. No numeric confidence. |
| Import resolution | `import-resolution.v1` against the snapshot inventory: `resolved`, `ambiguous`, `not_found`, `external`, `not_attempted` (path aliases such as `@/`). Python roots are the repository root, each `pyproject.toml`/`setup.py`/`setup.cfg` directory and its `src/`. A directory without `__init__.py` never shadows an installed package. TS/JS follows a fixed extension and `index` priority. |
| Relations | contains, imports, exports, references (same file only), renders, uses_hook and inherits (same file or one-hop import binding), defines_route, maps_table. Each relation is backed by a `relation` fact. There is no cross-file call graph. |
| Reproducibility | `input_authority_digest` short-circuits an unchanged rebuild. `catalog_digest` covers sorted rows without row ids or timestamps. `portable_digest` replaces identities with locator, qualified name, kind and ordinal, so two databases holding the same bytes agree. |
| Freshness (§5.3) | A catalog is stale when the structural or processing authority it captured is no longer active, processing freshness fails, or the catalog contract (versions, ceiling, current extractor digests) changes. Current reads return 409 `SEMANTIC_CATALOG_STALE` (or `SEMANTIC_CATALOG_REQUIRED`); explicit `run_id` history stays readable and is labelled. |
| Persistence | Migration `0011_semantic_catalog` (additive): `semantic_catalog_runs`, `semantic_entities`, `semantic_facts`, `semantic_relations`, plus `project_versions.active_semantic_catalog_run_id` (composite deferred FK). Downgrade refuses while catalog runs exist. The tables are listed by `buildlens diagnostics`. |
| API | `api/semantic_routes.py`: `POST|GET /catalog`, `GET /catalog/runs[/{id}]`, `GET /entities` (kind, language, locator prefix, both coverages, text query; paginated), `GET /entities/{key}` (facts with provenance and verification, neighbour facts, relations with labels, evidence, omitted regions, integrity errors). `/semantic/catalog` is a JSON-only mutation. |
| UI | The snapshot panel gains "Semantic catalog" (build, status, counts). `/explore` is read-only: an entity tree with filters, source assembled from stored chunks (whitespace gaps marked, omitted outline regions shaded) and Facts, Relations and Coverage tabs. Facts carry "Parsed", "Pattern · local evidence" or "Pattern only" badges plus their verification state. |

### Deviations from the plan (Milestone B)
- **Migration split.** The catalog tables are migration `0011_semantic_catalog`. Milestone C will add the generation tables as a new revision instead of amending an applied one, so the plan's single `0011_semantic_model` becomes two revisions.
- **Derived values in `F#`.** Import resolution, recognizer conditions and relation layout depend on the whole inventory, not only on the cited rows. They are stored in each fact's `derived` record, which the digest covers. Re-rendering detects any change to a cited row; a change to the derivation itself is detected by catalog-digest reproduction, not by single-fact re-rendering.
- **Coverage strictness.** A symbol is `incomplete` when any declaration inside it has no stored chunk (`DECLARATION_WITHOUT_CHUNK`), not only when its own region lacks one.
- **Alembic table names** are not captured, because call arguments are not extracted. The entities are named `table #n in <file>`.
- **Concurrency.** Catalog builds use the in-process lock plus captured-revision publication (a late concurrent build is retained as `superseded`). The DB-enforced leases in the plan apply to generation (Milestone C).
- **Not yet built:** entity history, interpretations, generation tabs and a catalog CLI command. They belong to Milestone C.
- **Placeholder policy.** The 48,000-byte package ceiling and the 20 neighbour-fact limit are recorded in the catalog contract, pending tuning (§10.7).

### Verification (Milestone B, 2026-10-25)
All of this ran locally against disposable PostgreSQL 16 / pgvector 0.8.6 databases or copies. Nothing was published or run in hosted CI.

**Backend suite**
- Python 3.14.6 and 3.11.15, each run alone: 261 passed, 1 skipped (the lab-database test without `BUILDLENS_TEST_LAB_DATABASE_URL`), 5 deselected (real model/provider).
- Ruff, format and mypy are clean on both runtimes.
- The 3.11 run caught a migration annotation (`sa.SchemaItem`) that 3.14's deferred annotations had hidden; it is fixed.
- New tests:
  - `tests/unit/test_semantic_catalog.py` (15): resolver, recognizers with negatives, coverage states and reasons, budget outline/rejection, TS merges and partial files, non-current contracts, order-independent determinism, renderer row dependence.
  - `tests/integration/test_semantic_catalog.py` (6): entities, recognizers and relations on a synthetic Python + TSX repository; short-circuit; staleness (409) and labelled history; identical catalog and portable digests after reprocessing; equal portable and different catalog digests across two projects; tampered symbol and chunk rows detected at read time; a legacy (facts-less) build with the repository deleted and the source reader disabled; superseded and failed runs never moving the pointer; HTTP contracts; downgrade refusal.

**Real repository (BuildLens itself, disposable database)**
- Pipeline: 254 structural artifacts and 2,432 symbols; the catalog builds 2,825 entities, 14,793 facts and 6,514 relations in about 3 s.
- Stored coverage is `complete` for every entity except the two files whose structure failed. There are no coordinate mismatches.
- Recognizers: 56 FastAPI routes, 22 SQLAlchemy and 22 Alembic tables, 39 React components.
- About 300 sampled entity details re-verified with no integrity error.
- An unchanged rebuild short-circuits. After a reprocess, the rebuilt catalog has an identical `catalog_digest` and `portable_digest`.
- The first run surfaced 24 `alembic` imports wrongly marked `not_found`, because the repository's `backend/alembic/` folder was treated as a package. Fixed and covered by a unit test.

**Stage 11 baseline (§8.6, Milestone B)**
- The retained Stage 11 volume was copied, with the original mounted read-only and never started, then upgraded 0009 → 0011.
- All 14 pre-existing data tables are byte-identical over their original columns, both after the upgrade and after building a catalog.
- The legacy catalog builds with the source reader disabled: 1,353 entities, all Python carrying `FACTS_NOT_EXTRACTED`, two `STRUCTURE_FAILED` files `unavailable`, no sampled integrity error.
- `eval validate` on Python 3.11.15: corpus passed, 30/30 cases bound, 0 invalid; definition and judgments digests are identical to Milestone A's record.
- `lab parity` on Python 3.11.15: 90/90 retrieval, ranking and packed context equal; corpus digest `03c559c4…` unchanged.
  - The first two attempts timed out, because the first query loads the embedding model inside the 15 s search deadline and a cold load took about 15.4 s in the freshly built environment.
  - This is pre-existing Stage 10–12 behaviour, unrelated to this milestone. The passing run raised only `BUILDLENS_EMBEDDINGS_SEARCH_TIMEOUT_SECONDS` to 60, a timing allowance that does not affect rankings.

**Browser run**
- Synthetic Python + TSX repository through the real API and Vite dev server: snapshot → structure → processing → catalog → Explore (entity tree, verified source with whitespace gaps and shaded outline regions, labelled facts, relations in both directions).
- Mutations were issued through the local API.
- The desktop preview runner could not access the Desktop folder, so both servers were started from the shell.

**Frontend:** 26 tests passed (4 new). Lint, TypeScript and production build passed.

## Milestone C: controlled semantic generation

AI output is a separate record from facts (F4) and is never Ask evidence. An interpretation is a
set of cited claims plus unknowns; there is no free-form prose. Nothing runs automatically:
every outbound request is authorised by a persisted, single-use plan, budgeted in the database
before sending, and sent only through `OutboundGate`.

| Capability | Implementation |
|---|---|
| Packages and prompt | `application/semantic_prompt.py`: the package is rebuilt from the catalog and stored chunks, every chunk hash-verified and every fact re-rendered, and must reproduce the catalog's `package_digest`. `E#` chunks, own and neighbour `F#` facts, limitations and the omitted-region list go into one canonical untrusted-data JSON. The prompt (`semantic-prompt.v1`) and strict output schema (`semantic-claims.v1`) are versioned. |
| Validator | `application/semantic_validation.py` (`semantic-validator.v1`), checks 1–9: structure and size with duplicate keys rejected; evidence ids; exact, unique 8–200-character quotes; identifier grounding; basis (code / documentation / string from the artifact's comment, docstring and string ranges; `behavior` and `side_effect` need a code quote); omitted implementation (behaviour about omitted children, neighbours or external callees is rejected unless the claim is a `calls` assertion that matches parsed facts); assertions versus facts (`matches_fact` / `conflicts_with_fact` / `not_checkable`); verdict lexicon rejected and quantifiers flagged; secret screening. Claims are `cited` or `conflicts_with_fact`; interpretations are `accepted`, `accepted_with_conflicts`, `insufficient_evidence` or `rejected` (never stored). |
| Outbound gate (§11.2) | `application/outbound_gate.py`: a T1 transaction mints a single-use `GateTicket` bound to the run, lease token and `request_digest`. Each adapter's `send` first redeems the ticket against the exact canonical bytes; a reused, foreign or mismatched ticket is refused before any I/O. |
| Providers | `infrastructure/generation/semantic_{local,openai,gemini}.py`. Loopback: OpenAI-compatible chat completions on `http://127.0.0.1:<port>` or `http://[::1]:<port>` only, `trust_env=False`, no redirects, no retries, no probes, 1 MiB response cap, labelled "loopback endpoint". OpenAI: `max_retries=0`, `store=False`, `stream=False`, `background=False`, no tools. Gemini: `HttpRetryOptions(attempts=1)`, AFC disabled, no `count_tokens` or model listing. Clients are created lazily; construction, status, rendering and plan creation send nothing. |
| Plans (§11.1) | `semantic_generation_plans` and `_items` hold the selection, mode, `accept_profiles`, provider configuration and its digest, generator profile, captured catalog / structural / processing / index authority, caps, totals, the union of contributing locators (own source and neighbour-fact sources with bytes), the DB-time `issued_at`/`expires_at`, a nonce generated once, and `plan_digest` over the stored rows. |
| Lifecycle (§4.4, §11.3) | T0 locks the snapshot, verifies the stored digest and the supplied one (`PLAN_INVALID`), rejects a consumed plan (`PLAN_ALREADY_USED`), expiry (`PLAN_EXPIRED`), a changed provider configuration (`PLAN_CONFIG_CHANGED`) or changed authority (`PLAN_CHANGED`), abandons expired-lease runs and inserts the run (`UNIQUE plan_id`; one `executing` run per snapshot by a partial unique index, else `OPERATION_CONFLICT`). T1 enforces the outbound and per-entity budgets under the run lock. A pre-send recheck re-reads authority, rebuilds the package and requires the planned `request_digest`; otherwise the attempt is recorded `not_sent`. T2 records the outcome and, if accepted, the interpretation and claims. T4 copies the previous index forward, overlays this run's results and moves the pointer only when captured authority (including the index run) is unchanged; otherwise the run is `superseded`. Every write uses the DB-time fencing predicate. |
| Fencing and recovery | A fenced T2 records a `semantic_generation_late_receipts` row (no content) and never a candidate. Abandonment (next T0, `buildlens semantic recover`, or self-abandonment) marks attempts without outcomes `outcome_unknown`. Terminal states never change (a guarded `UPDATE … WHERE state='executing'` plus a CHECK). Candidates committed under a valid lease survive `superseded` and `abandoned` runs and stay reusable. |
| Retries and bounds | Retries are explicit only: 429/503 with `Retry-After` ≤ 60 s that still fits the deadline, each a new T1 with a new ticket and budget increment. Reported prompt tokens above request bytes + 512 stop the run (`TOKEN_BOUND_VIOLATED`). Defaults: 10 requests, 2 attempts per entity, 300 s deadline, 64,000-byte request ceiling, 30-minute plan TTL (placeholders). |
| Reuse and freshness (§4.4.3, §5.3) | `input_key` is source freshness only; `generator_profile_digest` (provider, requested model, policy) is separate. `generate` mode reuses only the same profile; `reuse_only` needs explicit `accept_profiles` (a list or `any`) and sends nothing. Index entries record `generated`, `reused` or `retained` and `reused_from_profile`; provenance (profile, requested and returned model, run, attempt) is immutable. Entry state is computed at read time: `current`, `stale` (`SOURCE_CHANGED`, `FACTS_CHANGED`, `NEIGHBOR_FACTS_CHANGED`, `CONTEXT_COVERAGE_CHANGED`, `CONTRACT_CHANGED`, `ENTITY_REMOVED`) or `absent`. |
| Read-time citations (F2) | Every claim citation (chunk stable key + hash + offsets, or fact digest + offsets) is resolved and its quote hash re-verified on read; a failing citation withholds the claim text. Quote text is never stored. |
| Persistence | Migration `0012_semantic_generation` (additive): plans, plan items, runs, attempts, attempt outcomes, late receipts, interpretations, claims, index entries, `project_versions.active_semantic_index_run_id`, and `semantic_entities.content_digest`. Downgrade refuses while plans or runs exist. |
| API, CLI, UI | `GET /api/v1/semantic-generation` (configuration only); `POST …/semantic/generation/plan`, `GET …/generation/plans/{id}`, `POST …/generation` with `{plan_id, plan_digest, confirm: true}`, `GET …/generation/runs[/{id}]` with attempts and outcomes, `GET …/entities/{key}/interpretation`, `…/history`, `GET …/interpretations/{id}`, and `state` filtering on `/entities`. `buildlens semantic catalog|status|plan|generate|recover`. Explore adds Interpretation (provenance banner, chips, conflicts and unknowns, cited lines highlighted), History and Generate (plan with every locator, bytes, caps, sharing class, retention note and expiry; explicit checkbox before "Authorise and run"). Model text is rendered as plain text only (F7). |

### Deviations from the plan (Milestone C)
- **Ask adapters untouched.** Instead of a move-only refactor of the Ask Gemini/OpenAI adapters into a shared `common.py`, the Ask files are byte-unchanged and the semantic adapters construct their own clients (`semantic_common.py` holds only semantic helpers). Ask parity therefore holds by construction; the existing Ask tests pass unchanged.
- **Freshness key.** The plan defines `input_key` from `package_digest`, but fact digests include cited structural row ids (§3.5), which change in every snapshot, so cross-snapshot reuse (§5.3) could never occur. `input_key` is instead derived from a per-entity `content_digest`: the catalog's portable content projection (chunk hashes and positions, rendered fact texts, omitted regions by portable key, mode). The row-level `package_digest` is still recomputed and required before every send. This is catalog contract `semantic-catalog.v2`; v1 catalogs show `CATALOG_CONTRACT_CHANGED` and must be rebuilt.
- **Migrations.** Generation tables are a separate revision `0012`, not an amendment of `0011`.
- **`not_sent` outcome.** An attempt whose pre-send recheck fails after T1 consumed budget is recorded `not_sent` with a reason (`AUTHORITY_CHANGED`, `PACKAGE_INTEGRITY`, `REQUEST_DIGEST_CHANGED`) rather than reusing another code.
- **T0 order.** A consumed plan reports `PLAN_ALREADY_USED` before the expiry, configuration and authority checks (its own publication would otherwise surface as `PLAN_CHANGED`).
- **Scenario 6 (R3).** With one `executing` run per snapshot, a newer index cannot be published by another process while a run still holds its lease; the guard was tested by forcing the active index pointer, and a run whose lease was taken over is fenced at T4.
- **Basis classification.** Without comment/string ranges (legacy facts) quotes are `unclassified`, so behaviour claims are rejected; TS/JS string literals are not ranged and count as code. Unknowns are grounded against both facts and evidence.
- **Gemini `Retry-After`.** The SDK error does not reliably expose response headers, so a Gemini 429 is recorded and not retried.
- **Not verified against real providers.** The OpenAI and Gemini adapters were exercised only through injected mock transports; whether each provider accepts the strict schema as written (`anyOf` with null, `minItems`/`maxItems`) is unverified. The opt-in real-provider gate was not run.

### Verification (Milestone C, 2026-10-25)
- **Backend:** 307 passed, 1 skipped (lab-database test without its URL), 5 deselected on Python 3.14.6 and 3.11.15; ruff, format and mypy clean on both. New tests:
  - `tests/unit/test_semantic_validation.py` (20): every check's rejection code, basis labels, omitted-implementation cases (external callee, neighbour, the allowed `calls` form, a mismatched assertion), assertion outcomes, quantifier flags, unknowns, statuses, identifier extraction, stale-reason manifest diffs.
  - `tests/unit/test_semantic_outbound.py` (17): construction, status and render make 0 requests for all three adapters; one ticket is exactly one request; reused, forged or byte-mismatched tickets are refused; a 429 is one request with no SDK retry; loopback sends exactly the canonical bytes, refuses redirects, caps responses, ignores proxy environment and accepts only literal loopback URLs; an AST architecture test (only `gate.send`; every adapter redeems first; no provider imports in application code; Ask adapters free of semantic code).
  - `tests/integration/test_semantic_generation.py` (9): plan stability, reuse and profile rules, returned-model provenance; tampered item or digest, expiry, configuration and authority changes; explicit 429 retry, budget edge, token bound, rejected output; pre-send recheck blocking changed bytes and authority with 0 requests; all six fencing scenarios; concurrent T1 at the budget edge; two OS processes consuming one plan (one run, one `PLAN_ALREADY_USED`); a slow run in another process blocking a second run (`OPERATION_CONFLICT`); HTTP contracts and a contract bump making entries `stale`; a new snapshot whose `reuse_only` plan adds exact-key entries with 0 requests.
- **Frontend:** 29 tests passed (3 new), lint, types and build.
- **Browser run:** synthetic Python + TSX repository, real API configured for a loopback stub: plan and execution through the local API (6 requests, 6 accepted), then Explore showing "AI current" badges and the Interpretation, History and Generate tabs.
- **Stage 11 baseline:** a fresh copy of the retained volume upgraded 0009 → 0012: the 14 pre-existing tables byte-identical after the upgrade and after a catalog build; the legacy catalog builds with the source reader disabled; `eval validate` 30/30 with identical digests; `lab parity` 90/90 (retrieval, ranking and packed context equal; corpus digest `03c559c4…` unchanged; run with the 60 s search timeout described under Milestone B).

## Not built yet (later milestones)
- Milestone D: adversarial repository, grounding metrics, pre-registered human review, an optional authorised real-provider run.
