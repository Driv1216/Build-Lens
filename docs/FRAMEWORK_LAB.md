# Stage 12 — LangChain / LlamaIndex engineering laboratory

**Status: complete — 2026-10-25, per user instruction.** Implementation, the retained
900/900 benchmark, final targeted corrections and documented verification are complete.
Recorded limitations remain, including unimplemented real generation and unrun hosted CI.
This status update does not assert another independent review; the dated review and test
history remains in [CURRENT_STATE.md](../CURRENT_STATE.md).

The laboratory runs genuine framework-native RAG pipelines (LangChain 1.x, LlamaIndex 0.14)
over the **same authoritative BuildLens evidence** as the native Stage 8–11 stack, inspects every
step, and measures them with the Stage 11 metrics plus engineering metrics. It is a learning and
evidence instrument: **no framework was adopted into production**.

Measured results, verification records and limitations are summarized in `CURRENT_STATE.md`
and retained under `.buildlens-eval/lab/runs/` (git-ignored). This document explains the design.

## 1. Isolation model — process isolation is not a security sandbox

| Layer | What it guarantees | What it does **not** guarantee |
| --- | --- | --- |
| **Supervised worker process** (primary) | Framework code never runs in the API/CLI process (tested via `sys.modules`). Each framework gets its own spawned process with a scrubbed environment: no `BUILDLENS_DATABASE_URL`, no provider keys, private `HOME`/`TMPDIR`, offline variables, `PYTHONHASHSEED=0`. Parent-enforced absolute deadlines, sampled RSS limit (a live worker whose RSS can no longer be measured is killed: `LAB_MEMORY_SUPERVISION_LOST`), kill-and-replace on **every** failed request — including a well-formed `ok: false` response — so a failed process is never reused. The worker's `generation` changes on every start and kill; the parent forgets indexes loaded into a discarded process and rebuilds them. | It is ordinary OS process separation under the same user. A malicious dependency could still read files the user can read, unless the guard below sees it. Kill-and-replace bounds state carried between requests, not what one request can do. |
| **No authority in the worker** | Workers have no production-database or repository access. Evidence arrives only from the Corpus Gateway; embeddings, reranking, native retrieval and tools are parent callbacks under BuildLens locks, contracts and budgets. The separate **lab** database credential (`BUILDLENS_LAB_PG_URL`) and the single network allowance to the lab server go only to *database-scoped* workers — separate processes (`langchain+lab-db`, `llamaindex+lab-db`, `langchain_postgres`) that serve the pipelines declaring `needs_lab_database` (LS-1/2/3). All other workers stay uncredentialed with no network allowance, even when a lab database is configured. | A database-scoped worker holds the lab role's (least-privilege) authority for its lifetime. |
| **Audit-hook guard** (defense in depth) | PEP 578 hook installed before any framework import: denies socket connect/getaddrinfo (except an explicit allow-list such as the lab DB), process creation, `chdir`, writes outside the index directory, and reads under protected roots (home, repository) that are not explicitly readable (venv, package source, index, sandbox). Rename/replace/move check **both** source and destination; hard links check **both** source and destination (a link aliases the source inode, so linking a protected or out-of-root file is denied); `dir_fd`-relative and descriptor-only mutations are resolved through the descriptor, and unresolvable paths are denied. **File opens must use absolute paths:** the `open` audit event carries no `dir_fd`, so the real target of a relative open (e.g. descending from an unprotected ancestor with `os.open(name, flags, dir_fd=fd)`) cannot be established, and every relative open is denied — including legitimate descriptor-relative opens inside a write root (fail closed). Directory listings (`os.listdir`/`os.scandir`, by path or descriptor) follow the protected-read rule; SQLite database files must be absolute and inside a write root (`:memory:` exempt); symlink creation is denied (a link could be retargeted between check and use). Each distinct denial is recorded once and returned. | Audit hooks see only Python-level operations. C extensions (libpq, native parsers, SQLite internals beyond its `connect` event) and `ctypes` are not observed; code that is already malicious can bypass hooks. Unaudited calls stay unchecked: `os.stat`/`lstat`/`access`/`readlink` (also with `dir_fd`) can still disclose existence, metadata and link targets under protected roots, and `os.mkfifo`/`os.mknod` are not seen. Path checks are realpath-based; without symlink creation a worker cannot retarget paths inside its own roots at the Python level, but this is not a race-free kernel check. Framework code that relies on relative or descriptor-relative opens (e.g. fd-based `shutil.rmtree`) fails under the guard; all 16 in-memory framework pipelines, LS-1/2/3 and the LD-1 loaders were qualified (CPython's compiler probes the pseudo-file `<unknown>` and is now refused, with no functional change). One tolerated event: urllib3's import-time IPv6 probe binds `::1` port 0 (no traffic). |
| **Separate lab database** | pgvector experiments use a separate server and database with a least-privilege role (NOSUPERUSER/NOCREATEDB/NOCREATEROLE/NOREPLICATION/NOBYPASSRLS, connection limit 8, 30 s statement / 5 s lock timeout, CONNECT only on `buildlens_lab`, CREATE only in schema `lab`; inside the lab database PUBLIC loses CONNECT and `public` CREATE). The settings validator rejects a lab URL on the production server with the same database or role. Provisioning changes only the lab role and lab database by default. It first checks the server and **refuses, changing nothing**, if PUBLIC can still CONNECT to any other database, or if a pre-existing role of that name holds CONNECT on another database through its own, owner or inherited (role-membership) grants (`LAB_DATABASE_ROLE_NOT_CONFINED`). `--dedicated-server` is an explicit confirmation for a disposable lab-only server: it is refused unless the server holds nothing but `postgres` and the lab database (`LAB_DATABASE_SERVER_NOT_DEDICATED`), and then revokes PUBLIC CONNECT on `postgres` — the only server-wide change, which is reported. Afterwards the role's connectable databases must be exactly the lab database. | Lab tables are disposable experiment state, not evidence. Never point provisioning at a shared server you do not administer. |

macOS `sandbox-exec` is deprecated and no OS-level sandbox is used.

## 2. Worker RPC (`stage12.worker.v1`)

Length-prefixed UTF-8 JSON frames (4-byte big-endian length), at most 8 MiB, objects only,
no NaN/Infinity, no duplicate keys. Kinds: `hello`, `request`, `call` (worker→parent callback),
`reply`, `response`. The parent:

* reads with a non-blocking deadline reader and kills the worker on timeout, EOF, protocol error,
  oversized frame, RSS breach or loss of RSS measurement (`LAB_WORKER_TIMEOUT`, `…_CRASHED`,
  `…_PROTOCOL_ERROR`, `LAB_TRANSFER_TOO_LARGE`, `…_MEMORY_LIMIT`, `LAB_MEMORY_SUPERVISION_LOST`);
* also kills the worker when a request fails any other way — an `ok: false` response
  (e.g. `WORKER_OPERATION_UNKNOWN`, `WORKER_EXCEPTION`, `VECTOR_REUSE_MISS`) or a parent-side
  failure mid-request such as an unframeable callback reply. There is no "reset-safe" error
  category: the next request always starts a fresh process;
* serves callbacks only from a per-request allow-list with a call budget
  (`LAB_CALLBACK_NOT_ALLOWED`, `LAB_CALLBACK_BUDGET_EXCEEDED`); callback exceptions cross the
  boundary as type names only (`LAB_CALLBACK_FAILED`);
* bounds every transfer in the bridge (≤64 texts, 256 KiB/text, 4 MiB/batch, 4 KiB queries,
  ≤64 rerank items, 4 MiB candidate content) and documents in 256-item / 4 MiB batches.

Worker stdout is re-pointed to stderr so framework prints cannot corrupt the protocol. The isolated
`langchain-postgres` environment vendors `wire.py` and `guard.py` byte-for-byte (tested).

## 3. Identity-safe precomputed-vector reuse

`reuse-stored` (strict) means: the framework's **own** embedding call path asks for the vector of
the exact text it intends to index; the ledger answers from verified stored vectors keyed by
`sha256(text)`; a miss is an error (`VECTOR_REUSE_MISS`). Stored rows are re-verified before use
(input hash = sha256(content), contract digest, dimension, finiteness, unit norm, membership,
authority before/after). A build is accepted as **genuine reuse** only if the bridge embedded zero
document texts, the worker computed zero, and every vector digest read back from the framework's
own persisted store (LangChain `InMemoryVectorStore.dump`, LlamaIndex `SimpleVectorStore`, or the
PostgreSQL table itself) equals the stored float32 digest. Every embedded text is recorded with its
raw and embedded hashes and a transformation class (`identity`, `metadata_prefixed`, `rewritten`).
Strict reuse of LlamaIndex nodes with metadata in the embed view fails loudly; `reuse-or-compute`
records the transformation and recomputes. Queries are always embedded live through the bridge.

## 4. Corpus Gateway and span anchoring

* `native_chunks`: the Stage 11 authoritative loader (freshness, contract, ownership, membership).
* `artifact_sources`: whole artifacts via the supervised secure reader, checked against the
  snapshot sha256 fingerprint, decoded exactly as processing decodes, secret-screened.
  **Root identity**: strict by default. The lab-only opt-in
  `BUILDLENS_LAB_ALLOW_ROOT_DEVICE_RENUMBERING` tolerates a changed `st_dev` only when the recorded
  inode matches and every file matches its frozen fingerprint; the policy is recorded per build.
* Every returned item is re-verified in the parent: chunk ids are re-bound to authoritative chunks
  (text hash must match), artifact spans are re-sliced and hashed. Unanchorable nodes (rewritten
  or ambiguous text) are counted and can never satisfy coverage. **Composite items** (LlamaIndex
  sentence windows, whose sentences are re-joined with spaces) report each constituent sentence's
  span and text hash. The parent credits a constituent only if its source slice hashes to the
  reported value and occurs, in order, in the delivered text; consecutive verified constituents
  are joined only across whitespace-only gaps (in the source and in the delivered text). The
  item's credited `spans` are these verified segments; a bounding span alone is never anchored.
  LangChain windows are exact source slices and are hash-verified like any slice. Coverage (the
  Stage 11 sweep, the derived projection and the constructed tier) uses the verified segments,
  and benchmark records persist them (`spans`). *Impact:* the two sentence-window experiments
  and their native baseline were regenerated on the retained corpus (`bench-r1-20261024-windows2`,
  90/90) and LD-2 was regenerated (`ld2-20261024-composite`). No coverage, ranking or derived
  metric changed: all 960 LlamaIndex window items verified (373 context rows, one segment each)
  and LangChain windows verified as 2,880 exact slices. LangChain-window `context_kib` reads 23.55
  instead of the retained 23.59 because of tie reorderings in 5 cases; a control run of the
  pre-correction code gives the same 23.55, so this drift predates the correction. The retained
  `bench-r1-20261024b` figures remain the reported benchmark.
* Filters: `locator_prefix` and `language` apply in both corpus modes; `kind` applies to
  `native_chunks` only (whole artifacts have no chunk kind) and is rejected (`CONFIG_INVALID`;
  the Gateway also refuses it) for `artifact_sources`. An unknown kind never matches.

## 5. Pipelines

| Outcome | Pipeline id | What it exercises |
| --- | --- | --- |
| N0 | `native.context` | Unchanged Stage 9 ContextService via observation hooks |
| LC-A | `langchain.native_retriever` | `BaseRetriever` over native ranking, LCEL `RunnableParallel`/`assign`, compression, prompt → offline model → `with_structured_output` → BuildLens citation validation |
| LC-B | `langchain.vector` | `InMemoryVectorStore` (similarity / MMR / threshold) with reused vectors |
| LC-C | `langchain.ingest` | `RecursiveCharacterTextSplitter.from_language`, `MarkdownHeaderTextSplitter`, `ContextualCompressionRetriever` + cross-encoder compressor |
| AR-1 | `langchain.ensemble`, `llamaindex.fusion` | `EnsembleRetriever` weighted RRF (chunk-id vs page-content dedupe); `QueryFusionRetriever` modes |
| AR-2 | `langchain.parent_document`, `llamaindex.parent_child` | `ParentDocumentRetriever`; `RecursiveRetriever` + `IndexNode` small-to-big |
| AR-3 | `langchain.auto_merge`, `llamaindex.auto_merging` | Hand-built LCEL merge (no LangChain equivalent); `HierarchicalNodeParser` + `AutoMergingRetriever` |
| AR-4 | `langchain.sentence_window`, `llamaindex.sentence_window` | LCEL window expansion; `SentenceWindowNodeParser` + `MetadataReplacementPostProcessor` |
| AG-1 | `langchain.agent` | `create_agent` with read-only tools, allow-listed middleware, call limits, evidence budget, no checkpointer |
| LI-A | `llamaindex.native_retriever` | `RetrieverQueryEngine`, bridge rerank postprocessor, response modes |
| LI-B | `llamaindex.vector` | `TextNode`s with relationships, `VectorStoreIndex`, BM25, `PrevNextNodePostprocessor`, cutoff, `LongContextReorder` |
| LI-C | `llamaindex.ingest` | `IngestionPipeline` + docstore/cache dedupe, Sentence/Markdown/Token/Code splitters, `StorageContext.persist` / `load_index_from_storage` |
| LI-WF | `llamaindex.workflow` | Stage 9 retrieval re-expressed as a LlamaIndex Workflow |
| LS-1/2/3 | `langchain.pg_custom`, `langchain_postgres.pgvector`, `llamaindex.pgvector` | Custom `VectorStore` on pgvector 0.4.1; official `PGVectorStore` in an isolated env; LlamaIndex `PGVectorStore` |

**Real generation is not implemented.** The lab status reports generation as not enabled, not
available and not implemented whatever `BUILDLENS_LAB_GENERATION_ENABLED` says (the flag has no
effect), and workers are never offered the `generate` callback. The bridge stub refuses unless
authorized and raises `LAB_GENERATION_NOT_IMPLEMENTED` even then; `langchain-google-genai` and
`llama-index-llms-google-genai` (extra `lab-providers`) are installed but unused. Building the
authorized path (bridge to the existing Gemini adapter and framework-native provider classes with
zero retries) requires an explicit authorization scope and is left for a follow-up.

Offline models (`OfflineChatModel`, `OfflineLLM`) are deterministic extractive stand-ins that
implement the real framework interfaces (tool binding, structured output, synthesizer calls).
They exercise **mechanics only** and are never evidence of answer quality.

## 6. Scoring tiers

* **approved** — R1 human judgments; only for pipelines whose items are BuildLens chunks.
* **span** — approved required-evidence span coverage; any anchored item counts. Read together
  with delivered context KiB and coverage per KiB.
* **derived** — `derived.r1-span-projection.v1` (τ = 0.5); **not human-approved**.
* **constructed** — synthetic dataset spans known by construction (LD-2); **not human-approved**.

Reports include a **matched subset**: answerable cases every pipeline could fully see (artifact
pipelines cannot see files whose bytes changed after the frozen snapshot).

## 7. Usage

```sh
cd backend
uv sync --frozen --extra dev --extra lab-langchain --extra lab-llamaindex --extra lab-pgvector --extra lab-code
uv run --no-sync buildlens lab status
uv run --no-sync buildlens lab save path/to/config.json
uv run --no-sync buildlens lab bench --dataset ../.buildlens-eval/datasets/buildlens-v4.r1-reviewed.json \
  --project <id> --snapshot <id> --experiment <name> ... --run-id <run> --baseline n0-native-hybrid
uv run --no-sync buildlens lab report ../.buildlens-eval/lab/runs/<run>
uv run --no-sync buildlens lab parity --stage11-run ../.buildlens-eval/runs/retrieval-20261002 ...
uv run --no-sync buildlens lab loader-diff --output <path>
uv run --no-sync buildlens lab env sync langchain-postgres      # explicit, never automatic
uv run --no-sync buildlens lab db provision --credentials <0600 file>   # separate server only;
#   add --dedicated-server only for a disposable lab-only server (one reported server-wide change)
```

The UI is under `/lab` (overview, experiment builder, probe workbench, index inspector, benchmark
runs, loader safety). Index builds and probes are synchronous and deadline-bounded; benchmarks run
from the CLI.

## 8. Measured findings (2026-10-24)

All numbers come from retained artifacts under `.buildlens-eval/lab/runs/` (see `CURRENT_STATE.md`
for identities). Offline runs: no generative model was called, so **no answer-quality claim** is
made. Dataset `4-r1-reviewed` (30 cases, 24 answerable) on frozen corpus `03c559c4…`.

**Harness validity.** Lab-native reproduced the retained Stage 11 run `7e93df61` exactly for
90/90 case×variant pairs (retrieval, pre-seed ranking and packed context) with the real pinned
models. LangChain and LlamaIndex adapters over native retrieval (LC-A, LI-A) and the LlamaIndex
Workflow re-expression returned identical rankings to native in every case (MRR@32 0.4560
unreranked, 0.5972 reranked, over all 24 answerable cases).

**Stores and reuse.** `InMemoryVectorStore`, `VectorStoreIndex`/`SimpleVectorStore`, the custom
LangChain pgvector store (LS-1), the official `langchain-postgres` `PGVectorStore` (LS-2, isolated
environment) and LlamaIndex `PGVectorStore` (LS-3) all returned the **same top-32 set** as native
exact semantic search on all 30 questions, with identical order in 24–28/30 cases; every
difference is a reordering inside that set (first divergence at rank 24–30, except one
unanswerable case at rank 2), consistent with different tie-breaking among equal similarities
(native breaks ties by stable key). No reported metric differed (MRR@32 0.4560 for all). All five
showed **verified genuine reuse**: 2,099 vectors reused, 0 computed, every digest read back from the
store equal to the stored vector.

**Fusion is the clearest measured gain.** LangChain `EnsembleRetriever` (vector + bm25s BM25,
weighted RRF) vs native hybrid, 24 answerable cases: MRR@32 0.5552 vs 0.4560 (paired Δ +0.099,
9 wins / 4 losses / 11 ties, bootstrap 95% −0.039…0.237 — not conclusive), approved Recall@5
0.3615 vs 0.2064, span context coverage 0.4236 vs 0.2708 (Δ +0.153, 6/1/17, bootstrap
0.021…0.306) but with ~2× delivered context (15.96 vs 8.09 KiB; coverage per KiB 0.035 vs 0.042).
BM25 alone has the best approved Precision@5 among unreranked pipelines (0.300) but lower MRR
(0.367). Mechanism: the native PostgreSQL lexical branch requires every query term in one field
group and returns **zero** candidates for these full-sentence questions (Stage 11 finding); BM25
scores partial matches. Content-vs-id dedupe in `EnsembleRetriever` made no measurable difference
on this corpus. Cross-encoder reranking remains the strongest single change (MRR@32 0.5972,
Δ +0.141, 8/4/12, bootstrap 0.008…0.297).

**Re-chunking is not free.** Framework-native ingestion (LC-C, LI-C, AR-3, AR-4) over the 138
artifacts whose bytes still match the frozen snapshot scored *below* native chunks on approved
span coverage in the 19-case matched subset (evidence coverage@5: native 0.132; LC-C 0.070;
LI-C 0.088; LlamaIndex auto-merging 0.053; LlamaIndex sentence window 0.000). Parent/child
retrieval was the exception (LI parent-child 0.211 @5, 0.404 packed coverage) but delivered
28.7 KiB of context vs 8.8 KiB native (coverage per KiB 0.015 vs 0.031). Auto-merging with
128-token leaves retrieved fragments (3.4–8.8 KiB context, coverage ≤0.07): leaf size dominates.

**Framework behaviours made visible.**
* LlamaIndex embeds `metadata + text` by default; under strict reuse this fails loudly
  (`VECTOR_REUSE_MISS`); `reuse-or-compute` recorded every node as `metadata_prefixed`/`rewritten`.
* LlamaIndex counts metadata against `chunk_size` (113 tiny children before metadata was excluded
  from both views in the parent-child fixture).
* LlamaIndex hierarchical node offsets are relative to the parent node; anchoring requires summing
  along `PARENT` links. Sentence-window text is joined with extra spaces, so windows are only
  anchorable as composites of verified sentence spans.
* `MarkdownHeaderTextSplitter` discards offsets (2 of 4 fixture nodes unanchorable).
* `SentenceWindowNodeParser` treats code regions without sentence punctuation as giant
  "sentences": 57 exceeded the embedding window contract and were dropped
  (`max_sentence_chars=4000`); the LCEL equivalent dropped 24.
* `IngestionPipeline` docstore dedupe: a second run over unchanged documents produced 0 new nodes.
* `PrevNextNodePostprocessor` expands by document order, not BuildLens logical-unit parts; it
  lowered packed coverage here (0.194 vs 0.382) by spending budget on neighbours.
* `langchain-community` emits a sunset/deprecation warning on import.

**Loaders (LD-1, synthetic sandbox).** Default `DirectoryLoader` (20 files) and
`SimpleDirectoryReader` (23 files) both **followed an escaping symlink**, loaded a PEM key and a
GitHub-token-shaped file (both synthetic), loaded a gitignored file and `node_modules`, and put
absolute paths in metadata. Hidden-file variants additionally loaded `.env` and
`.buildlens-eval/answers.md`. BuildLens processed 8 files, excluded 3, secret-rejected 1,
decode-rejected 1, size-rejected 1, did not follow the symlink, and marked 10 unsupported.
`GenericLoader`+`LanguageParser` failed (`ImportError`, optional parser dependency absent).

**Unsupported languages (LD-2, constructed, tiny).** Native: 0/8 TS/TSX/JS/Go/Rust/SQL/shell
questions covered@5 (BuildLens produces no chunks for them); all four framework ingestion
pipelines 8/8; 4/4 for languages BuildLens processes. A 12-question synthetic corpus shows
capability, not retrieval quality.

**Engineering.** Cold import (median of 5, incl. ~38 ms interpreter start): langchain-core 231 ms,
langchain (`create_agent`) 1,998 ms, langchain-classic retrievers 1,649 ms, llama-index-core
2,943 ms, llama-index BM25 3,095 ms. Dependency closure beyond base: lab-langchain 45 packages,
lab-llamaindex 51, lab-providers 73. Adapter SLOC: LangChain worker 1,624; LlamaIndex worker
1,361; shared worker runtime 761; parent orchestration 1,762. Query p50 with the worker+RPC+
verification path is ~0.9–1.5 s vs native 0.48–0.80 s (same embedding worker); latencies are
indicative only (single machine, sequential, includes parent re-verification).

**Not established.** Answer quality or citation quality of any framework (no generation was
authorized); generalization beyond this corpus; statistical significance (24 cases, descriptive
bootstrap only); production adoption value. Derived-tier numbers are not human-approved.
