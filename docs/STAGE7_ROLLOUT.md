# Stage 7 pgvector rollout

Stage 7 requires PostgreSQL 16 with pgvector 0.8.6 installed in `public`. Compose and CI pin the
multi-platform manifest for `pgvector/pgvector:0.8.6-pg16-bookworm`. Migration `0008` checks the
extension binaries, creates exactly version 0.8.6 when absent, and refuses a different version or
schema. Application startup never installs packages or upgrades extensions.

## Existing Compose databases

Do not attach an Alpine PostgreSQL data directory directly to the Debian pgvector image. Stop API
writes, retain the old volume, and use a logical migration:

1. Start the old `postgres:16-alpine` image against the old volume on an isolated port.
2. Create a custom-format `pg_dump` for every non-template database and verify every archive with
   `pg_restore --list`.
3. Start the pinned pgvector image with a new volume (`postgres-data-v2`).
4. Create each database from `template0`, restore its archive, and compare database/table counts.
5. Run `alembic upgrade head`, `buildlens doctor`, and `buildlens diagnostics`.
6. Restart PostgreSQL and repeat `buildlens doctor` plus representative semantic searches.
7. Keep the old volume and verified backups until acceptance and rollback windows close.

Do not delete the old volume as part of this migration. Downgrade refuses to remove Stage 7 objects
when embedding history exists; restore a compatible logical backup instead. An empty-schema
downgrade leaves the shared `vector` extension installed.

## Managed PostgreSQL

Ask the database administrator to install the PostgreSQL 16 pgvector 0.8.6 package. Either let the
migration create the extension with sufficient privilege or pre-provision `vector` version 0.8.6 in
`public`; then run the same migration. Missing binaries, insufficient privileges, wrong versions,
or wrong schemas are actionable migration failures.

## Local rollout evidence (2026-09-18)

The stopped legacy volume `buildlens_postgres-data` contained PostgreSQL 16.15, no pgvector
extension, databases `buildlens`, `buildlens_stage4_test`, and `postgres`, and a Stage 7 source
database at `0007_evidence_ownership` with zero projects, snapshots, processing runs, and chunks.
Both application databases were dumped in custom format and verified. They were restored into the
separate `buildlens_postgres-data-v2` volume; `buildlens_stage4_test` remained at its historical
`0004_stable_incremental` revision with two projects. The `buildlens` database migrated to
`0008_persistent_vector_evidence`; doctor verified `vector` 0.8.6 in `public` and a cosine probe.
Source-free diagnostics reported all application/vector tables empty and no unvalidated
constraints. The database was restarted and the probe passed again. The original volume remains
retained; verified temporary logical backups are under `/tmp/buildlens-stage7-backup` for this
workstation session.

Disposable migration verification also covered an administrator-preprovisioned extension and a
database owner without extension-install privilege. A pinned plain PostgreSQL 16 Bookworm image,
without pgvector binaries, failed at `0008` with the expected actionable requirement instead of
partially publishing Stage 7 schema. The final local empty database was downgraded to `0007`,
re-upgraded to the finished `0008` constraint set, and passed doctor/diagnostics plus a database
restart probe.
