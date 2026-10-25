# BuildLens current state

**Stage 13 — Semantic Project Model: Milestone A (multi-language structural foundation)
implemented and verified locally, 2026-10-25. Milestones B–D are not started; no AI text is
generated or stored.** Python declarations, contracts and fingerprints are frozen (golden
parity captured from `816b136`). Additive, separately contracted passes now provide
`python-facts.v1` and parse-only `tree-sitter` TS/TSX/JS declarations (`tree-sitter` 0.26.0,
`tree-sitter-typescript` 0.23.2 and `tree-sitter-javascript` 0.25.0 in the base install). Migration
`0010_multilanguage_structure` is additive only. TS/JS evidence carries its own authority (M4)
and is chunked, so it is searchable through the unchanged retrieval paths. Verification is
summarised in [docs/SEMANTIC_MODEL.md](docs/SEMANTIC_MODEL.md):
- Golden parity holds on Python 3.14.6 and 3.11.15.
- A real old-code-populated upgrade left every pre-existing data table byte-identical, and old runs stay current.
- On an upgraded copy of the retained Stage 11 corpus, `eval validate` passed 30/30 and `lab parity` passed 90/90.
- The TS compiler differential reports one explained disagreement.

A heap-corruption crash in py-tree-sitter 0.26.0 `Point` was found on real files and avoided,
with a regression test. Deviations from the plan are listed in that document: no in-parse RSS
sampling, and `.mjs/.cjs/.mts/.cts` unsupported. Nothing is committed and hosted CI has not been run.

**Implemented scope:** Phase 2, Stage 12 — LangChain / LlamaIndex engineering laboratory, on top
of the closed Stage 11 native evaluation (unchanged).

**Stage 12 implementation and verification, 2026-10-24.** Baseline: Stages 9–11 committed as
`8c7fad5` (tag `stage11-closed`), Stage 12 diff base `stage12-base` (`e326126`), work on branch
`stage12/framework-lab`. No production tables, migrations or Stage 5–11 behaviour changed; no
framework was adopted into production. Design: [docs/FRAMEWORK_LAB.md](docs/FRAMEWORK_LAB.md).

| Stage 12 dimension | Actual state |
| --- | --- |
| Harness parity (real models, retained corpus `03c559c4…`) | **Passed**: lab-native reproduced Stage 11 run `7e93df61` for 90/90 case×variant pairs (retrieval, ranking, packed context) |
| Real benchmark `bench-r1-20261024b` | **Completed**: 30 pipelines × 30 approved R1 cases, 900/900 successful, authority stable; 19-case matched subset |
| Framework-over-native adapters (LC-A, LI-A, LI Workflow) | Identical rankings to native in 30/30 cases (unreranked and reranked) |
| Stored-vector reuse | Verified genuine for all 9 native-chunk indexes: 2,099 reused, 0 computed, digests read back from each store |
| Stores (InMemory, SimpleVectorStore, LS-1/2/3 pgvector) | Same top-32 set as native on 30/30; identical order 24–28/30 (tie reorderings only); no metric differed |
| Generation | **Not authorized, not executed.** `generate` is a gated stub; offline models exercise mechanics only; no answer-quality claim |
| Loader safety (LD-1, synthetic sandbox) | Default LangChain/LlamaIndex loaders followed an escaping symlink and loaded synthetic key/token bait; BuildLens excluded/rejected them |
| Unsupported languages (LD-2, constructed, 12 questions) | Native 0/8 vs framework ingestion 8/8 at covered@5; capability only, tiny synthetic corpus |

**Measured highlights (24 answerable cases; approved tier unless stated).** Native hybrid MRR@32
0.4560 / Recall@5 0.2064; reranked 0.5972 / 0.4294; LangChain `EnsembleRetriever` (vector + BM25)
0.5552 / 0.3615 (paired MRR Δ +0.099, 9/4/11, bootstrap −0.039…0.237, not conclusive); span
context coverage 0.4236 vs 0.2708 native hybrid with ~2× context (15.96 vs 8.09 KiB). LlamaIndex
BM25 alone: best unreranked Precision@5 (0.300). Framework re-chunking pipelines scored below native
chunks on approved span coverage in the matched subset (evidence coverage@5 native 0.132 vs
0.000–0.088), except LlamaIndex parent-child (0.211) at 3.3× context. Full tables:
`.buildlens-eval/lab/runs/bench-r1-20261024b/summary.md`; findings in `docs/FRAMEWORK_LAB.md`.

**Verification, 2026-10-24.** Backend default suite incl. lab and lab-database tests passed on
Python 3.14.6 and 3.11.15 (disposable PostgreSQL 16.15/pgvector 0.8.6 at 127.0.0.1:55439; separate
least-privilege lab database at 127.0.0.1:55412). Real-model gate (pinned MiniLM `1110a243…`,
cross-encoder `233902d2…`, re-acquired with the official CLI on approval) passed 4/4 incl. the new
lab gate (recompute parity < 1e-5). Ruff, format and mypy passed on both versions. Genuine base
install (dev + generation extras only) passes the base selection and serves `/lab/status` with
frameworks reported missing. Frontend: 21 tests, lint, TypeScript and build passed; `npm audit`
reports the same 5 pre-existing toolchain advisories as the baseline lockfile (new dependencies add
none; not fixed in Stage 12). Browser check of `/lab`, `/lab/probe` (three pipelines on the real
corpus) and `/lab/runs` passed after fixing a workbench bug it exposed. Re-scoring is
byte-deterministic; the retained Stage 11 database is unchanged (all 15 tables, row counts + md5).
Exact gate outputs are in the Stage 12 review bundle.

**Problems found and fixed during verification:** sentence-window expansion after dropping
oversized sentences (23 errors in the first full run `bench-r1-20261024`, retained); framework
default-path builds over artifacts produced 0 nodes until the root-identity cause was found;
react-table re-render loop; stale-probe lock; probe sending listing-only fields; provisioning
error echoing a (never-used) generated password; RSS supervision silently skipped without psutil
(now fails closed).

**Corrections after independent technical review, 2026-10-24 (commits `bcf2a98`, `14ff248`;
awaiting focused re-review; not accepted).** The review of bundle `04a32260…` accepted the benchmark evidence but
reproduced worker defects, now corrected with regression tests that fail on the unfixed code:
S12-SEC-01 the audit-hook guard checked only the source of rename/replace and only the destination
of hard links (a protected file could be overwritten, linked and read) — both paths are now
checked, `dir_fd`/descriptor mutations are resolved, `chdir` is denied, vendored copy identical;
S12-SUP-02 a worker returning `ok: false` was reused — every failed request now kills and replaces
the process, and loaded-index state follows the worker generation; S12-SUP-03 RSS supervision
failed open after startup — losing the measurement now kills the worker; S12-PRIV-04 every
framework worker received the lab DB credential when one was configured — only database-scoped
workers serving LS-1/2/3 do now; S12-OPS-05 provisioning revoked PUBLIC privileges on every
database of the server — it is now scoped to the lab database, refuses (changing nothing) on a
shared server, and makes its single server-wide change only with a validated `--dedicated-server`.
Audit hooks remain defense in depth, not a sandbox. Benchmark, parity and LD runs were not
repeated (successful-request analytics are unchanged); the full lab suite, lab-database stores and
the real-model gate were rerun on `14ff248`: 193 passed on Python 3.14.6 and 3.11.15 (incl. lab and
lab-database tests, none skipped), base install 176 passed, real-model 4/4, ruff/format/mypy
clean; frontend unchanged. The first gate run exposed a startup race in the new RSS check under
the slower base install (reported `…_LOST` instead of `…_UNAVAILABLE`, both fail closed); fixed in
`14ff248`, then 20/20 repeated runs passed.

**Focused re-review, 2026-10-24: S12-SEC-06 corrected in `4706af0` (awaiting re-review; not
accepted).** The re-review confirmed the other four corrections but reproduced a guard bypass: the
`open` audit event omits `dir_fd`, so descending from an unprotected ancestor with descriptor-relative
`os.open` read (and, with a writable cwd, wrote) protected files. The guard now denies every relative
open (fail closed, including legitimate descriptor-relative opens in a write root), applies the
protected-read rule to directory listings, requires absolute write-root SQLite paths and denies
symlink creation; vendored copy identical. A spawned-worker regression test (two cwd variants) fails
on the unfixed guard and passes now; 195 passed on Python 3.14.6 and 3.11.15, base install 176. All
framework pipelines and LD-1 loaders were qualified under the stricter guard. Unaudited calls
(`os.stat`/`access`/`readlink`, `mkfifo`/`mknod`) and native code remain outside the guard; it is
still defense in depth, not a sandbox. Open item from the re-review: provisioning preflights only
PUBLIC CONNECT, not direct grants to a pre-existing role.

**Final targeted corrections, 2026-10-24 (commits `9be09a4`, `4a684c7`; awaiting review; not
accepted).** (1) Composite evidence: LlamaIndex sentence windows were credited for their whole
bounding span without any parent check; the worker now reports each constituent's span and hash,
the parent verifies every constituent against the source and the delivered text, and coverage
credits only verified segments (LangChain windows are exact slices and are hash-verified as
such). (2) The `kind` filter was silently ignored for `artifact_sources`; it is now rejected
there and an unknown kind never matches. (3) Generation was reported as "authorized" when the
flag was set although no provider integration exists; it is now reported not enabled, not
available and not implemented, and `generate` is never offered to workers. (4) Provisioning now
refuses, before any change, a pre-existing role holding CONNECT on another database through its
own, owner or inherited grants (previously the lab database was created before the refusal).
Regression tests reproduce each defect on the unfixed code. 206 passed on Python 3.14.6 and
3.11.15, base install 186. Scoring semantics changed, so the sentence-window experiments and
LD-2 were regenerated: no coverage, ranking or derived metric changed (details in
`docs/FRAMEWORK_LAB.md` section 4); the retained benchmark figures stand.

**Limitations and deviations.** (1) macOS renumbered the repository volume's `st_dev`; the
production secure reader refuses the original snapshot root (`ROOT_CHANGED`). Lab artifact
experiments used an explicit lab-only opt-in that requires the recorded inode and per-file sha256
fingerprints; 16 snapshot files changed on disk since 2026-10-01 and 2 were removed, so artifact
pipelines saw 138/157 artifacts and are compared on a 19-case matched subset. (2) Audit hooks are
defense in depth, not a sandbox. (3) Offline models are mechanics only. (4) `GenericLoader` +
`LanguageParser` could not run (`ImportError`). (5) Latencies are indicative (single machine).
(6) `@tanstack/react-table` pinned to 8.21.3 instead of the planned 9.x (v9 API redesign).
(7) Derived and constructed tiers are not human-approved. Hosted CI has not been run.

**Stage 11 closed, 2026-10-02:** implementation, verification, approved retrieval scoring,
authorized provider execution and human review are finalized. The real-provider benchmark is
**inconclusive/partial** because 88/90 requests failed operationally; answer quality is **not
passed**. Closure records the measured outcome and limitations. Stage 12 has not started.

**Stage 11 implementation, 2026-10-01:** Added the shared `.buildlens-eval` exclusion boundary
(`stage2.v3`), secure source-read eligibility (`posix-secure-reader.v3`), passive ranking and
failure-stage observation, shared local RAG lifecycle wiring, exact frozen-corpus binding,
retrieval/structural metrics, sequential CLI runs, atomic local artifacts, deterministic reports,
paired comparisons and human-review validation. Benchmark definitions and reviews are stored only
inside the excluded directory. The user explicitly approved relevance packet R1 on 2026-10-02;
dataset `4-r1-reviewed` records that approval separately from provider and answer verification.

| Stage 11 dimension | Current state |
| --- | --- |
| Infrastructure verification | Passed: migrated PostgreSQL integration, backend regressions on Python 3.11/3.14, full static/frontend checks, and real local-model tests |
| Retrieval baseline execution | Executed: 90/90 successful production evaluations; rescored with approved curated relevance at 1/5/10/32; zero invalid/error/unavailable observations |
| Human ground-truth approval | Approved R1: 30 cases, 798 judgments (98 relevant / 700 irrelevant); native validation passed 30/30 |
| Real-provider answer evaluation | Executed; inconclusive/partial: 88/90 operational failures, 2 valid abstentions, zero answered responses; answer quality not passed |
| Semantic answer review | Approved A1: 2/2 valid responses reviewed; both safe abstentions, task-incorrect and sole facet missing; 88 errors ineligible |

**Stage 11 closure verification, 2026-10-02 (local date):** 143 deterministic backend tests
passed on each of Python 3.11.15 and 3.14.6 with unique disposable PostgreSQL 16.15 databases,
real migrations 0001–0009 and pgvector 0.8.6. Full Ruff, formatting and mypy checks passed.
Three pinned CPU real-model tests passed, including pgvector and reranked PostgreSQL context.
Frontend verification passed 16 tests, lint, TypeScript and production build. Full verification
found and repaired evaluator typing issues missed by the earlier scoped checks. A new integration
test exercises all 90 synthetic variant evaluations, exact binding, exclusion, deterministic
rescoring, contamination rejection and mid-run authority invalidation. Real-corpus offline comparison exposed an oversized duplicated score artifact; compact
score references fixed it (38 MiB to 2.7 MiB), and native comparison/rescoring passed. Independent verification
records can be attached explicitly to offline reports using `--verification`; this does not
approve ground truth or establish provider/semantic results.

A new snapshot was prepared through normal onboarding, structure, processing and real embedding
operations in the retained isolated `buildlens-stage11-postgres` container and volume, exposed
only at loopback port 55411. Existing databases were not reset or modified. Project
`641a63cd-b382-438f-b34a-8d6015e353c0`, snapshot `0360b2ca-ca58-4f6c-9544-c3d3870997a8`,
processing `9b60b920-a61f-41f1-9af8-515b36a98eb2`; the full embedding authority and inventory
are retained in `.buildlens-eval/datasets/corpus-20261002.json`. Corpus digest:
`03c559c4fc0b585ae6da7256711e5219e07f60469d0c2d89bfd13e4c9e655ce3`.
It contains 270 inventory entries, 157 searchable artifacts and 2,099 embedded chunks.
Processing is partial: two Python test fixtures containing recognizable synthetic GitHub-token
patterns were rejected structurally; 28 artifact types are unsupported, including TS/TSX.
All benchmark targets bind despite these documented gaps. Benchmark assets are excluded by
`stage2.v3` and `posix-secure-reader.v3` before preparation.

Original dataset `3-bound-draft` preserves six cases per category. Eleven cases received explicit agent
source-span refinements, with exact excerpts and revision history under the excluded datasets
boundary. The original draft is retained. The explicit user message `Approve Stage 11 relevance
packet R1` accepted the concrete definitions, evidence requirements and relevance proposals;
`buildlens-v4.r1-reviewed.json` records human acceptance and sealed definition/judgment digests.
The 30-card review replaced manual work through 3,000 raw entries. All 30 reviewed cases validate
with zero invalid bindings. The retained real retrieval run is
`7e93df61-1fe5-4ba4-adda-d6c33518960a` (`.buildlens-eval/runs/retrieval-20261002`):
all 90 evaluations succeeded in 160.42 seconds, authority remained stable, and no observation
was unavailable. Offline rescoring uses the 24 answerable approved cases:

| Measurement | Semantic | Hybrid | Hybrid + reranker |
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
| Required-evidence coverage@5 | 16.67% | 16.67% | 27.08% |
| Complete evidence@5 | 3/24 | 3/24 | 6/24 |
| Required-evidence coverage@32 | 44.44% | 44.44% | 44.44% |
| Complete evidence@32 | 9/24 | 9/24 | 9/24 |
| Packed-context coverage | 27.08% | 27.08% | 33.33% |
| Complete packed context | 5/24 | 5/24 | 7/24 |
| Mean evaluation duration | 1.290 s | 1.294 s | 2.719 s |

Durations include evaluator corpus verification and model warm-up, not just production service
latency. Semantic and hybrid ranking orders were identical on all 30 questions: the production
lexical branch returned zero candidates for these full-sentence queries. This corpus does not
establish a hybrid advantage. Recall/Precision/MRR are case macro means over 24 answerable cases,
with 100% approved judgment coverage at every reported cutoff. Recall measures curated relevance,
not exhaustive corpus recall; 30 known relevant sources outside rank 32 remain in its denominators.
Positions 33–100 remain unjudged except curated supporting sources, so native whole-ranking
provisional flags remain. Six unsupported cases have no Recall/Precision/MRR or evidence-coverage
denominator. Source-span coverage uses unchanged approved target requirements and remains distinct
from binary relevance and answer quality. Reranking improved MRR in 8 cases, worsened it in 4 and
left 12 unchanged; packed coverage improved in 3, worsened in 2 and left 19 unchanged. Repeated
reviewed reporting was byte-identical, all 90 native case files remained unchanged, and offline
self-comparison passed. The human subsequently authorized external-sharing scope R1 and all
90 production Ask evaluations. The exact scope and message are recorded separately under
`.buildlens-eval/reviews/gemini-authorization-r1/`; the earlier rejected preflight made no calls.

**Gemini corpus run, 2026-10-02:** run `e31f32f9-cc7a-4671-8988-a86e1690630b`
(`.buildlens-eval/runs/answers-gemini-r1-20261002`) retained 90/90 evaluations with stable
authority: 2 valid `insufficient_evidence` responses, 69 `GENERATION_RATE_LIMITED` failures,
and 19 `GENERATION_UNAVAILABLE` failures. There were zero answered responses, no retries and
no fallback. All 270 production token counts succeeded; full prompts measured 4,286–10,417
input tokens. The two hybrid responses were `identifier-06` (8,363 input / 537 output tokens,
26.58 seconds total) and `semantic-02` (4,479 / 317, 13.77 seconds), both returning
`gemini-3.8-flash`. Usage and billing of failed generation requests are unknown; no cause is
asserted for the sanitized unavailable errors. A metadata-only model check and offline SDK
construction check passed. These diagnostics made no additional generation request.

All authorized questions/context packages matched the retained scope, generation evidence stayed
within it, and retrieval/ranking observations matched the earlier baseline. Repeated reporting
was byte-identical and left all 90 answer-run case files unchanged. Four focused tests passed
(prompt parity, Ask failure capture, error denominators and human/structural separation); the
full gate was not rerun. The native CLI correctly exited 1 for the 88 operational failures.

The user explicitly approved packet A1 with `Approve Stage 11 answer packet A1.`. Both valid
hybrid responses are human-reviewed: **appropriate safe abstention (2/2)**, **incorrect for
task completion (2/2)**, and **the sole required facet missing (2/2)**. These are conditional
counts over the two valid responses; they are not an estimate of corpus answer quality. Both
have zero statements/citations, so grounding, citation precision/completeness and direct/inference
support are N/A with zero denominators. The 88 operational failures have no answer and receive
no semantic judgment; all 18 unsupported-question evaluations failed operationally. A complete
three-way answer-quality comparison is unavailable. The real-provider benchmark remains
**inconclusive/partial**, and answer quality is **not passed**.

The approved frozen target places `processing_contract()` in
`backend/src/buildlens/domain/source.py`; the compact approval-summary location was corrected
without changing dataset definitions, bindings or the approved judgments. Actual approval
provenance and validated reviews are retained under
`.buildlens-eval/reviews/answer-proposal-a1/{human-approval.json,reviewed/}`. Native offline
rescoring finalized `reviewed-answer-results-a1.json` in the answer run. Its generic summary
label `partial: 2 result reviews` covers the 90-result run; every eligible valid response is
reviewed (2/2), and the 88 failures are ineligible, not awaiting review.

Three small finalization tests passed (human/structural separation, approval provenance and
offline revised-judgment rescoring). Native review validation, repeated byte-identical scoring,
score integrity and immutable-artifact checks passed; all 180 retained case files, manifests,
approved dataset, sharing scope and retrieval scores are unchanged. No new provider request
or full gate was run. Stage 11 is **closed with the inconclusive provider result retained**;
final gates, evidence and limitations are in
`.buildlens-eval/runs/stage11-closure-20261002/{closure.json,REPORT.md,verification.json}`.
Stage 12 work started after this closure (see above).

See [evaluation workflow and measured results](docs/EVALUATION.md), the retained run summary,
and `.buildlens-eval/runs/preparation-20261002/verification.json` for separate verification evidence.

The Stage 10 and earlier verification records below are retained independently.
**Foundation verification:** 2026-09-14. **Development database reset:** 2026-09-17.
**Stage 8 implementation verification:** 2026-09-19.
**Stage 9 implementation verification:** 2026-09-20 (PostgreSQL and static gates passed; real reranker pending then). The 2026-09-25 real reranker CPU and PostgreSQL gates passed.

Stage 10 adds opt-in `/ask` with a versioned grounded prompt, provider token counting,
whole-item generation repacking, strict statement/citation validation, authority checks around
provider calls, and a compact UI. It adds no database tables or stored conversations. The default
provider is Gemini `gemini-3.8-flash` through the optional `generation` extra; OpenAI `gpt-6-luna`
remains available only when `BUILDLENS_GENERATION_PROVIDER=openai` is selected. Ask remains disabled
until the backend flag and selected provider key are set. There is no fallback. Local configuration
inspection is available at `/generation`.

**Stage 10 local verification, 2026-09-25:** Python 3.11 and 3.14 each passed 117 backend tests
against an isolated, migrated PostgreSQL 16/pgvector container; four explicitly gated tests were
deselected. Both runtimes passed Ruff, formatting, and mypy. A frozen, non-editable Python 3.11
base installation passed 85 unit tests with one optional-SDK test skipped; the frozen generation
and ML-extra installation, CLI entry point, and real embedding and reranker models also worked in
temporary Python 3.11 environments. The real reranker CPU/window/batch and PostgreSQL context
tests passed, as did the real embedding/PostgreSQL test. The frontend passed 16 tests, ESLint,
TypeScript, and production build.

**Gemini closure, 2026-09-28:** Python 3.11 and 3.14 each passed 121 deterministic backend tests
against migrated disposable PostgreSQL 16/pgvector. Ruff, formatting, and mypy passed; the
frontend passed 16 tests, lint, and production build. The synthetic real-provider fixture passed
PostgreSQL → real embeddings → hybrid retrieval → real cross-encoder → Stage 9 context → Gemini
token count → `gemini-3.8-flash` structured cited answer → citation validation → final authority
recheck, plus an insufficient-evidence answer and the timeout path. The final answered request
counted 768 input tokens; Gemini reported 768 input and 186 output tokens, with 6.20 seconds total
latency. The abstention counted and used 769 input tokens and 191 output tokens, with 12.20 seconds
total latency. The returned model ID was `gemini-3.8-flash` and no provider fallback occurred.
One earlier synthetic abstention attempt returned a sanitized 503; the cause was not established,
and two subsequent full fixture runs passed. No repository Ask request was sent to Gemini.

The OpenAI `gpt-6-luna` adapter remains supported and selected only by configuration. Its paid
closure gate was not run because no OpenAI key was configured. Stage 11 answer-quality measurement
remains outside this stage.

Stage 9 adds a separate `/context` operation without changing `/search`. The application obtains at
most 100 authoritative Stage 8 results, validates/deduplicates them, optionally reranks the first 32
with a dedicated pinned local cross-encoder, selects at most eight seeds, expands one adjacent split
part in either direction, and packs whole observations into 16 items and an exact 32,000-byte
canonical JSON evidence allowance. Every included source is rehydrated under a final shared authority
lock. Integrity failures abort; budget and policy exclusions remain inspectable query-time decisions.

The two ranking policies are explicit. `retrieval_order` performs no reranker admission, model load,
or inference. `local_cross_encoder` uses `cross-encoder/ms-marco-MiniLM-L6-v2` at revision
`233902d25c440f23af6f7d6e94d2946bac0bee0a`, local safetensors only, non-overlapping tail-preserving
windows, maximum raw-logit aggregation, a spawned CPU worker, absolute deadlines, sampled 2 GiB RSS,
and replace-on-failure cleanup. Acquisition is a separate checksum-verifying CLI operation. No
context/reranking rows or migrations were added.

Local Stage 9 verification passed 93 backend tests, including disposable PostgreSQL 16 migrations and
all four retrieval modes feeding bounded context; three real-model tests were deselected. Ruff,
formatting, full-source mypy, 10 frontend tests, ESLint, TypeScript, and a production build also
passed. At the time of this Stage 9 record, no reranker model had been downloaded; the separate
Python 3.11 CPU and PostgreSQL gate subsequently passed on 2026-09-25 as recorded above.

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
