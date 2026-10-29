# Stage 14 — Architecture Intelligence

Stage 14 adds an immutable, evidence-backed **architecture model** on top of the Stage 13
semantic catalog. It is deterministic and uses no LLM, model download or network access. It
reads PostgreSQL only and never executes or reads repository code during a build.

- **Branch:** `stage14/architecture-intelligence`, from `main @ 7589e69`, which is code-equal to `b9811f9`.
- **Plan:** revision 2.1 (revision 2 plus contract addendum C-1…C-6).
- **Milestones:**
  - A: module dependency graph (`1bfb88c`).
  - B: literal evidence and API architecture (`076da6e`).
  - C: integrations, database, frontend routes, subsystems and declared rules (`fdc6761`).
  - D: evaluation, hardening, performance and documentation (`b8b816b`).
  - Remediation R01–R05 of the independent technical review (see "Review remediation").

## What it is

An **architecture run** is a projection of one Stage 13 catalog run plus the structural rows
that catalog cites. Runs are snapshot-owned and immutable, and are published under the snapshot lock.

Run states:

- A **stale** run stays readable as labelled history.
- A **failed** run is retained with its code.
- **Current reads** fail closed with `ARCHITECTURE_REQUIRED` or `ARCHITECTURE_STALE`. Nothing rebuilds automatically.

Three axes are kept apart (C-4):

| Axis | Values | Carried by |
| --- | --- | --- |
| Evidence tier | `observed`, `derived`, `inferred`, `unknown` | Graph elements, diagnostics, query results |
| Declared intent | Owner rule-set versions (`forbid`, `layers`) | `architecture_rule_sets` (no tier) |
| Conformance outcome | `not_satisfied`, `satisfied_in_observed_graph`, `not_determinable`, `selector_matches_nothing` | Rule results, whose witness edges keep their own tiers |

Inferred elements carry named conditions, never a numeric confidence. Absence claims are
coverage-gated (`absence-gating.v1`): when coverage is incomplete, the claim becomes
`ABSENCE_NOT_DETERMINABLE` with the gaps that block it.

## Contracts

The run's `architecture_contract` records every contract below. Changing any of them makes
existing runs stale (`ARCHITECTURE_CONTRACT_CHANGED`).

| Contract | Meaning |
| --- | --- |
| `architecture-model.v2` | Node, edge, component and diagnostic vocabulary; v1 runs (pre-literal) are stale history |
| `architecture-integrity.v1` | Complete element digests (every semantic field and the evidence-link set); same-run composite FKs; cache invariants I-C1…I-C5 (I-C2 also binds the run row's coverage, R04); fail-closed authoritative reads; `/integrity` inspection |
| `import-classes.v2` | Classes `unconditional`, `presumed_unconditional`, `conditional`, `elision_dependent`, `type_level`, `deferred`; class sets S0–S4 describe the static import relation only |
| `scc.tarjan-iterative.v1`, `containment.v1`, `aggregation.v1` | Cycles per class set and level; containment hierarchy; aggregated `depends_on` edges |
| `direction-diagnostics.v1` (D) | `PRODUCTION_IMPORTS_TEST`, `RUNTIME_IMPORTS_MIGRATION`, `CROSS_WORKSPACE_RELATIVE_IMPORT`: inferred from path conventions only |
| `python-architecture-literals.v1`, `ts-architecture-literals.v1` | Additive extractor passes (never in the catalog contract or processing fingerprints); rows `architecture-literal-rows.v2`; sanitisation `url-origin-path-only.v1`; the TS pass records `callee_scope` under `ts-callee-scope.v1` (R03) |
| `router-binding.v1`, `http-sinks.v2`, `client-wrapper.v1`, `client-folding.v1` | Full endpoint paths through mounts; request sites only from recognised sinks or qualified wrappers whose root name no enclosing scope declares (v2, R03); URL folding |
| `api-matching.v3` (D) | Destination classes first; product-automaton intersection of the two path patterns; no unconditional "matched" state. v3: typed converters accept only their own characters |
| `manifest-facts.v1`, `package-matching.v1`, `database-view.v1`, `frontend-routes.v1`, `layer-candidates.v1`, `functional-slices.v1`, `absence-gating.v1`, `stdlib-lists.v1` | Milestone C views |
| `architecture-rules.v1` | Owner-declared rules (API/CLI only, never from repository files), append-only versions, evaluated per request, never enforced |
| `architecture-evaluation.v1` (D) | The pre-registered evaluation protocol (unchanged by the remediation); scorer output `architecture-evaluation-score.v2` and human labels `architecture-api-labels.v2` (R01, R02) |

## Views, API and CLI

### Views (`/architecture`)

- **Overview, Modules:** containment tree, DSM, neighbourhood diagram, reachability and path with search status.
- **Cycles and Diagnostics:** with class-set chips.
- **API:** endpoints with full paths; request sites with every branch and its conditions.
- **Integrations:** imported versus declared packages; unbound hosts.
- **Database:** SQLAlchemy and Alembic tables merged by name, foreign keys and consumers.
- **Frontend:** routes and the components they render.
- **Subsystems:** layer candidates, functional slice candidates and declared-rule conformance.

The inspector shows tier badges, conditions, verification state, verified source excerpts and witness paths.

### HTTP

The architecture routes live under `/api/v1/projects/{p}/snapshots/{s}/architecture`:

```text
POST ""          GET ""          GET /runs        GET /runs/{run_id}
GET /overview    GET /tree       GET /node        GET /edges
GET /dependencies                GET /path        GET /cycles       GET /cycle
GET /matrix      GET /neighbourhood               GET /diagnostics  GET /api
GET /views/{integrations|database|frontend|subsystems}
GET /coverage    GET /evidence/{digest}           GET /integrity
GET /rule-results
```

Project-scoped routes: `POST|GET /api/v1/projects/{p}/architecture-rules` and `GET …/architecture-rules/versions`.

Every read accepts `run_id`. A response first re-verifies every element it returns (and the constituents of returned paths, witnesses, matrix cells and conformance results): element digests, then evidence. Totals, histograms and negative claims are computed from the structure verified when the graph was cached, bound to the current rows by the per-table fingerprint and to the run row's coverage (I-C2, I-C4).

### CLI

```text
buildlens architecture build | status | overview | deps | path | cycles | diagnostics | api |
                       view | coverage | verify | export --format json|dot
buildlens architecture rules set | show | check
buildlens architecture eval preregister | module-units | module-score | api-packet |
                            labels-template | api-score
```

## Lifecycle (F10)

1. **Pre-literal runs** (structural runs without the literal pass):
   - Python module-scope imports are `presumed_unconditional`.
   - API paths, client calls, Alembic names and frontend routes are `unknown`, with `LITERALS_NOT_EXTRACTED`.
2. **Gaining literals** takes explicit steps: a new structural run, then a catalog build, then an architecture build.
3. **Literal coverage** is tracked per artifact: `extracted`, `partial`, `failed`, `not_current`, `not_extracted` or `not_applicable`.
4. **A literal contract change** excludes the stale rows, and runs that used them become stale (`LITERAL_CONTRACT_CHANGED`).

The upgraded Stage 11 corpus (below) is a real pre-literal example:

- 138 files `not_extracted`;
- 1,867 `presumed_unconditional` statements;
- 83 `deferred` statements.

## Milestone D: evaluation (`architecture-evaluation.v1`)

### Pre-registration

- **Status:** frozen before any scored run.
- **Approval:** approved by the owner (Driv1216) on 2026-10-29, unchanged from the draft, with D4 and D5 at their plan defaults.
- **Digest:** `0dd8e3b184e41ec208f3bc5bb5ed8833a5aa3162eea0042309d6111242409450`.
- **Location:** `.buildlens-eval/reviews/architecture-evaluation-20261029/preregistration.json`, write-once and read-only (git-ignored, like Stage 13).

**What it fixes:**

- unit definitions, outcomes and metrics, each metric with numerator, denominator and a Wilson 95% interval;
- the zero-denominator rule: undefined fails; an empty stratum makes the suite `inconclusive`;
- thresholds, the oracle, the disagreement codes, the blind-packet design, the sink-recall sample, and the 22 adversarial cases.

**Corpus:**

- `git archive fdc6761`: 425 files, tree sha256 `55e47899…3685`, extracted read-only outside the repository.
- Built through the real HTTP app in a dedicated scratch database (`stage14_eval`), never in `buildlens-stage13-eval`.

**Oracle (lab-only, D4):**

- Script: `backend/scripts/architecture_differential.{py,mjs}`. It imports nothing from BuildLens.
- **Python:** `ast` plus `importlib.machinery.PathFinder.find_spec` over the pre-registered roots `backend/src` and `backend`.
- **TS/JS:** TypeScript 5.7.3 `createSourceFile` plus `resolveModuleName` (Bundler options), with a host confined to the archive.
- Nothing is imported, emitted or executed.

**Scoring** is mechanical: `application/architecture_evaluation.py`, exposed through `buildlens architecture eval …`. It refuses:

- an unapproved, incomplete or text-changed pre-registration;
- another corpus;
- changed packets, sealed answers or labels;
- incomplete labels.

### Results

| Suite | Result | Evidence |
| --- | --- | --- |
| Fixtures (designed oracles) | **Passed** | F-mod 11/11 units (10 correct, 1 correct alias abstention); F-alias 9/9 (4 alias imports abstain, no API candidate through the alias-bound wrapper, absence becomes `ABSENCE_NOT_DETERMINABLE`, no tsconfig chunk cited). The oracle script reproduces F-mod's designed labels 11/11 |
| Self-hosted module edges | **Passed** (re-scored with the corrected scorer v2, R02) | Non-alias stratum: precision 0.998933 (4,682/4,687, Wilson 0.9975–0.9995); recall 0.998933 (4,682/4,687); coverage 0.987152 (4,687/4,748). Python 4,500/4,505 correct, TS/JS 182/182; abstentions 0; alias edges 0 (BuildLens has no alias imports). Five disagreements, all `ORACLE_LIMITATION` (see below), counted against precision; 0 unexplained, 0 system defects; 0 system-only predictions, so v2 equals v1 here (`module.score.r2.scorer-v2.json`) |
| Self-hosted API (human packet) | **Awaiting owner labels**: not passed, not failed | Blind packet r2: 60 request sites, 90 endpoints, 8 sampled files for sink recall, all as hash-verified stored source. The system's answers are sealed separately |
| Adversarial | **Passed** (after two fixes) | 0 false connections (4 designed positives still connect), 0 source reads during build, 0 Python-level outbound connections, sentinels never fire, 13/13 tamper operations detected and then repaired to a clean `/integrity` |

**The five module disagreements.** All are imports inside `backend/lab_envs/langchain_postgres`.

- That directory is a separate Python project, with its own `pyproject.toml` and `src/` root, and runs in its own lab environment.
- `import-resolution.v1` adds `<dir>/src` for every `pyproject.toml`, so it resolves the package there.
- The pre-registered oracle roots do not include that root, so the oracle labels these imports external.
- They are classified `ORACLE_LIMITATION` in `module.explanations.json`. The code is applied by the implementer and is open to owner review; explanations never change the metrics.

**Ineligible units.** 61 of 4,748 units are ineligible:

| Units | Cause |
| --- | --- |
| 27 | Two token-fixture test files screened at structure (`structure_failed`) |
| 28 | Four frontend test files with `PARTIAL_PARSE_ARTIFACT` |
| 4 | A `.buildlens-eval` authoring script, which is outside the snapshot |
| 2 | `ts_differential.mjs`, because `.mjs` is unsupported |

**API packet.** `.buildlens-eval/reviews/architecture-evaluation-20261029/api-packet/` contains:

- `REVIEW.md`;
- `packet.json` (digest `de75b91d…eb10`, unchanged);
- `labels.template.v2.json`, the template to fill. `labels.template.json` is the superseded v1 template: scoring refuses v1 labels (`LABELS_VERSION`).

The sealed answers are in `sealed/api.sealed.json` (mode 0600). Do not open them until every label is written.

The v2 template has these sections, and scoring refuses a file unless every one is complete:

- **Sites:** per site, `statically_determinable`, method, destination class and `targets`. A determinable site names one or more endpoint unit ids of this packet, or exactly `["external"]` or `["none"]`; a site that is not determinable is exactly `["opaque"]`.
- **Endpoints:** per endpoint, `determinable`, and if so its full paths (each starting with `/`) and methods.
- **Sink sample:** per sampled file, `reviewed: true` and the start lines of every HTTP request site in it (an empty list when there are none).

Missing, unexpected or duplicated units, fields, targets or lines are refused before anything is scored.

To write a template for this packet again (it reads the blind packet only):

```bash
buildlens architecture eval labels-template --packet …/api-packet/packet.json --out <new file>
```

To score the filled labels:

```bash
buildlens architecture eval api-score --preregistration …/preregistration.json --packet …/api-packet/packet.json --sealed …/sealed/api.sealed.json --labels <filled labels> --out …/api.score.json
```

Packet r1 was generated from run `949bf65b` before the D4 fixes. It was superseded before any labelling (`superseded/README.txt`); its sealed site answers equal r2's for all 60 sites.

### Hardening found by the adversarial suite (D4)

| Finding | Correction |
| --- | --- |
| `fetch('/api/items/abc')` became a candidate for `GET /api/items/{item_id:int}`: typed converters were compiled as "any segment" | Typed converters (`int`, `float`, `uuid`; Flask `<int:…>`) accept only their own characters; `api-matching.v3`. Unit test added |
| A root holding both `pyproject.toml` and `package.json` produced the same layer scope twice, so the build failed with `ARCHITECTURE_KEY_COLLISION` | One layer candidate per scope node (the npm rule over all containers). A view element produced twice now fails the build closed instead of silently overwriting |
| **Stage 13:** an edited `semantic_facts.rendered_text` still verified. `verify_facts` re-derived the digest but never compared the stored text, which is what readers are shown | `verify_facts` also requires the stored text to equal the re-render (`FACT_DIGEST_MISMATCH` otherwise). Regression test in `test_semantic_catalog.py`. A behaviour change in Stage 13 code (stricter only) |
| Three planned direction diagnostics were missing | `direction-diagnostics.v1` (above) |

### Performance (D5)

Measured on BuildLens `fdc6761` (425 files; 4,715 import units; 6,846 evidence items) on the owner's Mac with local PostgreSQL 16. There are 10 repetitions per query, against placeholder gates of a 30 s build and a 1 s query p95.

**Build:**

| Measure | Value | Gate |
| --- | --- | --- |
| Architecture build | 2.89 s in-process, 3.15 s through the CLI; peak RSS 162 MB | **Passed** |
| Pipeline before it (HTTP) | snapshot 1.9 s, structure 14.2 s, processing 3.2 s, catalog 4.7 s | Not gated |
| Cold graph load (I-C1 full structural verification) | 0.45 s | Not gated |
| Full `/integrity` | 0.81 s (was 3.93 s) | Not gated |

**Queries.** The slowest p95 is 0.915 s, so the 1 s gate **passes**.

| Query | p95 |
| --- | --- |
| `overview` | 0.915 s (was 4.16 s; it verifies every element per I-C3) |
| `matrix` | 0.34 s |
| All other queries | 0.22–0.28 s |

About 0.2 s of every request is the I-C2 per-table fingerprint plus the freshness `select()`.

The overview improvement came from a Stage 13 persistence fix. `verify_facts` loaded each cited row's authority with one `session.get` per row, about 11,000 queries per full verification. The authority rows are now bulk-loaded into the session's identity map first. The results are identical.

### Baselines (D6)

**Stage 11/12** (P5c), on a fresh copy of `buildlens-stage11-postgres-data`. The original was mounted read-only and never started; the copy ran in a new container, `buildlens-stage14-baseline`, on port 55442.

- Upgrade 0009 → 0016: all 14 pre-existing tables are byte-identical after the upgrade and after the catalog and architecture builds.
- The legacy catalog rebuilds with the source reader disabled. Its digest is `39b62524…`, identical to Stage 13's record.
- The architecture build reads no sources:
  - outcome `partial`; 1,950 statements;
  - literals `not_extracted` for 138 files;
  - integrity verified, with 0 failures over 2,692 evidence items;
  - an unchanged rebuild short-circuits.
- `eval validate` (Python 3.11.15): 30/30 cases bound, 0 invalid. The definition digest `43f6401d…` and judgments digest `0a9a4686…` are unchanged.
- `lab parity` (Python 3.11.15, with the documented 60 s search-timeout allowance): passed on the first attempt.
  - 90/90 for retrieval, ranking and packed context;
  - corpus digest `03c559c4…` unchanged.

Records are in `.buildlens-eval/runs/stage14-milestone-d-20261029/`. The baseline volume `buildlens-stage14-baseline-data` is a disposable copy; its container is stopped.

**Stage 13** (P5b):

- `buildlens-stage13-eval` was not started or touched.
- The frozen pre-registration still validates (`ed8c5c91…a888`).
- The frozen sample's digest recomputes (`8f9631bb…13f62b7`).

### Milestone D gate (2026-10-29)

- **Backend:** 407 passed, 2 skipped, 5 deselected on Python 3.14.6 and on 3.11.15, against disposable PostgreSQL 16 / pgvector 0.8.6. Ruff, format and mypy are clean. Golden parity is part of the suite on both runtimes.
- **Frontend:** 42 tests, ESLint and the production build. There are no frontend changes in Milestone D.
- **New tests:**
  - `tests/unit/test_architecture_evaluation.py` (8): empty, abstaining and under-predicting outputs fail; zero denominators; tampering and corpus binding.
  - `tests/integration/test_architecture_evaluation.py` (3): F-mod, F-alias and the blind packet.
  - `tests/integration/test_architecture_adversarial.py` (2): gates, and tamper-and-repair.
  - The typed-converter unit test, the containment-tree root test, and the Stage 13 fact-text regression test.
- **Browser run:** the BuildLens evaluation run through the real API and the Vite dev server.
  - Checked: the overview tree, Modules with the neighbourhood and inspector, API (the `listEntities` example: a conditional candidate with `BASE_FROM_FALLBACK_LITERAL` and `LOOPBACK_ORIGIN_ASSUMED_LOCAL_BACKEND`; the environment branch is `BASE_UNKNOWN`), and Subsystems.
  - No console errors.
  - It found the tree-root defect: parentless view nodes were listed as files. The root now lists containment kinds only.
- **Not done:** hosted CI. A review bundle was built from `b8b816b` for the independent review, which found R01–R05 (below).

## Review remediation (R01–R05)

An independent technical review of `b8b816b` reported five findings. All five were reproduced on the original code with new regression tests, then fixed without changing the frozen pre-registration (digest `0dd8e3b1…9450`), the retained oracle, the system observations or any historical score file.

| Finding | Defect reproduced on `b8b816b` | Correction |
| --- | --- | --- |
| R01 API evaluation integrity | A label file with no endpoint or sink-sample labels scored `passed`; `external_service_labels` was a constant 0; an `external` label became `none`; an unknown endpoint id was accepted | `validate_api_labels` refuses incomplete, unexpected, duplicated or out-of-vocabulary labels (labels v2: endpoints carry `determinable`, sampled files `reviewed`); the sealed answers must cover exactly the packet's units; both hard gates take their values from the sealed answers and labels; `external`, `opaque`, `none` and endpoint targets are scored as distinct labels |
| R02 module precision | Predictions for statements the oracle does not enumerate were left out of precision (1,001 correct plus 100 injected predictions explained as oracle limitations: precision 1.0, `passed`) | System-only emissions in eligible files are `emitted_incorrect` and count in precision (same case: 1,001/1,101 = 0.909, `failed`); only predictions in ineligible files, and units with no prediction, are listed as out of scope. Explanations never change metrics |
| R03 HTTP sink shadowing | `function inspect(fetch) { return fetch("/api/users") }` and 12 other shadowed calls became request sites with `calls_endpoint` edges | The TS pass records each callee root's binding scope (`ts-callee-scope.v1`): parameters, catch and loop bindings, block and hoisted `var` declarations, module declarations, and `with` (unknown). A sink, or a caller of a wrapper, whose root is declared locally is not a request site (`http-sinks.v2`); unknown scope is never a sink |
| R04 cache integrity | After the graph was cached, an edit of the run row's `coverage` was served and used for absence and `search: complete` claims; `/evidence/{digest}` labelled the text from one read `verified` on the strength of a second read | I-C2 now also compares the run's coverage with the coverage verified inside `graph_digest` (mismatch: 409 and eviction); evidence inspection verifies exactly the row it returns |
| R05 frontend stale requests | Dependency, path and evidence requests in the inspector were not abortable, so a late response for an earlier class set or an earlier click replaced the current one | `useRequest` keys its result to its inputs; `useLatestRequest` aborts the previous on-demand request and drops any superseded result; inspector panels remount per run revision and node |

Consequences:

- **Staleness.** The TS pass contract and `http-sinks` changed, so every earlier run is stale (`LITERAL_CONTRACT_CHANGED`, `ARCHITECTURE_CONTRACT_CHANGED`) and stays readable as labelled history. Regaining a current model takes a new structural run, then a catalog build, then an architecture build.
- **Corpus re-check.** The frozen corpus (`fdc6761`) was rebuilt with the remediated code in a separate scratch database. Module units are identical to the retained r2 observations. The request sites are exactly the packet's 60 sites. The per-site system attributes are hash-identical to the sealed run's (compared by hash only, never shown). The new run verifies 6,846 evidence items with 0 failures.
- **API evaluation.** Still awaiting the owner's labels; the existing blind packet r2 stays valid.

## Deviations from the plan (for owner review)

- **Milestone B:**
  - an additional literal kind `type_import`;
  - two import hops (one re-export hop) for router binding;
  - a strict segment-bounded placeholder assumption with recorded alternatives;
  - a `method_unknown` outcome;
  - `ROUTER_NOT_MOUNTED` gated, otherwise `MOUNT_NOT_DETERMINABLE`;
  - parameter-only wrappers qualify.
- **Milestone C:**
  - no CLI sub-app seeds for slices;
  - a restricted in-house glob instead of `pathspec`, whose `gitwildmatch` is deprecated;
  - a vendored Node built-ins list;
  - Python-package layer scopes;
  - literal-row contract v2.
- **Milestone D:**
  - Scored units are import rows (one per imported name for Python `from` imports, one per statement for TS/JS), not "statements". They are pre-registered in `UNITS`.
  - Outbound detection covers Python-level sockets. The PostgreSQL driver's own connection to the test database is excluded by port.
- **Not implemented** (§10 lists them): `NO_OBSERVED_INBOUND_DEPENDENCIES`, `NO_OBSERVED_STATIC_CALLER` and `LIMIT_TRUNCATED`.
  - Graph limits fail the run closed, so `LIMIT_TRUNCATED` cannot arise.
  - The two absence diagnostics would need per-module gating rules not yet designed. They are left out rather than emitted ungated.

## Known limits

- **No call graph, flow tracing or impact analysis.** Stage 14 is the static substrate for Stages 15 and 16.
- **Aliases** (`@/`, `~/`, `#`, `/`) are always `unknown`: no tsconfig or Vite config is read (C-6).
- **Import-name ≠ distribution-name matching** stays `unknown`. Env-var reads and Python outbound URLs are not covered.
- **Functional slices** use imports, names and API links. Exclusive reachability over imports over-approximates coupling.
- **Python guard context** is syntactic only: aliased `TYPE_CHECKING` sentinels are `conditional`.
- **The integrity contract** detects inconsistent tampering. An actor who recomputes every digest, including the run's `graph_digest`, is out of scope.
