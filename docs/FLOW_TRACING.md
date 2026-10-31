# Stage 15 — Flow Tracing

**Status (2026-11-01):** Milestones A0 and A are implemented on the local branch `stage15/flow-tracing`. They are not committed and not independently reviewed. Milestones B–F (flow model, `FlowTrace` engine, `/flows` UI, explanations, evaluation) are **not started**.

The approved plan is Revision 3.1, which includes the A0 amendments approved on 2026-11-01. It lives outside the repository, in the owner's planning notes.

Stage 15 changes no earlier stage's status:
- The Stage 13 real-model evaluation is still deferred.
- The Stage 14 pre-registered human API suite is still not done.
- The Stage 14 R01–R05 remediation still awaits re-review.

## Milestone A0 — coverage, semantics and cost spike (2026-11-01)

A0 was a lab-only, read-only spike. Its evidence is in `.buildlens-eval/runs/stage15-a0-20261101/`, which is git-ignored: `REPORT.md`, `fastapi-semantics.v1.draft.json`, the measurements and `manifest.json`. It produced four results:

- **FastAPI semantics.** Yield-dependency teardown timing, background-task order, dependency order and status codes were verified from FastAPI source at eight tags (0.105.0 → 0.157.0) and from Starlette 1.6.0.
- **Version facts.** BuildLens's exact FastAPI pin is only in `backend/uv.lock`, which is 1,128,325 bytes — above the 1 MiB source cap.
- **D9 and D10 both triggered** on the two owner-approved corpora: `fastapi/full-stack-fastapi-template@f4bcfab` (MIT) and `polarsource/polar@9b67985` (Apache-2.0).
  - Aliases are 42.8% of the template's imports and 27.7% of Polar web's.
  - Stage 14 records 0 request sites in the template.
- **New decisions D13–D17**, approved by the owner: workspace packages, TanStack Query, external callback contracts, a router gap, and reading version files as data.

## Milestone A — the flow evidence run (D11)

A flow evidence run reads source **once**, through the supervised secure reader (`posix-secure-reader.v3`). The bytes must equal both the snapshot fingerprint and the active structural analysis's `input_fingerprint`. It writes only Stage 15 tables and the new pointer `project_versions.active_flow_evidence_run_id` (INV-3).

### What it records

Contracts: `flow-evidence.v1`, `python-flow.v1`, `ts-flow.v1`, `flow-cfg.v1`, `flow-shapes.v1`, `flow-display.v1` and `framework-version-facts.v1`.

For every scope — module, function, method, class body, lambda, comprehension, arrow function, function expression, class static block — it records:

| Part | Detail |
|---|---|
| Statement-level CFG | See below |
| Callsites | Callee and argument *shapes*, `awaited`, spans, plus decoded offsets for TS. One record per source call, listing every CFG node that evaluates it (`copies`) |
| Bindings | Parameters, assignments, loop targets, `with … as`, `except … as`, walrus, imports, destructuring, `self.attr` stores |
| JSX attribute bindings | TS only: element, intrinsic or component, attribute, value shape |
| Summaries | Parameters, returns and raises shapes, `invokes_parameter`, `normal_exit_reachable`, yield and await points, `executes` (class bodies, comprehensions, static blocks), dynamic features, unreachable nodes |

**The CFG:**
- **Edge kinds:** normal, true/false, case/no-match, exception, loop back/exit, break/continue (with labels), fall-through, resume normal/throw, return and raise.
- **Abrupt completions** are routed outward through enclosing frames. A `finally` body (and a `with` exit) is lowered **once per completion kind that reaches it**, so every graph path is a real path.
- **Guards** are Ferrante–Ottenstein–Warren control dependences over Cooper–Harvey–Kennedy post-dominators of the *normal* CFG (`domain/flow_cfg.py`). Exceptional reachability is reported separately (`exceptional_sources`).

**Analysis-safe facts vs display text:**
- Analysis-safe facts use exact identifiers (a homoglyph never equals ASCII). A literal is stored as `(type, sha256, length)`. `None`, booleans and small ints are stored by value, and path-shaped strings keep a sanitised path.
- Display text is bounded to 256 bytes, secret-screened (withheld on failure) and flagged for bidi and control characters. It is stored apart in `flow_display_texts`, and no analysis reads it.

**Version facts (D17).** Read as data in the worker (`tomllib`, `json`, `yaml.safe_load_all`), with an 8 MiB cap:
- files: `uv.lock`, `poetry.lock`, `pdm.lock`, `requirements*.txt`, `pyproject.toml`, `package-lock.json`, `pnpm-lock.yaml`, `package.json`;
- only a fixed package list is extracted;
- npm facts are kept per workspace importer.

### Lifecycle and invariants

- **Binding.** A run is bound to the active structural analysis (snapshot `FOR UPDATE`, captured authority, `superseded`, retained failures).
- **Freshness.** It is current while that analysis is active and the contract is unchanged. Otherwise current reads fail closed (409 `FLOW_EVIDENCE_REQUIRED` or `FLOW_EVIDENCE_STALE`), and history stays readable with `freshness_reasons`.
- **Reuse.** A rebuild reuses artifacts by `(input_fingerprint, flow-pass contract digest)` only when the stored rows reproduce their published evidence digest. Scope owners are rebound by stable identity plus exact declaration position; otherwise the artifact is extracted again.
- **Caps** (placeholders, D7):

  | Cap | Value | Exceeding it |
  |---|---|---|
  | Callsites per scope | 1,024 | Truncates that scope (`FLOW_LIMIT`) |
  | CFG nodes per scope | 2,048 | Truncates that scope (`FLOW_LIMIT`) |
  | Callsites per artifact | 8,192 | Truncates the remaining scopes |
  | Callsites per run | 500,000 | Fails the run closed |

### API and CLI

- **HTTP**, under `/api/v1/projects/{p}/snapshots/{s}/flow`:
  - `POST|GET /evidence` (POST requires JSON);
  - `GET /evidence/runs[/{id}]`;
  - `GET /evidence/coverage`;
  - `GET /evidence/artifacts/{artifact_analysis_id}`;
  - `GET /evidence/framework-facts`.
- **CLI:** `buildlens flow evidence run|status|coverage|facts`.

### Persistence

Migration `0017_flow_evidence` is additive. It adds:
- `flow_evidence_runs`, `flow_artifact_coverage`, `flow_scopes`, `flow_callsites`, `flow_jsx_bindings`, `flow_display_texts`, `flow_framework_facts`;
- `project_versions.active_flow_evidence_run_id`.

Same-run composite foreign keys bind coverage to `artifact_analyses` and scope owners to `symbols` of the same artifact analysis (INV-2). The downgrade refuses while runs exist.

### Measured (local, 2026-11-01, one run per phase; indicative, not the S7 protocol)

The flow evidence run, measured in a disposable PostgreSQL 16/pgvector 0.8.6 database (`.buildlens-eval/runs/stage15-milestone-a-20261101/`):

| Corpus | Artifacts | Outcome | Scopes / callsites | Flow run | Rerun after a new structural run | Worker peak RSS | Flow tables (two runs) |
|---|---|---|---|---|---|---|---|
| BuildLens `5c65617` | 337 | `partial`: 330 extracted, 5 partial (`SYNTAX_RECOVERED`), 2 not parsed by structure | 7,056 / 29,407 | 8.6 s | 7.6 s, 313 reused | 117.8 MB | about 87 MB |
| Template `f4bcfab` | 152 | `complete`: 152 extracted | 885 / 2,723 | 1.35 s | 1.37 s, 152 reused | 80.2 MB | — |

- The BuildLens storage breaks down as scopes about 32 MB, callsites 37 MB and display texts 17 MB.
- An in-process exception sweep over every Python and TS file of BuildLens, the template and Polar (about 7,000 files) raised **no** exception.
- Polar's diagnostics were all expected: 10 Jinja templates (`SYNTAX_ERROR`), recovered TS syntax errors, and one squashed Alembic migration truncated at the per-scope callsite cap.

### Verification (Milestone A gate, Python 3.14.6, disposable PostgreSQL 16 / pgvector 0.8.6)

**Flow tests:**
- `tests/unit/test_flow_cfg.py` (24): the hand-written guard and CFG expectations from plan §3.2 plus a construct matrix covering early exits, `finally` copies, return through `finally`, `for`/`else`, infinite loops, short-circuit, conditional expressions, `assert`, `match` with guards, `with` exits, scope kinds, bindings, switch fall-through, optional calls, labelled `continue`, `do`/`while`, logical assignment, partial parse, atoms, homoglyphs, display screening and caps.
- `tests/unit/test_flow_versions.py` (6).
- `tests/integration/test_flow_evidence.py` (5):
  - INV-3: every pre-existing table byte-identical over its pre-existing columns;
  - INV-4: the catalog and architecture runs stay fresh; a later structural run stales flow evidence; the rebuild reuses and rebinds;
  - INV-2: owners belong to the same structural run;
  - a source change after the snapshot gives `not_current`;
  - the HTTP contract, including the 415 JSON guard and a 409 on stale reads;
  - the downgrade refusal.

**Gate selection:** all unit and API tests plus the upgrade, persistent-embedding, foundation, architecture-API, multi-language and schema integration modules: **387 passed**.

**Migrations:** the chain is linear with the single head `0017_flow_evidence`. The empty-database round trip 0017 → 0016 → 0017 gives an identical `pg_dump -s`, except pg_dump's random `\restrict` tokens.

**Static checks:** ruff check and format are clean. mypy reports one error, in `tests/unit/test_architecture_literals.py:361`, which is **pre-existing**: the same error appears on a clean `git archive` of `5c65617` with this environment's mypy.

**Not run at this gate**, by the proportional-verification workflow: the Python 3.11 suite, the full backend suite, the frontend suite (there are no frontend changes) and the baselines. Those belong to stage closure.

**Defect found by the measurement and fixed:** 26 real BuildLens TS/TSX files failed with `EXTRACTOR_FAILED` because the unparenthesised single-parameter arrow (`x => …`) was mishandled. There is now a regression test.

### Deviations from the plan (for owner review)

- **`finally` lowering.** A `finally` body is lowered by **duplication per completion kind**, not once with completion-tagged edges and a pending-completion path state. Duplication is exact without path state, so post-dominance and guards need no special case. A callsite inside `finally` keeps one record with `copies`.
- **No `flow_bindings` table.** Bindings are stored in each scope's `summary` JSON. They are read per scope by reaching definitions in Milestone B.
- **Source byte cap.** The flow run's total cap defaults to 256 MiB; the structural run's is 64 MiB.
- **Approximations:**
  - `except*` is lowered like `except`;
  - non-static TS class field initialisers are not lowered yet (constructor semantics are Milestone B);
  - generator finalisation at garbage collection is not modelled.
- **Version files not covered:** `bun.lock` and `yarn.lock`. The template's frontend uses `bun.lock`, so only its declared ranges are read.
- **Reuse speed.** Reuse saves little time today (7.6 s against 8.6 s on BuildLens), because reused rows are loaded and re-inserted per artifact.
- **Persistence cost.** About 43 MB per run for a repository the size of BuildLens, and retention is indefinite (D7 input).

### Environment notes

- **Stale virtualenv copy.** The repository virtualenv holds a stale, non-editable `buildlens` copy (Finder-style `* 2.py` duplicates under `site-packages/buildlens`). Milestone A checks therefore ran with `PYTHONPATH=src:.`.
- **Test database.** A disposable test database container, `buildlens-stage15-test` (loopback port 55450, trust authentication), was created for these tests. The existing `buildlens-stage14-test` container was not used or changed.
