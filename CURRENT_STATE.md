# BuildLens current state

**Implemented scope:** Phase 2, Stage 8 — Lexical & Hybrid Retrieval.
**Foundation verification:** 2026-09-14. **Development database reset:** 2026-09-17.
**Stage 8 implementation verification:** 2026-09-19.

Stage 8 adds one explicit-mode retrieval endpoint over current PostgreSQL evidence. Exact mode
performs literal `C`-collated declaration-name, qualified-name, and full-locator matching. Lexical
mode uses `pg_catalog.simple`, a versioned camel/acronym/path normalizer, and independent content,
symbol, path, and chunk-metadata groups. Hybrid fuses bounded 100-candidate lexical and
exact-cosine lists with exact-arithmetic RRF (`k=60`) and explicit exact precedence. Candidate
selection excludes source bodies and vector payloads; one batch hydrates only the final chunks.
The Stage 7 semantic endpoint remains as a compatibility adapter over the same retrieval service.

Exact and lexical retrieval make no model/provider call. Semantic and hybrid require compatible
active embeddings and fail explicitly when unavailable. All branches share Stage 5 freshness,
ownership, filtering, integrity, timeout, and snapshot-lock rules. The UI defaults to lexical and
shows all four modes, component provenance, score labels, bounded-candidate notices, stale-response
cancellation, and an explicit hybrid-to-lexical switch.

Local Stage 8 verification passed 85 backend tests (including disposable PostgreSQL 16 migration
and all four API modes), 9 frontend tests, Ruff/formatting, full-source mypy, ESLint, TypeScript, and
the production build. Migration `0009` function properties, GIN catalogs, ORM parity,
normalization, tokenless queries, literal path matches, provider isolation, semantic compatibility,
and duplicate-safe exact-arithmetic RRF were exercised. The separately gated pinned real-model test
was not rerun; Stage 7's recorded evidence remains applicable because embedding contracts and
stored vectors did not change.

## Stage 7 retained foundation

Stage 7 persists immutable embedding runs and per-run chunk observations in PostgreSQL/pgvector.
Preparation remains explicit and bounded, reuses compatible logical identities without inference,
and atomically publishes only after rechecking Stage 5 and embedding authority. Exact cosine search,
three metadata filters, batched source hydration, retained failed/superseded attempts, metadata-only
history, and complete observation/reuse provenance are implemented.

The base install remains usable without ML packages. Model acquisition is a separate CLI command;
runtime loading is local-only with safetensors, verified checksums, offline library settings, no
remote code, and a supervised replace-on-failure worker. Persisted status/history survives missing
model packages and process or database restart; search still requires a compatible query provider.

Local verification on 2026-09-18 passed 62 non-PostgreSQL backend tests and 18 disposable,
actual-migration PostgreSQL cases (80 total outside the separately gated real-model test). Stage 7
cases verify 384-dimensional float32/cosine parity within `1e-5`, negative scores, duplicate/tie
ordering, persistence across a new application runtime, zero-inference unchanged reuse with new
observation UUIDs, metadata-only inspection, filters, selective inference after one chunk changes,
mixed-dimension history, durable empty runs, failed and competing terminal attempts, contract
changes, and direct ownership/revision/dimension/hash/norm/reuse constraint attacks. Administrator-preprovisioned
and insufficient-privilege extension cases passed; a plain PostgreSQL 16 container without
pgvector failed with the intended actionable migration error. All 9 frontend tests, ESLint,
TypeScript production build, backend Ruff/format, and mypy passed.

The stopped Alpine volume was inventoried and retained. Verified logical backups of `buildlens` and
`buildlens_stage4_test` were restored into the separate pgvector volume. The main database migrated
from `0007` to `0008`; pgvector 0.8.6 and a known cosine probe passed before and after a database
restart, including after the final ownership constraints were applied. The historical Stage 4
database remains at `0004` with two projects. See
`docs/STAGE7_ROLLOUT.md` for the exact rollout record.

The Stage 7 Python 3.11 real-model gate downloaded revision
`1110a243fdf4706b3f48f1d95db1a4f5529b4d41` and verified offline inference,
semantic ranking, determinism, normalization, multi-window tail coverage, and pgvector score parity
within `1e-5` on a disposable migrated database. CI gives that job the same pinned pgvector service.

## Historical Stage 6 and foundation evidence

The following sections preserve earlier foundation verification evidence.

<!--
The separate Python 3.11 real-model gate downloaded revision
`1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, verified its manifest, then passed offline inference,
semantic ranking, determinism, normalization, and multi-window tail coverage. All 9 frontend tests,
ESLint, TypeScript, the production build, backend Ruff, formatting, and mypy passed. CI now contains
the same required explicit-acquisition real-model gate.
-->

## Foundation database reset before Stage 6

Read-only inspection on 2026-09-17 confirmed the local `buildlens` database contained only
disposable fixtures at `0005_artifact_processing`: four "Integration Project" registrations,
"Live API verification", "Live frontend verification", and "Stage 2 Closure Fixture".
Every registered path was under `/tmp` or `/private/tmp`. The closure fixture owned the only
two snapshots and all 13 inventory rows. All derived evidence tables were empty and there were
no other active database connections. No project data or history reasonably requiring
preservation was identified.

Following the user's explicit reset instruction, only the `buildlens` database was dropped and
recreated. No backup or legacy upgrade was performed. Actual migrations `0001`–`0007` were
applied to the empty database. The existing volume and separate `buildlens_stage4_test` database
were preserved. The Compose container was recreated with `127.0.0.1:5432` exposure and is healthy.
An ignored, mode-0600 `.env` supplies the existing local role credentials without hard-coded
application defaults or credential rotation.

Verification against this fresh development database passed: ORM/schema parity, API health,
registration, snapshots, Python structure, chunk retrieval, unchanged reuse, selective file
update, and failed retry preserving usable content. The temporary verification project and its
dependent evidence were then removed. Final state: `0007_evidence_ownership` (head), all 12
application tables empty, and zero unvalidated constraints. This verification used the real
FastAPI application through its test client and PostgreSQL. The database is ready for Stage 6
development; this reset predated the Stage 6 implementation.

## Foundation repair status

The repair preserves the modular monolith, explicit operations, PostgreSQL authority, registered
projects, snapshots, and retained history. Forward migrations `0006` and `0007` quarantine old
derived evidence and enforce repaired ownership on new writes. No existing installation was
migrated during the initial repair. The local disposable database was subsequently reset as
recorded above. Other installations with valuable history still require a verified backup before
upgrade and explicit regeneration afterward.

Persistence ordering, cross-snapshot rebinding, conservative identities, compatible reuse,
publication serialization, source boundaries, chunking, freshness, historical APIs, frontend scope,
pagination, dependency locks, and mandatory PostgreSQL CI verification are implemented. The
verification gates below passed in disposable environments. This is evidence for the repaired
foundation, not a guarantee that later stages or every possible repository construct are correct.

## Verification evidence

| Gate | Actual result |
|---|---|
| macOS / Python 3.14.6 | 51 backend tests passed, including real migrated PostgreSQL; full Ruff lint/format and mypy passed |
| Linux / Python 3.11.16 | 51 tests passed in a disposable container; frozen install, full lint/format and mypy passed |
| Linux / Python 3.14.7 | 51 tests passed in a disposable container; frozen install, full lint/format and mypy passed |
| macOS / Python 3.11.15 | Earlier 48-test checkpoint passed; final additional cases verified on macOS 3.14 and both Linux runtimes |
| PostgreSQL 16 | Generated databases, actual migrations through `0007`, populated `0005` upgrade, preserved invalid history, downgrade refusal, ORM/Alembic parity, rollback and competing publications passed |
| Frontend | Clean `npm ci`; 7 tests passed under Vitest 4.1.11; ESLint, TypeScript and production build passed; npm audit reported zero advisories |
| Package / CLI | `uv sync --frozen --extra dev --no-editable` and installed `buildlens --help` passed |
| Browser → API → PostgreSQL | Chromium on macOS, synthetic repository and separate migrated database: registration → snapshot → structure → chunks → unchanged rescan/reuse → one-file update/selective reuse → failed retry preserving usable chunks passed |
| Browser errors / presentation | No browser runtime errors; dashboard and evidence screenshots inspected; exact updated Python chunk remained readable after failed retry |
| CI configuration | Required PostgreSQL service and Linux Python 3.11/3.14 matrix committed as workspace files; hosted GitHub Actions execution itself has not been run |

The browser fixture contained `sample.py` and `README.md`. Snapshot 2 reused both processing
outcomes and the Python declaration. Snapshot 3 regenerated only changed Python while reusing
README chunks. Restarting the disposable API with a one-byte processing limit produced failed
attempt #2 while usable run #1 remained fresh and its `return value + 2` chunk remained readable.
Old selected content cleared when snapshots changed. Automated frontend cases cover late status
and chunk responses, server pagination, and coordinated structural/processing refresh.

Two upstream Starlette/httpx deprecation warnings remain in backend test output. They do not
represent skipped tests. A clean install initially found an advisory in the old Vitest dependency;
Vitest was updated to 4.1.11 and the full frontend checks repeated with zero npm advisories.
See the [maintainer advisory](https://github.com/vitest-dev/vitest/security/advisories/GHSA-82fw-gwwq-j7x9).

A final worker-progress change removed the shared mutex so terminating a worker cannot leave
the parent's byte-accounting read waiting on that mutex. All 19 focused source-boundary/worker
cases and full backend lint, format, and mypy checks passed after that change.
Operational diagnostics on the browser fixture reported 3 snapshots, 3 structural runs,
4 processing runs, 6 chunks, 309 content bytes, and no unvalidated constraints.

## Audit-to-fix checklist

| Finding / risk | Repair | Regression evidence |
|---|---|---|
| Structural foreign-key insertion order; responses lacking assigned identities/revisions | Explicit dependency-order flushes; persisted result returned | `test_structural_persistence_and_unchanged_rescan`, `test_competing_publications_and_rollback` |
| Cross-snapshot reuse retained previous observation ownership | Current artifact/snapshot/run IDs rebound; source IDs retained only as provenance | Structural and processing reuse integration cases; browser unchanged rescan |
| Unstable or overconfident declaration continuity | Preserve identical observations; unique lexical slots only; current evidence before predecessor | `test_selective_update_ambiguous_slots_and_contract_changes` |
| Failed/competing attempts replacing usable evidence | Snapshot-row lock, captured authority checks, separate retained terminal attempts | Both structural and processing competing-publication cases; failure-preservation integration and browser retry |
| Executable Git configuration | Central bounded invocation; hooks/fsmonitor/filters/config disabled; process-group termination | `test_git_executable_configuration_never_runs`; bounded runner implementation |
| FIFO, symlink/root substitution, unsafe ignore reads and source leaks in failures | Nonblocking/no-follow descriptor-relative reads; pinned identity; source-free diagnostics; supervised deadlines | FIFO/missing/substitution, failed-read byte accounting, nested ignore-control, worker timeout/crash tests |
| Inconsistent screening between extraction and chunks | Shared whole-artifact screen before either persistence path | Structural-screen and processing secret-screen tests |
| Resource checks after producing partial results; excluded entries consuming budgets | Byte progress accounting, eligible-only budgets, production-time chunk limits, whole-artifact discard | Exact chunk-limit, partial-run limits, failed-read accounting and worker tests |
| Producer incompatibility and missing processing reuse | Versioned contracts and verified identity/input/eligibility/dependency checks | Producer-change integration case, unchanged and selective browser/API flows |
| Ambiguous chunk anchors and line-shift churn | Artifact/contract/logical-unit/part/hash keys with occurrence discriminators | Repeated-module/nested-region unique-key test and unchanged-key integration cases |
| Decoding, newlines, Markdown, JSON/TOML/YAML boundaries | Shared exact coordinates, Python encoding detection, precise fences and format-specific units | 3 encodings × 3 newline forms; duplicate JSON members, nested/duplicate headings, TOML/YAML slices, generic Unicode slices |
| Freshness tied only to structural run UUID | Per-artifact required-evidence fingerprints and compatible contracts | Equivalent reanalysis stays fresh; producer changes regenerate; current content scope checks |
| Legacy derived content exposed through historical paths | Forward quarantine migrations plus current and historical content guards | Populated upgrade, HTTP lifecycle/quarantine case (rows remain stored) |
| Missing ownership constraints and metadata drift | Composite ownership FKs/indexes; conditional legacy validation; aligned ORM | `test_migrated_schema_matches_metadata`, populated invalid-history upgrade case |
| Destructive downgrade with retained runs | Upfront Stage 4 downgrade guard | `test_stage4_downgrade_refuses_before_destructive_changes` |
| Infrastructure constructed inside application services; ORM leakage | Typed ports and dependency wiring; public service methods/domain DTOs | Full mypy/lint and API lifecycle cases; application imports inspected |
| Aggregate hydration in status/list paths and source fetched for metadata | Summary projections, DB counts, batched metadata, deferred source columns | Fixed query-count and source-free SQL assertions for chunk/symbol metadata; lifecycle lists |
| Inconsistent scopes, mutation guards, errors and run metadata | Scoped history and content; structured 404/409/503; processing JSON guard | HTTP lifecycle/quarantine and actual unavailable-database response tests |
| Frontend stale requests and missing pages/status distinctions | Keyed scope, abort/ignore late results, cleared content, coordinated refresh, paginated server totals | `EvidencePanels.test.tsx`, API-error test and browser flow |
| Unreproducible dependencies and optional PostgreSQL CI | uv lock, frozen install, npm lock/ci, mandatory PostgreSQL Linux matrix | Frozen package/CLI and both Linux verification runs; clean frontend install |
| Unclear retention/rollout/phase documentation | Updated README/architecture; storage diagnostics; explicit backup and regeneration | Disposable migration fixtures, operational CLI, this verification record |

## Remaining limitations and rollout boundary

- The local development database was inspected, reset, migrated, and verified on 2026-09-17.
  No other installation was upgraded; no legacy backup/rollout was performed.
- Quarantine prevents API content access; it does not remove source bytes from database storage
  or backups. Legacy violations may remain intentionally unvalidated and are reported by
  `buildlens diagnostics`. Retained history grows without automatic deletion.
- The secure reader supports POSIX/macOS/Linux. Non-POSIX operation fails rather than weakening
  the reader contract. Ignore controls outside the registered repository root fail closed.
- Snapshots are observations, not atomic filesystem checkpoints or stored copies of every file.
  Compatible evidence can be reused; otherwise unavailable old bytes require a new snapshot.
- The secret screen detects narrow high-confidence patterns. Permitted expressions and chunks
  remain sensitive code content. The application is a local unauthenticated tool.
- Runtime/producer contract changes intentionally invalidate reuse. Chunking does not claim full
  semantic parsing of every Markdown/TOML/YAML construct; unsupported boundaries use explicit
  fallback diagnostics and exact slices.
- This workspace has no Git repository. No commit, PR, or hosted CI run was created. The workflow
  file is ready for a repository; required local equivalents passed.

No ANN index, agents, authentication system, background job platform, automatic regeneration, or
automatic deletion were added. Stage 7 candidates and database result sets are bounded; retention
is indefinite.
