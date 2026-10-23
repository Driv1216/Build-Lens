# Stage 12 — LangChain / LlamaIndex engineering laboratory

The laboratory runs genuine framework-native RAG pipelines (LangChain 1.x, LlamaIndex 0.14)
over the **same authoritative BuildLens evidence** as the native Stage 8–11 stack, inspects every
step, and measures them with the Stage 11 metrics plus engineering metrics. It is a learning and
evidence instrument: **no framework was adopted into production**.

Measured results, verification records and limitations are summarized in `CURRENT_STATE.md`
and retained under `.buildlens-eval/lab/runs/` (git-ignored). This document explains the design.

## 1. Isolation model — process isolation is not a security sandbox

| Layer | What it guarantees | What it does **not** guarantee |
| --- | --- | --- |
| **Supervised worker process** (primary) | Framework code never runs in the API/CLI process (tested via `sys.modules`). Each framework gets its own spawned process with a scrubbed environment: no `BUILDLENS_DATABASE_URL`, no provider keys, private `HOME`/`TMPDIR`, offline variables, `PYTHONHASHSEED=0`. Parent-enforced absolute deadlines, sampled RSS limit, kill-and-replace; a failed worker is never reused. | It is ordinary OS process separation under the same user. A malicious dependency could still read files the user can read, unless the guard below sees it. |
| **No authority in the worker** | Workers have no database or repository access. Evidence arrives only from the Corpus Gateway; embeddings, reranking, native retrieval and tools are parent callbacks under BuildLens locks, contracts and budgets. | — |
| **Audit-hook guard** (defense in depth) | PEP 578 hook installed before any framework import: denies socket connect/getaddrinfo (except an explicit allow-list such as the lab DB), process creation, writes outside the index directory, and reads under protected roots (home, repository) that are not explicitly readable (venv, package source, index, sandbox). Denials are recorded and returned. | Audit hooks see only Python-level operations. C extensions (libpq, native parsers) and `ctypes` are not observed; code that is already malicious can bypass hooks. One tolerated event: urllib3's import-time IPv6 probe binds `::1` port 0 (no traffic). |
| **Separate lab database** | pgvector experiments use a separate server and database with a least-privilege role (NOSUPERUSER/NOCREATEDB/NOCREATEROLE/NOREPLICATION/NOBYPASSRLS, connection limit 8, 30 s statement / 5 s lock timeout, CONNECT only on `buildlens_lab`, CREATE only in schema `lab`; PUBLIC loses CONNECT and `public` CREATE). The settings validator rejects a lab URL on the production server with the same database or role. | Lab tables are disposable experiment state, not evidence. |

macOS `sandbox-exec` is deprecated and no OS-level sandbox is used.

## 2. Worker RPC (`stage12.worker.v1`)

Length-prefixed UTF-8 JSON frames (4-byte big-endian length), at most 8 MiB, objects only,
no NaN/Infinity, no duplicate keys. Kinds: `hello`, `request`, `call` (worker→parent callback),
`reply`, `response`. The parent:

* reads with a non-blocking deadline reader and kills the worker on timeout, EOF, protocol error,
  oversized frame or RSS breach (`LAB_WORKER_TIMEOUT`, `…_CRASHED`, `…_PROTOCOL_ERROR`,
  `LAB_TRANSFER_TOO_LARGE`, `…_MEMORY_LIMIT`);
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
  or ambiguous text) are counted and can never satisfy coverage. Composite nodes (windows, merged
  parents) carry a bounds-checked union span flagged `composite`.

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
uv run --no-sync buildlens lab db provision --credentials <0600 file>   # separate server only
```

The UI is under `/lab` (overview, experiment builder, probe workbench, index inspector, benchmark
runs, loader safety). Index builds and probes are synchronous and deadline-bounded; benchmarks run
from the CLI.
