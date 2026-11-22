# Stage 15 — Part F evaluation

Part F evaluates the implemented flow tracer; completing an evaluation does not establish
that its quality gates passed. The retained execution record is
`.buildlens-eval/runs/stage15-f-20261112/` (git-ignored). The final results and remaining
limitations are recorded in `FLOW_TRACING.md` and `CURRENT_STATE.md`.

## Review authority and limits

On 2026-11-12 the owner requested autonomous execution, including acting as the reviewer.
That authorizes **AI-assisted source review**, not an assertion that a human reviewed the
results. The packet, labels, scores and closure report preserve that distinction. No human
approval, independent reviewer or independent gold validation is invented.

The implementing agent had already seen A–E fixtures and historical measurements of these
corpora. These are exploratory measurements on known repositories, not untouched holdouts.
The original independent human gold suite remains unfulfilled. AI-assisted results cannot
remove `HTTP_LINK_INDEPENDENT_EVALUATION_PENDING` or change the recorded Stage 14 API status.

No real model/provider is contacted. S6's real-model quality evaluation remains deferred;
its loopback-stub security and validator gates are exercised by the integration suite.

## Frozen inputs and chronology

1. `preregistration.json` binds the metrics, numerical gates, sampling seed, performance
   protocol, authorization amendment and complete file-hash manifests of three corpora.
2. BuildLens is exported with `git archive` at `febdf14`; the existing owner-approved archives
   are FastAPI template `f4bcfaba7c85be3c6332cc74a20819f586dcdbc6` (MIT) and Polar
   `9b679857ca56c8c1fdffe8edb46490a3151ce608` (Apache-2.0). No new repository is downloaded.
3. Source enumeration uses Python `ast` and TypeScript **5.7.3**, separately from BuildLens.
   Seed 1512 selects 50 callsites per language per corpus, round-robin over direct,
   attribute and dynamic source forms: 300 calls in total. Test and generated code are
   included; this is an explicitly balanced sample mixture, not corpus-prevalence accuracy.
4. Jedi **0.19.2** and TypeScript provide separate differential proposals. They are not
   ground truth. Jedi's extension loading is disabled; the TypeScript compiler emits
   nothing, loads no repository plugins, and its filesystem host is confined to the archive.
5. The source-only packet contains no tracer predictions. `source_review.py` records the
   AI-assisted adjudication and corrections. `labels.json` is frozen before observations
   are opened. Reviewer identity, reasons and `reviewed: true` are mandatory.
6. Observations come from the real snapshot → structure → processing → catalog →
   architecture → flow evidence → model → trace pipeline in Part F-owned PostgreSQL
   databases. Byte-verified temporary corpus copies respect the production inventory rule
   that excludes evaluation directories. Normal artifact, byte and chunk caps are retained;
   wall deadlines for initial lab pipeline phases are 600 seconds.

The twelve source-labelled path cases are **bounded callsite walks**, one per
corpus/language/direction. They inspect exact local scope visits, tiers, guards, outcomes,
continuations, async relations, effects and unknowns within the stated depth bound. They
are useful diagnostics; they do **not** fill the original S0 whole-application/cross-layer
gold strata. Full frontend → HTTP → database → response behavior also has designed
PostgreSQL fixtures in `test_flow_trace.py`, reported as S2 rather than independent S0 gold.
Six source-written lexical entry queries are evaluated separately as S1.

## Scoring rules

`application/flow_evaluation.py` supplies the mechanical scorer. Every input has a digest;
labels bind the packet, observations bind the exact labels, and scores bind all four records.
Scoring refuses changed records, missing or duplicate units, untouched label templates and
membership changes. An omitted extraction stays in the coverage and recall denominators.
Wrong emitted target sets count against precision; emitting a local target for an
indeterminable label also counts as an overclaim. Unknowns are never silently discarded.

Target identity comes from source declaration locations. Callsite matching uses the **full
start/end source span**, since `factory()` and `factory().method()` can share a start.
The first exporter used start positions only and did not canonicalize named arrow scopes;
its observations are retained. `source-positions.json` records the source-derived correction,
bound to the unchanged frozen packet. `.r2.json` observation files use the corrected export.
This is an exporter repair, not a change to labels or to tracer resolution.

Precision reports its numerator, denominator and Wilson 95% interval, with a lower-bound
gate of 0.90 and a minimum of 150 sampled calls per language. Recall's gate is 0.70.
The power calculation is retained in `power.json`. These rates describe the stated sample
mixture and AI-assisted judgments. They are not independent estimates of universal accuracy.
Source-form/corpus strata remain visible as diagnostics. Empty denominators are undefined,
and empty or insufficient strata are inconclusive.

Path scoring uses ordered, duplicate-preserving LCS for hop precision/recall, plus separate
set comparisons for the other facets. Exact match requires all facets to agree and no
forbidden target. An apparently perfect mechanical score never establishes stage acceptance
when the original gold suite is incomplete.

## S4–S8 and closure checks

- **S4:** differential target agreement, with oracle limitations and disagreements retained
  separately from source judgments. Installed-package inference, missing dependencies and
  aliases are known oracle limitations, not excuses to discard tracer errors.
- **Z3:** `flow_z3_experiment.py` compares 1,000 seeded integer-equality conjunctions against
  Z3 **4.13.4.0**; separate opaque evaluations and same-branch contradictions are controls.
  This experiment does not add Z3 to the application or prove whole-program soundness.
- **S5:** real graph integrity checks; existing element/evidence/cited-row tamper tests;
  pinned-trace freshness and non-interference checks. Large integrity reads are batched
  below PostgreSQL's bind-parameter limit without omitting evidence.
- **S6:** the existing real outbound gate and loopback `MockTransport` integration checks,
  plus adversarial claim validation. This is stub verification, not real-model quality.
- **S7:** each corpus and query class has 20 frozen inputs. Each workload uses five discarded
  warmups and 59 timed samples, uniformly selected with replacement using seed 151207.
  Forward, backward, path and lexical-entry queries are interleaved. Cold-application samples
  evict the flow graph cache; PostgreSQL buffers are uncontrolled. Warm samples retain it.
  The sample maximum is the one-sided 95% upper confidence bound on mixture p95 under the
  stated IID/stationarity assumptions. The placeholder gate is 2 seconds. Spearman drift
  above 0.30 in magnitude or a split-half median ratio above 1.25 makes the run
  `not_stationary`, with no latency gate evaluated. Every error remains counted; sample
  journals retain progress, and interrupted attempts are not completed measurements.
- **S8:** 10,000-call extraction bombs in both languages; deep nesting; recursion and
  generator-shaped chains; fan-out; DNF expansion/truncation; existing source-read,
  outbound, screening and cap tests.
- **Closure:** both required Python runtimes, PostgreSQL 16/pgvector 0.8.6 integration,
  Ruff/format/mypy, frontend tests/lint/build, migration round trip and ORM parity, frozen
  Stage 13/14 artifacts, upgraded Stage 11 corpus validation and native lab parity, and a
  browser run against the real scratch model. Hosted CI remains unrun.

The template completed the full uncensored S7 protocol. The larger BuildLens and Polar
attempts were interrupted and their journals retained; their original quantile suite is
therefore incomplete. A **post-observation amendment**, recorded separately in
`performance-deadline-amendment.json`, adds a two-second client-deadline diagnostic on the
same frozen inputs. It counts timeout failures and records actual elapsed attempt durations.
It supplies no completed-request p95 confidence bound when measurements are censored, and
a failed warmup never becomes a valid warm-cache performance claim. This diagnostic cannot
substitute for the original S7 statistical gate.

## Reproducing the tooling

Run from `backend/`, with development dependencies installed and `PYTHONPATH=src:.:scripts`.
The differential environment additionally needs the pinned Jedi and Z3 packages above;
they are lab-only dependencies, absent from the application dependency set.

```sh
python scripts/flow_evaluate.py prepare /absolute/path/to/a/new/run
python scripts/flow_evaluation_run.py build /absolute/path/to/run --corpus buildlens
python scripts/flow_evaluation_run.py build /absolute/path/to/run --corpus full-stack-fastapi-template
python scripts/flow_evaluation_run.py build /absolute/path/to/run --corpus polar
# Review source-only packets and freeze complete, honestly attributed labels.
python scripts/flow_evaluate.py validate /absolute/path/to/run
python scripts/flow_evaluation_run.py observe /absolute/path/to/run
python scripts/flow_evaluate.py score /absolute/path/to/run --out /absolute/path/to/run/score.json
python scripts/flow_evaluation_run.py performance /absolute/path/to/run --corpus buildlens
python scripts/flow_z3_experiment.py --out /absolute/path/to/run/z3.json
```

The lab runner requires a checkout containing the test support helpers. Its default admin
URL is the existing disposable Stage 15 server on loopback port 55450; use `--admin` for
another disposable PostgreSQL 16/pgvector 0.8.6 server. It records ownership immediately and
never drops or modifies an existing non-Part-F database. Generated records refuse overwrite.
Worker-spawning operations must run from guarded script files, not a stdin main module.

Databases and temporary corpus copies are retained as reproducibility evidence until the
owner explicitly chooses to remove them. Original evaluation files and historical failed
attempts remain available; failure is not rewritten as success.
