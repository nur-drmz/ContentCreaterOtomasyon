# ADR-003 — PostgreSQL Persistence

## Status
Accepted

## Context
All state is currently JSON on local filesystem. `creator_console/storage.py` defines a `StorageAdapter` interface, so a backend swap is already anticipated.

## Decision
Canonical SaaS persistence in PostgreSQL. Local filesystem remains the development adapter. Repository interfaces abstract both.

| Entity | Storage | Notes |
|---|---|---|
| users | PostgreSQL | authn + roles |
| workspaces | PostgreSQL | tenant boundary |
| workspace membership | PostgreSQL | `(user_id, workspace_id, role)` |
| projects | PostgreSQL | metadata; `project_id` namespace kept |
| artifact metadata | PostgreSQL | path, version, sha256, status, created_at, actor |
| immutable artifact versions | PostgreSQL + object storage | metadata in DB; blob outside |
| jobs | PostgreSQL | source of truth for status, lease, attempts, params |
| job attempts | PostgreSQL | one row per attempt |
| approvals | PostgreSQL | content-digest bound, policy reference |
| workflow/stage state | PostgreSQL | for turbo/long-form stage machines |
| audit events | append-only table / log sink | not JSON blobs |
| connected publishing accounts | PostgreSQL | encrypted tokens referenced by `secret_ref` |
| automation definitions | PostgreSQL | trigger/policy JSON |
| automation runs | PostgreSQL | one row per run |
| publication records | PostgreSQL | remote_id, platform, visibility, audit link |
| usage events | PostgreSQL + time-series sink | billing/observability |

What must NOT go into PostgreSQL as blobs:
- source video
- proxies / thumbnails / waveform
- TTS / subtitle files
- preview/final renders
- B-roll / music / stock downloads
- temporary intermediates

These belong in object storage. Metadata (path, sha256, size, mime) lives in PostgreSQL.

## One canonical backend per project (supersedes the earlier dual-write plan)

**General dual-write is NOT the migration strategy and must not be implemented.**

An earlier revision of this ADR proposed dual-writing every operation to both the
local JSON store and PostgreSQL during the transition. That is rejected:

- Two writes cannot be made atomic across a filesystem and a database, so a crash
  between them silently produces two disagreeing sources of truth.
- Job claim/lease is a compare-and-set. Running it against two stores makes "who
  owns this job" ambiguous, which is precisely the guarantee the job model exists
  to provide.
- Reconciling divergence needs a third component nobody asked for.

The rule instead:

> At any moment, one workspace/project has exactly ONE canonical persistence
> backend.

```
PersistenceBackend
    ├── LocalBackend         # today, and the development default forever
    └── PostgreSQLBackend    # a later milestone
```

Normal runtime therefore never writes an operation to both systems. Moving a
project between backends is an **explicit, offline-ish cutover operation**, not a
side effect of ordinary writes:

1. Freeze writes for that project (refuse new jobs; wait for in-flight jobs).
2. Copy all structured state into the target backend.
3. Verify: record counts, artifact digests, job statuses, audit event count and
   ordering must match.
4. Flip the project's canonical backend pointer.
5. Unfreeze. The source data is retained read-only until the cutover is accepted.

A failed verification aborts the cutover and leaves the original backend
canonical, which is what makes the operation safe to retry.

## Consequences
- `StorageAdapter` gains a `PostgresStorage` and `S3Storage` family.
- Transaction boundaries must include artifact metadata + audit event writes.
- Existing local projects continue working because the `LocalFilesystemStorage` adapter is retained.
- `JobStore` uses PostgreSQL row locks or advisory locks for claim/lease.
- A per-project "which backend is canonical" pointer is required before any PostgreSQL adapter ships.

## Rejected alternatives
- A. Keep JSON files and add a sync layer. Rejected: does not solve multi-tenant race conditions.
- B. Put media in PostgreSQL bytea. Rejected: violates size, backup, and CDN goals.
- C. Dual-write every operation to both backends. Rejected: non-atomic across two systems, and it makes job claim ownership ambiguous.

## Migration path
1. Define the ports from the operations the code actually performs (done in Milestone 1: `application/ports/`).
2. Bind the existing local implementations to those ports (done in Milestone 1).
3. Add a PostgreSQL implementation of the same ports behind configuration.
4. Add the explicit per-project copy + verify + cutover operation described above.
5. Migrate projects one at a time; no dual-write at any point.
