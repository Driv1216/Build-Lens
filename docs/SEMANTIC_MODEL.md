# Stage 13 — Semantic Project Model

**Status (2026-10-25): Milestone A (multi-language structural foundation) implemented and
verified locally.** Milestones B (deterministic semantic catalog), C (controlled AI
generation) and D (evaluation and hardening) are **not started**. Nothing in this stage
generates or stores AI text yet.

The approved plan (revision 4) separates deterministic facts from AI interpretations. It keeps
every existing Stage 1–12 producer byte-identical and adds capabilities as separately contracted,
per-artifact passes. This document records what Milestone A actually built and verified. The
contracts for later milestones are design commitments, not implemented behaviour.

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

## Not built yet (later milestones)
- Semantic catalog (entities, recognizers, import resolution, `F#` facts).
- AI generation (persisted plans, `OutboundGate`, generator profiles, claims, freshness index).
- The Explore UI.
- Semantic benchmarks.
