# BuildLens

BuildLens is a local engineering-intelligence application, implemented through **Phase 2,
Stage 8 — Lexical & Hybrid Retrieval**. It uses a modular Python backend, React/Vite UI,
and PostgreSQL. Operations start only when you explicitly request them.

The foundation repair preserves projects, snapshots, and historical records. Pre-repair
structural and processing runs are quarantined: their content remains stored, but normal and
historical content endpoints cannot expose it. Regeneration is explicit.

Embedding runs and immutable per-run vector observations are persisted in PostgreSQL/pgvector.
Exact, PostgreSQL full-text lexical, exact-cosine semantic, and deterministic RRF hybrid retrieval
operate over current evidence. Semantic/hybrid modes are explicitly prepared; exact/lexical modes
need no ML packages or model cache. Retained history is metadata-only. No ANN index, import resolution, call graph, agents, authentication, background job
platform, or automatic history deletion are included. See
[current verification](CURRENT_STATE.md) and [architecture](docs/ARCHITECTURE.md).

## Local setup

Install Python 3.11 or 3.14, uv 0.12.3, Node.js 22, and Docker. From the repository root:

```sh
cp .env.example .env
```

Choose a local `POSTGRES_PASSWORD` and set the corresponding URL-encoded password in
`BUILDLENS_DATABASE_URL`. The application requires a PostgreSQL URL; it has no credential
fallback. Compose binds PostgreSQL to `127.0.0.1:5432`.

**Changing Compose variables does not rotate the password of an existing PostgreSQL volume.**
Rotate an existing role's password using PostgreSQL administration, then update the URL.

```sh
docker compose up -d db
cd backend
uv sync --frozen --extra dev --no-editable
uv run --no-sync alembic upgrade head
PYTHONPATH=src uv run --no-sync uvicorn buildlens.api.main:app --host 127.0.0.1 --port 8000
```

The base install can inspect persisted status/history and run exact/lexical retrieval without
importing ML libraries. To prepare new vectors or use semantic/hybrid retrieval, install the frozen
optional extra and explicitly acquire the pinned,
checksum-recorded model. API startup, preparation, and search never download it:

```sh
cd backend
uv sync --frozen --extra dev --extra embeddings --no-editable
uv run --no-sync buildlens embeddings download-model
```

For an existing installation, follow the backup procedure below before upgrading. In another
terminal:

```sh
cd frontend
npm ci
npm run dev -- --host 127.0.0.1
```

Open `http://127.0.0.1:5173`. The default API URL is `http://127.0.0.1:8000/api/v1`.
The UI registers a path, takes a snapshot, analyses Python, processes artifacts, explicitly
prepares embeddings when needed, and searches current chunks in four modes. Each evidence panel identifies the latest
attempt separately from usable, stale, historical, or quarantined evidence.

`PYTHONPATH=src` keeps development execution pointed at current source while the frozen
installation also verifies the package and existing `buildlens` CLI entry point.

## API

All paths below are under `/api/v1`. Mutations use JSON, including `{}` for explicit operations.

| Scope | Endpoints |
|---|---|
| Health | `GET /health` |
| Projects | `GET, POST /projects`; `GET /projects/{project_id}` |
| Snapshots | `POST /projects/{project_id}/snapshots`; `GET .../snapshots/latest`; `GET .../snapshots/{snapshot_id}` |
| Inventory | `GET .../{snapshot_id}/artifacts`; `GET .../{snapshot_id}/changes` |
| Structure | `POST .../{snapshot_id}/structure`; `GET .../structure`; `GET .../structure/status` |
| Structural evidence | `GET .../structure/artifacts`, `/symbols`, `/symbols/{symbol_id}`, `/imports` |
| Structural history | `GET .../structure/runs`; `GET .../structure/runs/{run_id}` |
| Processing | `POST .../{snapshot_id}/processing`; `GET .../processing` |
| Processing evidence | `GET .../processing/artifacts`, `/chunks`, `/chunks/{chunk_id}` |
| Processing history | `GET .../processing/runs`; `GET .../processing/runs/{run_id}` |
| Embeddings | `GET, POST .../{snapshot_id}/embeddings` |
| Embedding history | `GET .../{snapshot_id}/embeddings/runs`; `GET .../embeddings/runs/{run_id}`; `GET .../embeddings/runs/{run_id}/rows` |
| Retrieval | `POST .../{snapshot_id}/search` with explicit `exact`, `lexical`, `semantic`, or `hybrid` mode |
| Semantic compatibility | `POST .../{snapshot_id}/semantic-search` |

Pages accept `limit` and `offset`. Evidence endpoints accept an explicit `run_id` for compatible
retained history. Default chunk access requires fresh, compatible active evidence. Chunk lists
contain metadata; only chunk detail retrieves source content. Responses include structured error
codes: missing parents or run IDs return 404, unavailable current evidence/conflicts return 409,
and infrastructure failures return sanitized 503 responses.

The common search endpoint has no pagination and accepts `top_k` from 1–50. Candidate lists are
fixed at 100 and report bounded counts and truncation. For example:

```sh
curl -sS -X POST "$API/projects/$PROJECT/snapshots/$SNAPSHOT/search" \
  -H 'Content-Type: application/json' \
  -d '{"mode":"exact","query":"Client.get_user","top_k":10}'
curl -sS -X POST "$API/projects/$PROJECT/snapshots/$SNAPSHOT/search" \
  -H 'Content-Type: application/json' \
  -d '{"mode":"lexical","query":"HTTP server","filters":{"language":"python"}}'
curl -sS -X POST "$API/projects/$PROJECT/snapshots/$SNAPSHOT/search" \
  -H 'Content-Type: application/json' \
  -d '{"mode":"semantic","query":"where are users loaded?","top_k":5}'
curl -sS -X POST "$API/projects/$PROJECT/snapshots/$SNAPSHOT/search" \
  -H 'Content-Type: application/json' \
  -d '{"mode":"hybrid","query":"load user","top_k":10}'
```

Snapshots record names, policy outcomes, Git metadata, and exact-byte SHA-256 fingerprints.
They are observations, not filesystem checkpoints or live-disk freshness claims. Git's executable
hooks, filesystem monitor, configured content filters, global/system configuration, and optional
index writes are disabled. Ignore controls use descriptor-relative reads and Git-compatible
matching for root and nested `.gitignore` files. Ignore rules above the registered root are
unavailable; that scope fails closed rather than reading outside it.

## Upgrade and retained history

Migrations `0001`–`0005` retain their upgrade semantics. Forward migrations `0006` and `0007`
quarantine old derived runs and repair ownership. Migration `0008` requires pgvector 0.8.6 in
`public` and creates persistent embedding evidence. Migration `0009` adds the immutable lexical
normalizer and GIN expression indexes without regenerating evidence. Test upgrades in disposable databases first.
For a real installation, stop the API and **take and verify a backup before migration**:

```sh
docker compose exec -T db pg_dump -U buildlens -Fc buildlens > buildlens-before-repair.dump
cd backend
uv run --no-sync alembic upgrade head
uv run --no-sync buildlens diagnostics
```

The Alpine-to-pgvector image transition must use logical backup/restore into a new volume; never
attach the old Alpine volume to the Debian image. Follow the complete
[Stage 7 rollout runbook](docs/STAGE7_ROLLOUT.md).

Quarantined legacy violations are preserved under PostgreSQL `NOT VALID` constraints. New
writes must satisfy those constraints; diagnostics explicitly lists pending validation. Review
legacy violations before validating them. The repair never deletes historical rows to make a
constraint pass. Repair downgrades refuse unsafe rollback; restore a compatible backup instead.
Stage 4 downgrade also refuses when multiple retained structural runs violate the older model.

Run counts and stored-content sizes grow intentionally. `buildlens diagnostics` reports counts,
chunk bytes, vector storage, embedding outcomes, and unvalidated constraints without exposing
content. `buildlens doctor` verifies connectivity, extension version/schema, and a cosine probe.
There is no automatic deletion policy.

Regenerate through the UI/API only when the named snapshot's bytes are available or compatible
verified evidence can be reused. If old snapshot bytes no longer exist, preserve that snapshot
and create a new one. Never relabel current bytes as an old snapshot.

## Verification

From `backend/`, use a disposable PostgreSQL service whose administrative role may create and
drop test databases. Fixtures generate a unique database per test and apply actual migrations;
they never drop the supplied administrative database or substitute SQLite.

```sh
export BUILDLENS_TEST_ADMIN_URL=postgresql+psycopg://postgres@127.0.0.1:55439/postgres
PYTHONPATH=src uv run --no-sync pytest
BUILDLENS_EMBEDDINGS_CACHE_DIR=/path/to/cache uv run --no-sync pytest -m real_model
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy
```

Missing PostgreSQL configuration is an explicit skip locally and a hard failure in CI. The CI
matrix requires PostgreSQL tests on Linux with Python 3.11 and 3.14, frozen uv dependencies,
and `npm ci` followed by frontend tests, lint, type checking, and build.

The `real_model` gate requires the pinned model to have been acquired explicitly and fails rather
than skipping when its cache is missing. See [CURRENT_STATE.md](CURRENT_STATE.md) for actual
verification evidence and remaining operational gates.
