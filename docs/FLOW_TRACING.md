# Stage 15 — Flow Tracing

**Status (2026-11-01):**
- Milestones A0 and A are done. Milestone A is committed on the local branch `stage15/flow-tracing` (`3cb2af8`).
- **Milestone B is implemented and committed locally** on the same branch. Its acceptance checks pass locally: designed fixtures, zero-edge non-links, the tamper suite and portable-digest reproduction. Approximations stay labelled in the model. Milestone B is not reviewed.
- Nothing has been independently reviewed.
- **Milestone C (the `FlowTrace` engine) is implemented and committed locally.** Its designed oracles pass locally.
- Milestones D–F (`/flows` UI, explanations, evaluation) are **not started**.

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

**Correction found in Milestone B (for owner review).** The A0 report treated the template's hey-api client as the `@hey-api/client-*` package. In fact the client runtime is **vendored**: the generated `client.gen.ts` imports `axios` directly. Binding by package name therefore misses it. D10 now also recognises a factory shape, `DESCRIPTOR_CLIENT_FACTORY_SHAPE`, with these conditions:
- the factory returns an object literal with at least three HTTP-verb keys and a `request` key;
- it is built in a module that imports `axios` or calls `fetch`.

This is a structural heuristic, so its links stay `inferred`. It needs the owner's approval as a D10 amendment.

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

## Milestone B — the flow model (implemented; committed locally)

A flow model run captures one flow evidence run, the semantic catalog run and the architecture run, all over the **same** structural analysis. Otherwise the build fails with `FLOW_INPUTS_INCONSISTENT`. It reads only PostgreSQL (INV-1). It writes only `flow_*` tables and `project_versions.active_flow_run_id` (migration `0018_flow_model`, INV-3).

The builder is pure:
- `application/flow_builder.py` holds the version table, stitching and responses;
- `flow_python.py` and `flow_typescript.py` hold resolution and the framework semantics;
- `flow_inputs.py` holds the inputs, the emitter and scope views.

The domain is in `domain/flow.py` (`flow-model.v1`, `flow-integrity.v1`). The store is `infrastructure/persistence/flow.py`, and the service is `application/flow.py`.

### Implemented

**Resolution** (`py-call-resolution.v1`, `ts-call-resolution.v1`):
- reaching definitions on the CFG (`flow_cfg.reaching_definitions`);
- imports through Stage 13 `ImportResolver`, with at most two re-export hops;
- `self`/`cls` through the C3 MRO, with `MRO_INCOMPLETE` for external bases;
- override `dispatch_candidate`s, fan-out capped;
- annotation receivers (`ANNOTATION_TRUSTED`) and `Depends` provider return types;
- one-level callbacks;
- TS lexical scope, ES/CommonJS imports, `this`, and prop callbacks (`PROP_CALLBACK_BINDING`);
- `ts-path-aliases.v1` (D9) with an `extends` chain of depth ≤ 3;
- `ts-workspace-packages.v1` (D13), with dist→source mapping only through literal `rootDir`/`outDir`.

No cross-file name similarity is ever used.

**Construction** (`py-construction.v1`):
- the metaclass `__call__` override gives `unknown` plus a candidate;
- `__new__` → `__init__`: `derived` only when `__new__` returns `super().__new__`. Otherwise `INIT_CONDITIONAL_ON_NEW_RESULT`, or no `__init__` at all when `__new__` returns another type (`NEW_RETURNS_OTHER_TYPE`);
- the dataclass-generated `__init__` → `__post_init__`, only for a resolved `dataclasses.dataclass` without `init=False` and without an explicit `__init__`;
- an Enum call is a lookup;
- framework model bases get `CONSTRUCTION_FRAMEWORK_NOT_MODELLED`.

**Async:**
- `creates_coroutine` / `creates_generator` (the body does not run);
- `awaits`;
- `invokes_until_first_await` for JS async functions;
- `asyncio.create_task` / `gather` registers, with `ORDER_NOT_DETERMINED`.

**FastAPI:**
- `handled_by` from Stage 14 endpoints;
- `injects` setup and teardown edges, with the A0-verified version table. Alternatives are per row; a declared range spanning a semantics boundary yields `SEMANTICS_ALTERNATIVE`;
- background tasks as `registers` with `fastapi_background`;
- exit-specific `response_outcome`s:
  - the route `status_code`;
  - the POST default of 200;
  - a returned `Response` used as-is;
  - dynamic `response.status_code`;
  - `HTTPException`;
  - the framework 422;
  - unhandled → `status_unknown` with a 500 alternative, never an unconditional 500.

**Application-wide response conditions.** These are attributed to the whole repository, not per application:
- `MIDDLEWARE_PRESENT_NOT_MODELLED` on every outcome;
- `EXCEPTION_HANDLER_TABLE_NOT_STATIC` and `EXCEPTION_HANDLER_REGISTERED_NOT_LINKED` on exceptional outcomes;
- `VALIDATION_HANDLER_REGISTERED` on the 422.

**SQLAlchemy** (`sqlalchemy-ops.v2`):
- the operation classes `performs` (query, stage, dml, flush, commit, rollback, close);
- `on_table` through Stage 14 `maps_table`, including for an inline `session.add(M(...))`;
- raw `text()` SQL gets `RAW_SQL_NOT_PARSED` and **no** table.

**React:**
- registration versus invocation: DOM `registers` with `scheduled_by` (`ELEMENT_MUST_BE_MOUNTED`);
- `useCallback` does not invoke;
- `useMemo` invokes in the render phase;
- `useEffect`/`useLayoutEffect` register with `react_commit`;
- `setState` → `schedules_render` (`RENDER_MAY_BAIL_OUT`, `UPDATES_MAY_BE_BATCHED`);
- navigation and storage effects.

**Libraries:**
- TanStack Query (D14): `mutate`/`mutateAsync` → `tanstack_mutation`, with rejection propagation and `onMutate`/`onSuccess`/`onError`/`onSettled` continuations, plus `useQuery` and a literal `enabled`;
- react-hook-form `handleSubmit` (D15).

**HTTP:**
- Stage 14 request sites are stitched to callsites by exact span (`issues_request`);
- `calls_endpoint` branches are copied with their Stage 14 conditions;
- hey-api/openapi-fetch descriptor sites (D10) are matched by method and segment;
- `responds` maps outcomes back to `fulfilled`/`rejected` continuations through the wrapper's `!response.ok` guard (`OK_GUARD_RECOGNIZED`).

**Every HTTP hop is `inferred`** and carries `HTTP_LINK_INDEPENDENT_EVALUATION_PENDING` (D1). The contract records `api_matching_evaluation: not_human_evaluated`.

**Integrity and freshness (INV-4–INV-6):**
- element digests cover every semantic field plus the evidence set;
- evidence items cite rows by id and row digest;
- every node and edge read re-verifies the returned elements, their evidence and each cited row (flow evidence rows, Stage 14 elements, import rows, framework and module facts), and fails closed with 409 `FLOW_INTEGRITY_FAILED`;
- the run has a `graph_digest` and a `portable_digest`;
- an unchanged rebuild short-circuits;
- a stale input gives 409 `FLOW_STALE`, while history stays readable and labelled.

**HTTP routes** under `…/flow`: `POST|GET ""`, `/runs[/{id}]`, `/node?key=`, `/edges`, `/diagnostics`, `/integrity`.

**CLI:** `buildlens flow build|status|node|integrity`. The CLI was **not exercised** at this gate.

### Completion pass (the items that were open at the first B checkpoint)

**Generators** (`py-async-semantics.v2`):
- `for x in g` over a generator iterator → `iterates` (`GENERATOR_FINALIZATION_NOT_MODELLED`);
- `next(g)`, `g.send()` and `__next__` → `resumes`;
- `g.throw()`, `g.close()` and the async forms → `throws_into` (`close` throws `GeneratorExit`);
- `with m()` over a `@contextmanager`/`@asynccontextmanager` generator → two `resumes` edges. `enter` runs to the first `yield`; `exit` resumes normally or throws in (`SUPPRESSION_DECIDED_BY_GENERATOR`), and the value bound by `as` is the yielded value, which is unknown;
- generator state is not tracked (`GENERATOR_STATE_NOT_TRACKED`).

**Python resolution:**
- `DECORATOR_MAY_WRAP` (tier `inferred`) for any decorator outside the transparent set: `staticmethod`, `classmethod`, `property`, `abstractmethod`, `dataclass`, `overload`/`final`/`override`, `functools.wraps`, context managers, and framework registration decorators;
- `NoReturn`/`Never` return annotations (`NORETURN_ANNOTATION_TRUSTED`) and callees whose normal exit is unreachable (`CALLEE_NEVER_RETURNS`) give `normal_continuation: false`, as do `sys.exit` and `os._exit` (`PROCESS_EXIT`);
- `Protocol` receivers get structural `dispatch_candidate`s (`PROTOCOL_IMPLEMENTATION_CANDIDATES`): only repository classes defining every public protocol method, capped by fan-out. ABC subclasses use the existing override candidates;
- `STAR_IMPORT` for names a module may receive from `import *`;
- `CONDITIONAL_IMPORT` when an import is one of several reaching definitions (`try`/`except ImportError`, `if`).

**SQLAlchemy:** `with session.begin()` / `begin_nested()` / `sessionmaker.begin()` → a `transaction_boundary` that commits on normal and rolls back on exceptional completion. Only body operations on the **same session identity** get `in_transaction`: the same reaching definitions of the receiver, or the item's `as` value. Other operations get no claim.

**FastAPI:**
- **Exceptions below the handler.** An `HTTPException` raised in an injected dependency (`RAISED_IN_DEPENDENCY`) or in a callee within three resolved hops (`RAISED_IN_CALLEE`) becomes a handler outcome with its status and a `raises_to` edge from the raising callsite. These carry `PROPAGATION_THROUGH_CALLERS_NOT_VERIFIED`.
- **Static exception handlers.** `@app.exception_handler(E)` and `add_exception_handler(E, h)` registrations with a resolvable `E` and `h` are linked by `raises_to`:
  - `HTTPException` and `RequestValidationError` outcomes link by type;
  - unhandled outcomes link to every other handler, with `EXCEPTION_TYPE_MATCH_NOT_DETERMINED`;
  - registrations that do not resolve give `EXCEPTION_HANDLER_REGISTERED_NOT_LINKED`;
  - table-driven loops keep `EXCEPTION_HANDLER_TABLE_NOT_STATIC`.
- **The response boundary.** A `framework_response_send` scheduler joins three kinds of `response_boundary` edge: every outcome is sent through it; yield-dependency teardowns record the side (`sides`, from the version table); background tasks run after the send (`relative_to_teardown`).
- **Background tasks:**
  - `BACKGROUND_TASKS_DROPPED_BY_RESPONSE` when the handler returns a `Response` constructed with its own `background`;
  - `BACKGROUND_TASK_USES_DEPENDENCY_RESOURCE` when a task argument is provided, directly or transitively, by a yield dependency.
- **Dependencies:**
  - `DEPENDENCY_SIDE_EFFECTS_ON_422_POSSIBLE` on the 422 outcome of a handler with dependencies;
  - `use_cache` recorded on every setup `injects` edge (`USE_CACHE_UNKNOWN` when it is not literal).

**React, TanStack Query and JavaScript async:**
- a function returned by an effect is registered as its cleanup (`react_commit:cleanup`, `CLEANUP_BEFORE_RERUN_OR_UNMOUNT`);
- a state setter → `may_rerun_effect` for effects of the same component whose dependencies list that state, or that have no dependency array. It never says "runs" (`DEPENDENCY_CHANGE_NOT_DETERMINED`, `RENDER_MAY_BAIL_OUT`), and `schedules_render` now names the owning component and state;
- `useQueryClient().invalidateQueries`/`refetchQueries`/`resetQueries` → `may_refetch` (`QUERY_KEY_MATCH_NOT_DETERMINED`);
- `setTimeout`/`setInterval`/`requestAnimationFrame`/`queueMicrotask` and `.then`/`.catch`/`.finally` reactions → `registers` with `timer`/`animation_frame`/`microtask` (`ORDER_NOT_DETERMINED`). Promise reactions add `THENABLE_ASSUMED`.

**TS construction** (`ts-construction.v1`):
- `new C()` → `constructs`, then the class's constructor. A class without one runs its base's (`IMPLICIT_SUPER_CONSTRUCTOR`, along the `extends` chain);
- `super(...)` resolves to the base constructor (`SUPER_CONSTRUCTOR`);
- a constructor that returns a value gives `CONSTRUCTOR_RETURN_OVERRIDE` (tier `unknown`);
- instance field initialisers are counted but not lowered (`FIELD_INITIALISERS_NOT_LOWERED`).

**Contract bump.** The extractor contracts are now `python-flow.v2` (return annotations, star imports) and `ts-flow.v2` (class `extends` and the instance-field count). Every evidence run under the older contracts is stale and is re-extracted.

### Remaining approximations (labelled in the model, not open plan items)

- Exception propagation through intermediate callers' `except` clauses is not verified.
- Generator state, finalisation, and unhandled promise rejections are not modelled.
- Protocol matching is by method names, not signatures.
- TS instance field initialisers are not lowered.
- The `@contextmanager` value bound by `as` is unknown.

Done before encoding, as the plan requires: the TanStack Query 5.104.1 and react-hook-form 7.88.0 callback semantics were verified against their pinned source (draft `.buildlens-eval/runs/stage15-milestone-b-20261101/library-semantics.v1.draft.json`). Every FastAPI version pinned in the corpora is within 0.105.0–0.157.0.

Path-level database effects (`staged`, `commit_reached`, `COMMIT_AFTER_RESPONSE_SENT`) are computed along trace paths, so they belong to Milestone C.

### Measured (local, 2026-11-01, single run; indicative, not the S7 protocol)

The run was on BuildLens `5c65617` (`git archive`), in a disposable database. Results are in `.buildlens-eval/runs/stage15-milestone-b-20261101/flow_model.buildlens-5c65617.json`.

**Output:**
- The model run's outcome is `partial`, because 5 artifacts are partial in the evidence run.
- The model has 37,444 nodes and 80,923 edges. By tier: observed 31,552, derived 22,983, inferred 5,275, unknown 21,113.
- 90 handlers, 295 response outcomes, 92 `injects` edges and 15 `on_table` edges.
- From the completion pass: 126 `resumes` (`@contextmanager` `with` blocks), 204 `raises_to`, 298 `response_boundary`, 57 `may_rerun_effect`, and 107 `dispatch_candidate` edges (18 before, mostly from `Protocol` store receivers). There were no `iterates` or `in_transaction` edges: BuildLens has no `for` over a repository generator and no `with session.begin()`.
- 60/60 Stage 14 request sites stitched by span (0 unstitched), with 58 `calls_endpoint` branches.
- 0 diagnostics.

**Cost:**

| Step | Result |
|---|---|
| Flow build | 14.2 s |
| Unchanged rebuild | 0.68 s (short-circuits to the same run) |
| Full integrity | 6.8 s; verified, 0 failures |
| Parent peak RSS | 373 → 853 MB across the build |
| Flow tables | about 124 MB (nodes 22 MB, edges 78 MB, evidence items 24 MB) |

Full integrity took 25.7 s before the bulk preload of cited evidence rows.

The most frequent `unresolved` reasons are `BUILTIN` 4,883, `EXTERNAL_PACKAGE:sqlalchemy` 3,555, `CALL_RESULT_UNKNOWN` 2,456 and `RECEIVER_TYPE_UNKNOWN` 1,234.

### Defects found and fixed during Milestone B verification

1. `on_table` targeted a `db_table` node that was never emitted.
2. **A forbidden link:** descriptor matching read a non-existent `method` attribute, so a POST descriptor also linked to a GET endpoint. Stage 14 nodes store `methods`.
3. A `__new__` that returns another type still produced a conditional `__init__` edge.
4. An inline `session.add(M(...))` attributed no table.
5. A declared range spanning only the 0.106/0.118 boundaries was not marked `SEMANTICS_ALTERNATIVE`.
6. **A runtime crash on any truncated scope:** `diagnose(code=…)` collided with the code parameter.
7. Edges emitted without attributes stored `None`.
8. **The portable digest did not reproduce.** Stage 14 element digests cover catalog identity ids, so citations now carry an id-free `portable` projection.
9. Full integrity re-fetched cited rows once per citation.
10. In the completion pass: `schedules_render` named the nested callback rather than the component that owns the state, and `super(...)` callsites had no label.

### Verification (Milestone B gate, Python 3.14.6, disposable PostgreSQL 16 / pgvector 0.8.6)

**`tests/integration/test_flow_model.py` (8).** Two designed fixtures check every family above against a designed verdict: `tests/flow_support.py::DESIGNED_REPOSITORY` and `SEMANTICS_REPOSITORY` (the completion pass). The second also includes non-links: a repository `begin()` is no transaction, and a list loop is not generator iteration. Beyond that, the module covers:
- **Adversarial non-links, all with zero edges:**
  - an unimported same-name function;
  - a homoglyph;
  - `getattr`;
  - a `.GET` on an unrelated object;
  - `useMutation` from another package;
  - a local `handleSubmit`;
  - a POST descriptor against a GET endpoint;
  - raw SQL.
- **The tamper suite**, detected by both the verified reads and `integrity`:
  - a node field;
  - an edge tier;
  - a cited evidence item;
  - a cited flow callsite row (`CITED_ROW_CHANGED:flow_scopes`);
  - a cited Stage 14 node;
  - a deleted edge.
- **INV-5:** an unchanged rebuild short-circuits. A second project over identical bytes, in the same database with every id different, gives an equal `portable_digest` and a different `graph_digest`. Reproduction across two separate databases was not run.
- **INV-3:** every pre-existing table is byte-identical over its pre-existing columns.
- **INV-4:** the inputs stay fresh; a later structural run makes current reads fail closed while history stays readable.
- **The HTTP contract.**
- **A truncated-scope regression test**, where the run is `partial`.
- **The 0018 downgrade refusal.**

**`tests/unit/test_flow_model_rules.py` (20):** the FastAPI version table per row, plus ranges, unknown versions and precedence; constraint satisfaction; descriptor method and segment matching; tsconfig `paths` with `extends` and JSONC; and workspace dist→source mapping with and without literal evidence.

**Gate selection:** the flow unit tests (A and B), `test_flow_evidence`, `test_flow_model`, `test_schema`, `test_upgrade` and `test_persistent_embeddings`: **71 passed**, after the completion pass. The migration round trip was not repeated, because the completion pass changed no migration or model.

**Migrations:**
- single head `0018_flow_model`;
- the empty-database round trip 0018 → 0016 → 0018 leaves 0 `flow_` objects after the downgrade and gives an identical `pg_dump -s` (3,958 lines), apart from the `\restrict` tokens.

**Static checks:** ruff check and format are clean; **mypy is clean on `src`** and on the new test modules.

**Not run**, under the proportional-verification workflow: the full backend suite, the Python 3.11 suite, the frontend suite (there are no frontend changes) and the baselines. Those belong to stage closure.

### Deviations from the plan (for owner review)

- **Evidence storage.** `flow_nodes.evidence` and `flow_edges.evidence` are JSON arrays of evidence-item digests, verified on read. There is no separate `flow_evidence_links` FK table, and `flow_evidence` is named `flow_evidence_items`.
- **Module layout.** The builder is split into `flow_python.py`, `flow_typescript.py`, `flow_builder.py` and `flow_inputs.py`, rather than the per-framework modules named in §2.4.
- **Evidence contract bump.** The flow evidence contract is now `flow-evidence.v2`: it adds module facts (`module-facts.v1`; tsconfig and package manifests read as data for D9 and D13). Milestone A evidence runs are therefore stale under the new contract and are rebuilt.
- **Descriptor base URL.** `ts-flow.v1` keeps literal text only for root-relative paths, so absolute `baseUrl` origins are not retained. Descriptor links carry `BASE_UNKNOWN` unless the factory's `baseUrl` is a root-relative literal (`BASE_FROM_FACTORY_LITERAL`).
- **Attribution scope.** Application-wide response conditions are attributed to the whole repository, not to each FastAPI application: a stated over-approximation.
- **The D10 factory-shape recognizer**, described in the A0 correction above.

## Milestone C — the `FlowTrace` engine (implemented; committed locally)

A `FlowTrace` (`flow-trace.v1`) is computed on demand. It walks one verified flow model run plus the scope CFGs it cites. Like the model, traces read PostgreSQL only (INV-1); a test deletes the repository before tracing. A trace is persisted only when pinned (D4), in `flow_trace_records`, so no migration was needed.

**Code:**
- `domain/flow_trace.py`: the contract, bounds and the sound fragment;
- `application/flow_trace.py`: the pure engine;
- `application/flow_trace_service.py`: verified reads, entries, pinning and the per-run graph cache.

### Entry points (`flow-entry-points.v1`)

The builder now emits `entry_point` nodes with `enters` edges (model contract `flow-model.v2`). The kinds are:
- `http_endpoint`: a Stage 14 endpoint → its handler;
- `cli_command`: Typer or Click command decorators;
- `asyncio_run`;
- `dom_event`: a handler registered on an intrinsic element;
- `effect`: `useEffect`/`useLayoutEffect`;
- `module`.

Scope nodes also carry the symbol's stable identity, which the portable digest excludes because it is database-assigned.

**Natural-language discovery** (`/entries?q=`) runs the existing `RetrievalService` (lexical by default) and maps hits to scopes by symbol identity. It then walks backward to the entry points that reach those scopes, within 8 hops. Candidates are ranked by hit rank, then distance, then key, each with its reason. **The user chooses**; no entry or edge is ever selected automatically.

**Not covered:** route-component entries (TanStack Router and the Next.js App Router remain the D16 gap).

### The walk and its four verdicts

**Forward walk** (bounded depth-first, k=1 call context), in this order:
1. `injects` setup;
2. callsites in CFG order: invocation, scheduling, effect and request edges;
3. scope-level registrations;
4. exit outcomes.

**Edge handling:**
- Requests cross by `issues_request` → `calls_endpoint` → `handled_by`.
- Outcomes cross the response boundary to teardown and background lanes, then `responds` back to the frontend continuation.
- Edges whose body does not run (`creates_*`) and effects are leaf steps.
- A revisit is a `shared` step; recursion is a `cycle_ref`.
- A generator dependency's body is split at its `yield`: setup runs to the `yield`, teardown resumes after it (`finally` copies count).

**Per step**, the four verdicts are kept apart:
- **L1:** the edge's tier, rule and conditions;
- **L2:** CFG reachability. The verdicts are `reachable_in_model`, `unreachable_static`, `unreachable_after_non_returning_call`, or `reachable_if_raises` with the raising nodes;
- **L3:** the path condition, as the disjunctive normal form of control-dependence guards accumulated from the entry (at most 16 terms; `approximate` beyond that);
- **L4:** feasibility.

**Lanes:**
- Each scheduler gets a lane, marked `ORDER_NOT_DETERMINED`.
- Three lanes carry a stated order:
  - the main lane is sequential by CFG within a scope;
  - teardown is ordered by the FastAPI version table;
  - TanStack mutation callbacks are ordered by their contract.

**Layers:** every hop is assigned one of Entry, Frontend, HTTP, Route, Dependencies, Service, Data, External or Scheduler.

**Ambiguity** produces segments, one per alternative (`AMBIGUOUS_CANDIDATE`, `SEMANTICS_ALTERNATIVE`); the engine never chooses.

**Gaps:**
- Unresolved callsites are gaps, except builtin, global and external-package calls, which are listed as `boundaries`;
- truncated and opaque scopes are gaps;
- any gap makes the status `incomplete_evidence`;
- truncation by depth, hop limit or deadline is never reported as `complete`.

**Database effects (`sqlalchemy-ops.v2`, path level):**
- Each `stage`/`dml`/`flush` operation is classified as `staged`, `sent_uncommitted` or `commit_reached`. `commit_reached` requires one of:
  - a commit on the same session (same receiver and reaching definitions, CFG-reachable after the operation);
  - a transaction scope;
  - a session injected by the dependency that commits (`SESSION_IDENTITY_BY_PARAMETER`).
  Otherwise the state is `commit_reached_session_unknown`.
- The commit's position relative to the response is `COMMIT_BEFORE_RESPONSE`, `COMMIT_BEFORE_RESPONSE_SEND` or `COMMIT_AFTER_RESPONSE_SENT`.
- The claim is always "a commit is reached on the path; it may still raise", never "saved".

**Backward walk:** incoming invocation, resolution and request edges up to entry points. A scope with no callers gets a coverage-gated absence claim: `no_callers_in_model` only when all of these hold:
- coverage is complete;
- there are no dynamic callsites in that language (unknown callees, callable parameters, unbound prop callbacks, star imports);
- no unresolved callsite has the same name;
- the scope is not a module.

Otherwise the verdict is `ABSENCE_NOT_DETERMINABLE` with its reasons.

**Structural path** (`/path`): one shortest witness, labelled `L3_structural_witness` and "other paths may exist". Absence within bounds is never proof that no path exists.

### Sound fragment (`flow-feasibility.v1`)

**Variables** are (scope, local name, reaching definitions). A variable is admissible only when all of these hold:
- the scope has no dynamic features or `with`;
- the name is not `nonlocal` or `global`;
- no nested scope rebinds it.

**Atoms in the fragment:**
- **Python:** `is None` / `is not None`, and `is True`/`is False` (identity);
- **JS:** `===`/`!==` against a primitive literal, `== null`/`!= null`, `typeof`, and truthiness (`HOST_OBJECT_FALSY_EXCLUDED`);
- **Python `==` and truthiness:** only when every reaching definition is a literal or an unshadowed builtin value-type constructor (annotations do not count);
- **literal knowledge:** a local whose only reaching definition is one literal has that value.

**Opaque atoms** (fresh unknowns, never used to refute):
- `isinstance`/`instanceof`;
- calls, attributes and getters;
- loose `==`;
- overridable operators;
- loop and case outcomes.

**Branch rule:** one activation of a scope takes one outcome at a branch outside every loop, so opposite outcomes of the same branch on one path contradict. This is how a background task registered after `if not name: raise …` is refuted on the 400 path.

**Decision:** an exact per-variable domain intersection. Cross-type numeric equality is undecided. A disjunction is refuted only when every term is and the guard set is not approximate. Every verdict lists its assumptions.

**Not in this version:** `isinstance` refutation (the plan allows it when the class's metaclass and hierarchy resolve), and the lab z3 experiment (Milestone F).

### Integrity, reproducibility and pinning

- **INV-6:** every trace and path re-verifies the hops' nodes, the steps' edges, their evidence items and every cited row. It also checks that the graph-cache digests equal the stored ones (`CACHE_DIFFERS`), and fails closed with 409 `FLOW_INTEGRITY_FAILED` (the cache entry is evicted). The verification budget is 25,000 elements (`FLOW_TRACE_TOO_LARGE`).
- **INV-7:** `trace_digest` = sha256 over {contract, graph digest, normalised query, result}. The result includes a `source_digest` over the touched artifacts' fingerprints and the digests of the scope evidence used. Recomputation is byte-identical.
- **Pinning:** `POST …/flow/traces` pins a trace (a trace that hit its deadline is refused, as not reproducible). `GET …/flow/traces/{digest}` re-verifies it as one of:
  - `current`;
  - `stale`, with reasons, when its run is no longer current;
  - `integrity_failed`, when recomputation differs or verification fails.

**HTTP routes:** `GET …/flow/entries`, `/trace`, `/path`, `POST /traces`, `GET /traces/{digest}`. **CLI:** `buildlens flow entries|trace|path|pin|verify`. The CLI was not exercised at this gate.

### Measured (local, 2026-11-01, single run; indicative, not the S7 protocol)

The run was on BuildLens `5c65617`, using `.buildlens-eval/runs/stage15-milestone-c-20261101/` (`flow_trace.buildlens-5c65617.json`, with the script alongside).

**Entry points:**

| Kind | Count |
|---|---|
| `http_endpoint` | 90 |
| `dom_event` | 107 |
| `cli_command` | 53 |
| `effect` | 40 |
| `asyncio_run` | 31 |
| `module` | 335 |

**Sample:** up to 40 entries per kind for `http_endpoint`, `dom_event`, `cli_command` and `effect`, each traced forward and backward. That is 320 traces.

**Results:**
- **Status:**
  - all 160 backward traces are `complete`;
  - of the 160 forward traces, 6 are `complete` and 154 are `incomplete_evidence`;
  - none hit the deadline, the hop limit or the depth limit.
  The forward gaps are the model's unresolved callsites: mostly `RECEIVER_TYPE_UNKNOWN`, `CALL_RESULT_UNKNOWN`, `ANNOTATION_UNRESOLVED` and `MRO_INCOMPLETE`.
- **Size:** at most 313 hops per trace (median 2, because backward traces from entries are short).
- **Feasibility:** 10,641 steps `consistent_in_fragment`, 34,948 `undetermined`, and 0 refuted. BuildLens has no contradictory guards within the fragment.
- **Reproducibility:** every forward trace recomputed byte-identically.

**Latency** (wall time per trace, including verification and the upstream freshness check):

| Traces | Median | Max |
|---|---|---|
| Forward, by entry kind | 0.56–0.85 s | 2.2 s |
| Backward | ≈ 0.5 s | — |

- The first trace, which also loads the graph, took 0.64 s.
- About 0.78 s of a typical trace is the Stage 13/14 freshness check.

### Verification (Milestone C gate, Python 3.14.6, disposable PostgreSQL 16 / pgvector 0.8.6)

**`tests/integration/test_flow_trace.py` (6):**
- the designed whole-path oracle from a DOM click to the database and back: journey order, layers, lanes, `responds` continuations, the database effect, and the branch oracle that refutes the 400-path background task;
- byte-identical recomputation through a fresh cache;
- no execution wording;
- the feasibility oracles of plan §6.2 (S2), each with its designed verdict:

  | Case | Verdict |
  |---|---|
  | `v is None` then `is not None` | refuted |
  | literal-typed `==` | refuted / consistent |
  | overridden `__eq__` | undetermined |
  | JS `===` and `typeof` contradictions | refuted |
  | loose `==` | undetermined |
  | a getter | undetermined |
  | a call evaluated twice | undetermined |
  | a closure-rebound variable | undetermined |

- a commit in yield-dependency teardown → `COMMIT_AFTER_RESPONSE_SENT`;
- absence gating in both directions: Python gives `no_callers_in_model`, TS gives `ABSENCE_NOT_DETERMINABLE` (dynamic callsites);
- the backward walk to the entry;
- the structural witness;
- pinning: `current` → tampered `integrity_failed` → restored `current`, with zero source reads;
- the HTTP contract, including natural-language discovery and stale pins;
- stale models block traces.

**`tests/unit/test_flow_feasibility.py` (6):** the fragment's contradiction rules, `consistent` only when everything is decided, opaque atoms never refuting, the branch rule, disjunction and approximation semantics, and the canonical digest.

**Gate selection:** the flow unit tests (A–C), `test_flow_evidence`, `test_flow_model`, `test_flow_trace`, `test_schema`, `test_upgrade` and `test_persistent_embeddings`: **83 passed**. ruff and mypy are clean.

**Defects found and fixed during the C work:**
- teardown statements in `finally` were missed, because only one lowered copy was checked;
- external-instance calls were counted as gaps;
- background tasks lacked their registration guard;
- the path search ignored effect edges;
- the 415 guard missed `/flow/traces`;
- the portable digest included the new identity attribute;
- child-scope lookup scanned every node, and loop membership was recomputed per branch.

### Deviations from the plan (for owner review)

- **Bounds:** `fanout` is recorded in the query but the walk relies on the model's fan-out cap. Hiding refuted segments is left to the UI; there is no query option.
- **Pinned digest scope:** the pinned record is looked up by digest within the snapshot. The newest matching run's record is the one re-verified.
- **Upstream cost:** about 0.78 s of a typical BuildLens trace is the existing Stage 13/14 freshness check (processing, catalog and architecture status), not the engine.

