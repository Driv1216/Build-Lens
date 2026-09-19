# BuildLens architecture

BuildLens is a modular monolith through **Phase 2, Stage 8 — Lexical & Hybrid Retrieval**.
React/Vite calls FastAPI; PostgreSQL is authoritative. Snapshots, structural analysis, and
processing are explicit user operations; embedding preparation is a separate persistent operation.
No repository modules are imported or executed.

## Boundaries

- `domain` owns immutable observations, stable identities, producer contracts, eligibility,
  shared decoding, source coordinates, and content screening.
- `application` owns use cases and typed store, reader, inspector, and analyzer ports. It has no
  SQLAlchemy, FastAPI, or infrastructure imports.
- `infrastructure` implements PostgreSQL stores, inventory, bounded Git invocation, secure POSIX
  traversal, supervised execution of trusted BuildLens readers/parsers/processors, and an optional
  offline Sentence Transformers worker.
- `api` wires concrete dependencies, calls public service methods, and converts domain/query DTOs
  to validated responses. `core` supplies validated environment configuration; `cli` supplies
  operational checks and retained-storage diagnostics.

Operations read and produce observations outside publication transactions. Trusted reusable
workers have parent-enforced deadlines and byte progress accounting; timeout/crash terminates
and replaces the worker. They are synchronous operation helpers, not a background job platform.

## Persistence and publication

Projects own snapshots (`project_versions`) and artifact path identities. Snapshot artifacts
record relative locators, inventory/ignore outcomes, root identity, and exact-byte fingerprints.
Structural runs own per-artifact outcomes, lexical symbols, and unresolved imports. Processing
runs own artifact outcomes and exact source chunks. Stable symbol identities belong to a
project's artifact identity; observation UUIDs belong to one retained run.

Publication locks the snapshot row and checks captured authority. Structural persistence flushes
analysis and artifact outcomes before identities and parent-first symbols, then imports. Processing
persists its run before outcomes and chunks. Active pointers change only inside a successful
transaction. Failed attempts remain separate from usable evidence. A competing attempt is retained
as superseded and cannot overwrite changed authority. Returned observations contain persisted
revisions and assigned identities.

Composite foreign keys enforce ownership across snapshots, identities, dependencies, and
chunk-symbol associations. Forward migrations `0006` and `0007` quarantine old derived runs and
clear usable active pointers without deleting history. Clean legacy constraints are validated;
legacy violations remain under `NOT VALID` constraints that still enforce new writes. The CLI
reports pending validation. Stage 4 downgrade refuses multiple retained runs before destructive
changes rather than deleting history to fit its earlier model.

## Incremental evidence

Reuse requires compatible versioned producer contracts, verified snapshot bytes, current
eligibility, and the same artifact identity. Contracts cover fingerprinting, reader policy,
parser/runtime, decoding, chunk boundaries, and screening. Python declarations reuse identical
observations' stable identities. Changed source continues only unique lexical declaration slots;
ambiguous slots receive new identities. Compatible current-snapshot evidence precedes the
predecessor baseline. Every reused observation is rebound to its current artifact, snapshot, and
run, with predecessor observation IDs retained solely as provenance.

Python chunks depend on equivalent required artifact structure, including stable declaration
identities and source coordinates. Reanalysis with equivalent evidence leaves processing fresh;
changed required evidence or incompatible contracts makes it stale. A run UUID change alone is
insufficient to make content stale. This is snapshot-qualified freshness, never a claim about
current filesystem bytes.

Chunk keys combine artifact identity, producer contract, distinct logical-unit anchor, part index,
and content hash. Absolute line offsets are excluded. Python residual anchors include neighboring
declaration identities and occurrence numbers. Markdown anchors distinguish repeated headings;
JSON paths retain member order and duplicate-member occurrences. TOML uses standard-library
validation and a quote-aware boundary scan. YAML uses bounded safe node composition without
constructing application objects. Unsupported units receive fallback diagnostics.

Shared decoding respects Python encoding declarations and BOMs. LF, CRLF, CR, and Unicode
coordinates refer to exact decoded source slices, without newline normalization. Generic splitting
tries paragraph, line, whitespace, then character boundaries. Empty sources have no empty chunks.

## Persistent vector evidence

Stage 7 reads only current content through Stage 5's shared compatibility, quarantine, and
structural-dependency freshness rule. Preparation streams bounded pages in locator/ordinal/UUID
order and verifies every exact UTF-8 slice. The logical key covers the full embedding contract,
stable chunk key, and input hash. Compatible retained observations are copied into new immutable
per-run rows with new observation UUIDs and explicit reuse provenance; only misses reach inference.

`embedding_runs` records processing authority, complete contracts, ordered membership, revisions,
coverage, diagnostics, and publication authority. `chunk_embeddings` binds each variable-dimension
pgvector value to its owning run, processing/artifact/chunk evidence, logical identity, and optional
reuse source. Composite foreign keys and checks enforce ownership, hashes, dimensions, unit norm,
and reuse provenance. Publication locks the snapshot and atomically inserts the run and all rows
before moving `active_embedding_run_id`. Failed and superseded attempts cannot replace active
evidence or supply reuse.

The pinned CPU MiniLM adapter embeds `chunk.content` verbatim. Long documents are tokenized without
truncation, split into consecutive token-ID payload windows, encoded in batches, normalized per
window, averaged by non-special-token count, and normalized again. Queries are trimmed only at the
edges and rejected rather than truncated when empty, over 4,096 UTF-8 bytes, or over the model
payload-token limit. PostgreSQL pgvector cosine distance implements exact search, with stable chunk
key and chunk ID as deterministic tie-breakers. Locator-prefix, language, and kind filters are
applied through authoritative joins before ranking and limit. Only ranked chunk IDs are hydrated
with source. Search holds a shared snapshot lock, uses deadline-bounded transaction-local
statement/lock timeouts, and rechecks both authorities before returning.

## Lexical and hybrid retrieval

The common snapshot-scoped `/search` contract requires `exact`, `lexical`, `semantic`, or
`hybrid`. A retrieval application service owns mode orchestration and exact-arithmetic RRF; a typed
retrieval session holds one shared snapshot lock while PostgreSQL selects candidate metadata and
hydrates final IDs. Every branch revalidates current processing authority, structural dependencies,
ownership, quarantine/supersession, filters, and source hashes before returning.

Exact matching bypasses normalization and compares declaration name, qualified name, and full
artifact-processing locator using literal case-sensitive `C` collation. Lexical matching applies
`buildlens_lexical_normalize_v1` identically to fields and plain queries, then uses explicit
`pg_catalog.simple` vectors. Content, symbol qualified name, path, and joined heading/JSON metadata
are independent groups: every query term must match within one group. The maximum matching group
rank is reported; exact matches precede lexical scores without receiving a fabricated score.

Migration `0009` adds GIN expression indexes for content, symbol qualified names, and locators.
Heading/JSON metadata stays scoped and unindexed because its array conversion is not immutable.
Each component fetches at most 100 distinct chunk candidates plus one truncation probe. Hybrid
unions the lexical and semantic lists by chunk UUID and computes `1 / (60 + rank)` per present
component. Exact matches sort before fused scores. Candidate queries do not project source bodies
or vectors; only the final at-most-50 IDs are hydrated in one batch.

## Security and resources

The API is a local, unauthenticated interface bound to loopback. Host/origin restrictions and JSON
mutation guards protect browser entry points; they are not an authentication system. PostgreSQL
Compose exposure is loopback-only and credentials must come from environment configuration.

Inventory, ignore controls, fingerprints, and processing use descriptor-relative no-follow
traversal and carry root device/inode identity. Final files are opened nonblocking before their
regular-file check. Symlinks, mount crossings, missing/unreadable files, observable substitutions,
and read failures produce source-free diagnostic codes. A parent deadline bounds potentially
blocking filesystem operations, processing, and worker communication.

Git invocation disables system/global configuration, executable fsmonitor and ordinary hooks,
configured content filters, and optional index writes. Duration and output are bounded; timeout
terminates its process group. Ignore controls outside the registered root fail closed.

Processing defaults are 1 MiB input per artifact, 64 MiB consumed bytes per operation, 5,000
eligible candidates, 4,096 characters per chunk, 1,024 chunks per artifact, and 20,000 chunks per
run. Excluded entries remain visible without consuming eligible-candidate slots. Failed reads count
actual consumed bytes. An artifact exceeding its chunk budget loses its whole partial output;
already completed artifacts can form a partial run. Positive, consistent configuration is required.

Embedding defaults are 20,000 chunks, 64 MiB source input, 128-row content pages, 16 inference
windows per model batch, 32 windows per chunk, 100,000 windows per preparation, and 64 MiB retained
vectors plus serialized provenance. One lock admits either preparation or search. Model load,
inference inactivity, preparation, and search deadlines are supervised. Worker RSS is sampled
against a configurable 2 GiB threshold; this is not an instantaneous operating-system memory cap.

Both extraction and chunking apply the same high-confidence secret screen before persistence.
Matching artifacts are rejected entirely; matching text is not logged or included in diagnostics.
The screen is deliberately narrow, not comprehensive secret detection. Permitted expression
fragments and chunks are sensitive source content stored in PostgreSQL.

## Queries and browser state

Status and history use summary projections; paginated lists use database counts and batched
queries. Chunk lists omit content, and symbol lists omit expression text. Only scoped detail
endpoints retrieve source. Default content access requires compatible active evidence (fresh for
processing). Explicit history can expose compatible retained content with run/freshness metadata.
Quarantined content is inaccessible through both normal and historical endpoints.

Missing parents/explicit run IDs return 404; unavailable current evidence and conflicts return 409;
infrastructure failures return sanitized 503 responses. The API never claims persistence after a
failed database operation. Responses carry structured error codes and relevant locators/provenance.

Frontend state is scoped to project, snapshot, evidence revision, mode, query, and filters. Scope
changes clear selected content; requests are aborted or late responses ignored. Structural and processing refreshes are
coordinated. Outcomes, symbols, imports, changes, chunks, and run history are paginated with server
totals. Semantic results are additionally scoped to the current run, query, filters, and newer searches.
Latest attempts, usable evidence, stale evidence, and quarantine have distinct displays.

## Retention and scope

There is no automatic history deletion. Operational diagnostics expose retained row counts,
stored chunk bytes, expression storage, and pending constraint validation. Real upgrades require a
verified backup; repair validation uses generated disposable databases and actual migrations.
Snapshots are observations, not content archives. If old bytes no longer exist and no compatible
evidence can be reused, create a new snapshot; never relabel current bytes as historical input.

Stage 8 deliberately adds no ANN index, reranking, neighbor expansion, context budgeting, RAG,
agents, import resolution, call graphs, authentication, automatic scanning, or automatic
regeneration. See `CURRENT_STATE.md` for actual verification evidence and remaining limitations.
