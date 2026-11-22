# BuildLens — Resume Evidence Audit

Audit date: 30 October 2026. Checkout: `d6feaf4`.

**Assessment:** BuildLens is strongest as a backend/data-integrity and applied retrieval engineering project. Its most persuasive evidence is transactional source-evidence management, deterministic multi-language architecture analysis, a measured local query optimization, and evaluated cross-encoder reranking. It does not currently support a claim of successful repository question answering, trained ML models, or production adoption.

## 1. Project Summary

### Purpose and implementation

BuildLens helps a developer understand a local repository through searchable source evidence and navigable architecture views. It inventories a registered directory, fingerprints source bytes, parses declarations/imports, produces source-aligned chunks, persists embeddings, and exposes search, evidence exploration, and architecture analysis. Optional generation builds on selected evidence rather than giving a model unrestricted repository access.

The architecture is a **modular monolith**, not microservices:

- React/TypeScript/Vite UI → FastAPI HTTP API → application services and typed ports → PostgreSQL/pgvector stores and filesystem/model adapters.
- Domain objects define evidence identity, ownership, eligibility, producer contracts, and graph/retrieval behavior. Database and web framework dependencies stay outside the application/domain core.
- Explicit snapshot, parsing, processing, embedding, catalog, and architecture operations publish retained runs. Trusted parsing/model/framework code executes in supervised helper processes; repository modules are not imported or executed.
- PostgreSQL is the authority for derived analysis. Architecture and semantic catalog builds operate on stored evidence, not live source reads.

### Completion and readiness

The source implements the documented architecture-intelligence scope, including UI/API/CLI paths. Earlier retrieval, context, framework-laboratory, and semantic-catalog implementations are present. Local verification is recorded through 29 October. This is an implemented local developer tool with substantial verification history, not a demonstrated public service.

Important boundaries:

- Retrieval and real local embedding/reranking evaluation have retained results.
- Real-provider Ask evaluation was operationally unsuccessful: 88 failures and two abstentions, with no answered responses. Answer quality has **not passed**.
- Controlled semantic generation exists, but real-model quality is deferred; the latest documentation says strict-schema acceptance for those adapters has only mock-transport verification.
- Architecture module evaluation passed its defined self-hosted scope; frontend-to-backend API matching still awaits human labels and emits conditional candidates, not confirmed connections.
- Hosted CI has not run according to `CURRENT_STATE.md`. Authentication, ANN indexing, a cross-file call graph, a background job platform, and automatic history deletion are absent.

### Attribution and history

All 40 reachable commits are attributed to `Driv1216 <Driv1216@users.noreply.github.com>`, matching the repository owner. The visible history supports contribution across backend, frontend, migrations, evaluation, and operational documentation. It does **not** establish which implementation decisions or lines were personally authored without assistance. Use “built/implemented” only for work you owned and can explain; do not claim sole, unaided authorship from Git metadata.

The earliest commit already contains development through retrieval, so early design iterations cannot be reconstructed commit by commit. Later history records context/Ask/evaluation, framework experiments, semantic analysis, architecture work, and explicit review repairs. Those repairs are useful interview evidence: publication integrity, worker failure handling, secure reads, evaluation denominator correctness, and cached-evidence validation were treated as engineering problems rather than hidden behind completion labels.

**Audit basis:** local Git history, manifests, architecture/status documents, selected core implementations, migrations, workflow configuration, and retained evaluation artifacts. This audit did not rerun tests, models, databases, provider calls, or benchmarks. “Verified” below means implementation corroborated by available recorded evidence, not a fresh independent execution. No prior development chat was available beyond this request, and no remote repository/deployment availability was checked.

## 2. Technical Stack

| Category | Verified technologies and use |
| --- | --- |
| Languages | Python backend/CLI; TypeScript and TSX frontend; SQL/PostgreSQL migrations and queries. Python, TS/TSX, JS/JSX are also analysis input languages. |
| Backend | FastAPI, Pydantic/Pydantic Settings, Uvicorn, Typer; application services and protocol-based ports. |
| Database | PostgreSQL 16, SQLAlchemy 2, psycopg 3, Alembic; composite ownership constraints, row locks, GIN full-text indexes, pgvector exact cosine search. |
| Parsing and ingestion | Python `ast`, tree-sitter TypeScript/JavaScript grammars, pathspec ignore matching, SHA-256 fingerprints, format-aware Python/Markdown/JSON/TOML/YAML processing. |
| Retrieval and ML inference | Sentence Transformers `all-MiniLM-L6-v2`; Transformers/PyTorch CPU inference; `cross-encoder/ms-marco-MiniLM-L6-v2`; exact/lexical/vector retrieval, reciprocal rank fusion, reranking, bounded adjacent-part expansion. Pretrained inference, not training/fine-tuning. |
| Generation | Optional Google GenAI and OpenAI SDK adapters; separate OpenAI-compatible loopback semantic adapter. Availability/configuration is not evidence of model quality or provider compatibility. |
| Experimental frameworks | LangChain/LCEL, LlamaIndex, BM25/BM25S, MMR, fusion, parent-child and sentence-window retrieval, in-memory and PostgreSQL stores. Confined to the laboratory; not adopted into the main application retrieval path. |
| Frontend | React 18, React Router, Vite, TanStack React Table, Recharts, CSS. Evidence, Explore, Architecture, and laboratory interfaces. |
| Tooling and verification | uv frozen dependencies, npm lockfile, Docker Compose, pytest, Ruff, mypy, Vitest, Testing Library, ESLint, TypeScript checks; GitHub Actions configuration exists, hosted execution unverified. |

Sources: `backend/pyproject.toml`, `frontend/package.json`, `compose.yaml`, `.github/workflows/verify.yml`, model-cache modules and the application/infrastructure directories. Avoid listing every optional library on a one-page resume.

## 3. Strongest Engineering Achievements

Ranked by evidence strength, engineering depth, and how readily a recruiter can understand the result.

### 1. Transactional, incremental source-evidence persistence

**Challenge:** Repeated analyses, changed files, concurrent attempts, and parser-contract changes can silently mix incompatible evidence or replace usable results with a failed run.

**Implementation:** Immutable per-run observations; stable artifact/symbol/chunk identities; exact-byte fingerprints and versioned producer contracts; ownership-enforcing composite foreign keys; snapshot row locks and authority checks before atomic active-pointer publication. Superseded/failed attempts are retained separately. Forward repair migrations quarantine incompatible history rather than deleting it. Embeddings are reused only under matching content/contract identity.

**Outcome:** Enables selective reuse and historical provenance while preventing stale or competing analyses from becoming current evidence. This is correctness engineering; no measured general speedup is established.

**Evidence/confidence:** High implementation confidence; recorded PostgreSQL migration, rollback, competing-publication and failed-retry verification. Inspect `docs/ARCHITECTURE.md`, `application/processing.py`, `application/embeddings.py`, `infrastructure/persistence/processing.py`, and migrations `0006`–`0008`. Laboratory records corroborate reuse of all 2,099 stored vectors in native-chunk indexes with zero recomputation for those builds.

**Role relevance:** SDE/backend **very high**: transactions, schema design, concurrency, migrations. AI/applied AI **high**: embedding lineage and reproducible evidence. Full-stack **high** when connected to the UI’s usable/stale/failed evidence states.

### 2. Evidence-linked multi-language architecture analysis

**Challenge:** Repository architecture cannot safely be inferred from filenames or unrestricted code execution; dynamic and ambiguous imports also prevent complete static certainty.

**Implementation:** Python AST and tree-sitter extraction feed a deterministic semantic catalog and persisted module graph. Iterative Tarjan identifies strongly connected components; bounded BFS produces paths/reachability with cited witness edges and explicit truncation states. Stored evidence is revalidated when serving graph results. API relationships expose assumptions and unknowns instead of promoting candidates to facts.

**Outcome:** Developers can inspect dependencies, cycles, and paths through React views and API/CLI queries without executing repository code. Negative reachability claims are withheld when exploration is incomplete.

**Evidence/confidence:** High for implemented graph behavior; self-hosted evaluator reports 4,682/4,687 correct non-alias import-resolution units with 98.72% eligibility coverage. This is not a broad cross-repository accuracy result or 4,687 distinct graph edges. API matching quality is unverified. Sources: `domain/architecture_graph.py`, `application/architecture*.py`, frontend architecture components, `docs/ARCHITECTURE_INTELLIGENCE.md`, corrected scorer artifact.

**Role relevance:** SDE/full-stack **very high**: algorithms, parsing, data modeling and usable inspection UI. AI/ML **medium**: deterministic grounding infrastructure, not learned architecture inference.

### 3. Reduced architecture-query overhead while retaining evidence checks

**Challenge:** Revalidating thousands of evidence items made the overview slow; simply caching validation would weaken integrity.

**Implementation:** Batched integrity work and a verified immutable graph cache, with fingerprints, assertion/evidence checks and eviction on mismatch. Validation results are not treated as permanently cached evidence.

**Outcome:** Retained local measurements show overview p95 falling from **4.157 s to 0.915 s**, approximately **78% lower**, and full-integrity time from 3.94 s to 0.81 s. The optimization made the local architecture interface more responsive under the measured conditions.

**Evidence/confidence:** Medium-high for the historical measurement; implementation and retained `performance.json` agree with the documented optimization. Owner’s Mac, local PostgreSQL container, 425-file self-hosted corpus, 6,846 evidence items, ten repetitions; no concurrency/load study. Measurements predate the latest remediation, which added a coverage check and did not rerun performance. Do not present this as current production latency.

**Role relevance:** SDE/backend **very high**, full-stack **high**, AI/ML **medium** for serving and evidence-validation efficiency.

### 4. Evaluated pretrained reranking over a source-search pipeline

**Challenge:** Vector similarity alone ranked relevant code evidence too low, and a model context needs complete, traceable source slices within a bounded request.

**Implementation:** Persistent MiniLM embeddings, PostgreSQL lexical and exact-cosine search, deterministic RRF fusion, a local MiniLM cross-encoder, adjacent-part expansion, and whole-item packing sized against canonical UTF-8 JSON. Model weights are explicitly downloaded, revision/checksum checked, and loaded by supervised workers. Long source embeddings use token windows rather than silent truncation.

**Outcome:** On 24 answerable human-reviewed cases, reranking increased macro Recall@5 from **20.64% to 42.94%** and MRR@32 from **0.4560 to 0.5972**. Complete packed evidence rose only from 5/24 to 7/24; the retrieval gain does not establish successful question answering.

**Evidence/confidence:** High for recorded dataset-specific results: `.buildlens-eval/runs/retrieval-20261002/reviewed-results-r1.json`; implementation in `application/retrieval.py`, `application/context.py`, persistence retrieval and model supervisors. Curated relevance is not exhaustive. Semantic and hybrid orders were identical because the lexical branch returned no candidates on these sentence queries. Reranking increased mean evaluation duration from about 1.29 s to 2.72 s; that includes evaluator/model overhead, not isolated serving latency.

**Role relevance:** AI/applied AI **very high**: retrieval evaluation and inference integration. Backend **high**: indexed search, authority checks, bounded hydration. ML research **limited**: no new model or training experiment.

### 5. Reproducible framework evaluation with source-provenance parity

**Challenge:** Comparing retrieval frameworks is misleading when loaders, chunking, evidence identities, and scoring denominators change together.

**Implementation:** An offline laboratory runs LangChain and LlamaIndex in supervised processes, binds returned items to source evidence, checks native parity, supports separate least-privilege vector stores, and distinguishes approved identity labels, source-span coverage and derived labels. Frozen corpus bindings and an excluded evaluation directory reduce benchmark contamination.

**Outcome:** Reproduced native retrieval/ranking/context across all 90 case–variant comparisons and compared framework alternatives without changing the main application path. Store alternatives preserved top-32 membership; apparent framework gains were qualified by larger contexts and uncertain estimates. The work supports a defensible decision to retain the existing implementation rather than claiming a framework win.

**Evidence/confidence:** High for retained offline comparisons: laboratory manifest, `scores.json`, `summary.md`, native adapters and `docs/FRAMEWORK_LAB.md`. Benchmark scope was 30 pipelines × 30 questions, but that is experiment coverage, not product impact. Re-chunked comparisons use a 19-case matched subset; the LangChain ensemble MRR gain’s bootstrap interval includes zero. Generation in this laboratory is an unwired/gated stub; deterministic answer mechanics are not an LLM agent result.

**Role relevance:** AI/applied AI **very high**: experimental design and error analysis. Backend **high**: process/database isolation and reproducibility. Full-stack **medium**: experiment/probe/results UI.

### 6. Safe ingestion and controlled outbound AI requests

**Challenge:** Repository paths can escape their root, hang reads, contain secrets, or change during inspection; provider requests can leak more source than a user authorized.

**Implementation:** Descriptor-relative POSIX reads, no-follow/nonblocking opens, root/device identity checks, bounded reads/deadlines and source-free errors; supervised workers fail closed on failures. Separate semantic-generation plans enumerate allowed source and expire; single-use tickets bind provider calls to exact canonical request digests. Ask validates citation structure and evidence references.

**Outcome:** Prevents specific unsafe reads and unapproved payload changes under the implemented threat model. Synthetic loader experiments found framework defaults following an escaping symlink/loading bait that BuildLens excluded or rejected. Architecture adversarial checks detected all recorded tamper operations.

**Evidence/confidence:** High for code and targeted recorded regression evidence; no claim of exhaustive security. Relevant code: `infrastructure/structural/reader.py`, laboratory guard/supervisor, `application/outbound_gate.py`, `application/semantic_generation.py`. Process isolation is not a universal OS sandbox; tree-sitter memory checks include a post-parse peak-RSS check. Citation validation does not prove semantic correctness or absence of hallucinations.

**Role relevance:** Backend **very high**, applied AI **high** for controlled data sharing and failure handling, full-stack **medium** for reviewable authorization flows.

## 4. Metrics & Evidence

| Result | Defensible interpretation | Limits / supporting evidence |
| --- | --- | --- |
| Recall@5 20.64% → 42.94%; MRR@32 0.4560 → 0.5972 | Reranking improved the recorded source-retrieval ranking on 24 answerable reviewed cases. Recall gain is **22.30 percentage points**, not 22.30%. | Historical self-repository benchmark; curated labels; six unsupported cases excluded. `reviewed-results-r1.json`. |
| Packed-context coverage 27.08% → 33.33%; complete cases 5/24 → 7/24 | Ranking gains translated into a smaller evidence-packing improvement. | Source coverage is not answer correctness; same artifact. |
| Overview p95 4.157 s → 0.915 s | Approximately 78% reduction in the retained local benchmark after batching. | Ten repetitions, one machine/corpus, no load test; latest remediation not rebenchmarked. Architecture `performance.json`. |
| 4,682/4,687 correct units; 4,687/4,748 eligible | 99.8933% precision/recall and 98.7152% coverage in the evaluator’s non-alias import-unit scope. | Not general architecture accuracy; no alias stratum in this corpus; five disagreements remain counted. `module.score.r2.scorer-v2.json`. |
| 2,099 vectors reused, zero computed in each native-chunk laboratory index | Demonstrates verified stored-vector reuse for those unchanged-index builds. | Not a percentage speedup or ongoing operational cost reduction; laboratory build records. |
| 90/90 native-parity comparisons match | Framework harness preserves the reference pipeline’s observations. | Reproducibility evidence, not 90 users or 100% retrieval relevance. Laboratory records. |
| 88/90 real-provider failures, two abstentions, zero answers | Real repository answer quality remains unestablished and operational reliability inadequate in this run. | 69 rate-limit and 19 generation-unavailable failures; both abstentions safe but task-incorrect. README, status and retained answer-run records. |

Recorded verification covers migrated PostgreSQL integration, static checks, frontend build/UI checks, and real local-model gates at their stated checkpoints. Test counts and milestone totals are deliberately not resume impact metrics. No verified user count, uptime, public deployment, production throughput, cross-repository generalization, training improvement, or commercial impact is available.

## 5. Resume Recommendations

### SDE / Backend / Full-Stack

Choose **transactional evidence persistence (#1)**, **architecture analysis (#2)**, and **local query optimization (#3)**. Together they show schema/concurrency correctness, algorithms and parsing, and a measured performance improvement. For a two-bullet entry, combine the graph implementation and optimization, then retain the persistence bullet.

For a full-stack application, mention the React architecture/evidence explorer within the graph achievement. Do not spend a bullet listing pages or CRUD endpoints. For security-oriented backend roles, replace the performance bullet with safe ingestion (#6) if you can explain descriptor-relative reads and race handling more confidently than the benchmark setup.

### AI / ML / Applied AI

Choose **evaluated reranking (#4)**, **framework comparison methodology (#5)**, and **controlled evidence/context handling (#6)**. This project fits applied AI/retrieval and ML systems internships better than model-development research positions. Keep pretrained inference distinct from model training. If space permits only two bullets, retain reranking and framework evaluation; briefly mention evidence bounds within the reranking bullet.

These choices are technically relevant across the target geographies. No hiring-market preference for India or East Asia was researched; avoid claiming geographic tailoring beyond clear English, explicit internship-relevant engineering, and readily explainable evidence.

### Claims to avoid or qualify

- “Production-grade RAG assistant,” “successful AI code explanations,” “high answer accuracy,” or “hallucination-free”: unsupported by provider outcomes.
- “Hybrid search outperformed vector search”: false for the retained native sentence-query benchmark.
- “99.9% architecture accuracy”: collapses a narrow import-unit evaluation into a universal claim.
- “78% faster application”: overgeneralizes one local architecture overview measurement.
- “Scalable vector search”: exact cosine is implemented; ANN and scale/load benchmarks are absent.
- “Microservices,” “autonomous agent,” “trained/fine-tuned MiniLM,” “deployed CI/CD”: not established. Experimental agent mechanics do not change that.
- Framework name lists, total tests, migrations, endpoints, milestones, arbitrary RRF constants, or the 900 experiment executions as impact.
- “Zero data leakage” or “secure sandbox”: controlled source access and request gating have a bounded threat model, not a universal guarantee.
- Claiming every feature is current production behavior: laboratory experiments and optional semantic interpretations are separate paths.

Before using a bullet, be ready to explain its failure cases: publication races and stale evidence; stable identity versus observation UUIDs; AST/tree-sitter limitations; Tarjan/BFS behavior; batch validation/cache invalidation; macro Recall/MRR and denominators; reranking cost; source-span versus answer quality; and synthetic versus real-provider evidence.

## 6. Candidate Resume Bullets

These are selectable drafts, not a recommendation to place six bullets on one resume. Personal ownership must match your actual work.

### SDE / Backend / Full-Stack

- Built a FastAPI/PostgreSQL evidence pipeline with atomic run publication, ownership constraints, and content-based reuse, preserving usable analysis across failures and concurrent updates.
- Implemented Python/TypeScript dependency analysis with AST/tree-sitter, iterative Tarjan cycle detection, and cited graph paths exposed through a React architecture explorer.
- Batched architecture evidence checks, reducing overview p95 from 4.16 s to 0.92 s in a local 425-file benchmark while retaining per-request validation.

### AI / ML / Applied AI

- Integrated MiniLM embeddings and cross-encoder reranking for code retrieval, increasing macro Recall@5 from 20.6% to 42.9% on 24 human-reviewed answerable cases.
- Built an offline LangChain/LlamaIndex evaluation harness with frozen corpus bindings and source-span scoring, reproducing native retrieval, ranking, and context across 90 comparisons.
- Engineered evidence-bounded AI requests with whole-source context packing, citation validation, and digest-bound authorization for semantic generation to constrain source sharing.

The third AI bullet describes implemented controls, not demonstrated generation quality. If that distinction is difficult to convey in an interview, use only the first two.

## 7. Project Links & Status

- Repository remote: [github.com/Driv1216/Build-Lens](https://github.com/Driv1216/Build-Lens). Verified from local Git configuration; remote visibility and availability were not checked.
- Audited commit: [d6feaf4](https://github.com/Driv1216/Build-Lens/commit/d6feaf4). Latest recorded commit: 29 October 2026, 19:40 IST.
- Earliest reachable commit: 19 September 2026. Documentation records foundation verification on 14 September; that earlier date is a documented checkpoint, not a visible commit or proven project start.
- Suggested resume date range: **Sep–Oct 2026** for the evidenced work. Use “Sep 2026–Present” only if you are actively continuing development; do not infer it from an unfinished evaluation.
- Demo: no public demo link verified. Local setup serves the frontend at `127.0.0.1:5173` and API at `127.0.0.1:8000`; these are setup addresses, not hosted demonstrations.
- Readiness: credible for a local technical walkthrough of ingestion, retrieval, evidence history and architecture exploration. Check local services/models before an interview. Do not rely on cloud generation as the main demo.
- Latest implementation includes review-remediation commit `d6feaf4`; independent re-review remains unperformed. API labels and semantic quality evaluation remain pending/deferred.

### Evidence navigation

Tracked sources: `README.md`, `CURRENT_STATE.md`, `docs/ARCHITECTURE.md`, `docs/EVALUATION.md`, `docs/FRAMEWORK_LAB.md`, `docs/SEMANTIC_MODEL.md`, `docs/ARCHITECTURE_INTELLIGENCE.md`; backend `domain/`, `application/`, `infrastructure/`, Alembic versions, and frontend evidence/explore/architecture/lab components.

Retained local sources inspected directly:

- `.buildlens-eval/runs/retrieval-20261002/reviewed-results-r1.json`
- `.buildlens-eval/lab/runs/bench-r1-20261024b/summary.md`
- `.buildlens-eval/reviews/architecture-evaluation-20261029/module.score.r2.scorer-v2.json`
- `.buildlens-eval/reviews/architecture-evaluation-20261029/performance.json`

These evaluation directories are intentionally Git-ignored and may not be available to a recruiter opening the repository. Preserve the originals; if publishing evidence later, prepare a sanitized summary that retains dataset scope, denominators, failed outcomes and measurement conditions. The public-facing documentation should also be reconciled before sharing: `docs/ARCHITECTURE.md` still opens with the older semantic-model scope, and backend package metadata still describes the earlier framework laboratory despite newer implemented architecture work.
