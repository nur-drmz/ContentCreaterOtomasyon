# ADR-004 — Object / Media Storage

## Status
Accepted

## Context
Current storage is local filesystem under `<root>/workspaces/<workspace_id>/projects/<project_id>/...`. Namespace isolation is enforced by `Storage.resolve()`.

## Decision
Canonical production object storage with a local filesystem development adapter. Media never becomes a client-supplied arbitrary path.

| Asset type | Storage tier | Key pattern |
|---|---|---|
| Source video | object storage / cold archive | `{tenant}/{workspace}/{project}/source/{sha256}.ext` |
| Proxies/thumbnails/waveform | object storage | `{tenant}/{workspace}/{project}/preview/{asset_id}` |
| TTS audio | object storage | `{tenant}/{workspace}/{project}/audio/{asset_id}.wav` |
| Subtitles | object storage | `{tenant}/{workspace}/{project}/subs/{asset_id}.srt` |
| Preview renders | object storage | `{tenant}/{workspace}/{project}/preview/{asset_id}.mp4` |
| Final renders | object storage (+ CDN) | `{tenant}/{workspace}/{project}/render/{asset_id}.mp4` |
| B-roll / music / stock | object storage | `{tenant}/{workspace}/{project}/stock/{asset_id}` |
| Temporary intermediates | ephemeral object storage / local | cleaned after job |

Immutable asset IDs are random opaque strings (e.g., `new_id()` style). SHA-256 is used for integrity and duplicate detection, not as the object key for user-visible assets. Storage keys are prefix-isolated by `tenant/workspace/project`.

Signed access:
- Backend generates presigned URLs or signed cookies for browser direct upload/download.
- Browser never sees bucket credentials.

Retention:
- Preview renders: project lifetime.
- Final renders: project lifetime + optional archive policy.
- Source video: until explicit deletion by workspace owner.
- Temp intermediates: TTL + job-end cleanup.

Local development adapter:
- Keeps the same namespace concept under `CONSOLE_ROOT`.
- Maps object keys to relative paths.
- No code changes in `local_pipeline`.

## The materialize lifecycle (clarified in Milestone 1)

`local_pipeline` and ffmpeg require a real local filesystem path. Rather than
teaching the deterministic media engine about buckets or URLs, the port
(`application/ports/object_store.py`) makes the round trip explicit and owned by
the application layer:

```
ObjectStore key
      │  materialize()      → download to a local working copy
      ▼
local filesystem path
      │
      ▼
local_pipeline / ffmpeg
      │
      ▼
local filesystem path
      │  put_file()         → publish the result back
      ▼
ObjectStore key
```

`materialize()` is a **context manager** so a cloud adapter can delete its
temporary copy deterministically. In the local adapter the stored file already IS
the working copy, so it yields the real path and cleans up nothing — development
pays nothing for an abstraction that only matters in production.

Consequently `local_pipeline` never learns about cloud storage, and no signed URL
or bucket name reaches it.

## Where the swap will happen (clarified in Milestone 3)

`SourceService` is now the single place that turns "the source attached to this
project" into a path a media tool can read:

```
SourceService.get_source_path()          # local mode, today
    → ProjectNamespaceStore.resolve(ownership, project.source.stored_path)

SourceService.get_source_path()          # object-storage mode, later
    → ObjectStore.materialize(ownership, key)   # temporary local copy
```

Readers consume it through `ports.SourceQuery`, so `EditorService` (and any
later Analysis/Render service) is already insulated: they ask for a path and
never learn which tier produced it. Uploads already stream through a
`.part` + `fsync` + `os.replace` sequence, which maps onto a multipart upload
followed by an atomic publish.

Two things must be resolved when the cloud adapter lands, and are recorded here
rather than discovered later:

- The source descriptor currently stores a namespace-**relative path**
  (`project.source.stored_path`). Object storage wants an **opaque asset key**
  per the table above. Both can coexist (`stored_path` for legacy projects, a
  new `asset_key` field), but the resolution order must be explicit.
- `source_strategy.source_fingerprint_sha256` digests `(basename, size, head,
  tail)` by seeking the file. Over a materialized copy that is unchanged; over a
  remote object it must not become a full download. Range reads cover it.

## Consequences
- The object-store port is separate from the repository ports: repositories persist structured state, the object store persists bytes.
- `local_pipeline` continues to receive plain local paths, produced by `materialize()`.
- A missing object raises rather than yielding empty bytes, so a later stage cannot treat a zero-byte file as valid media.
- Backward compatible: existing local projects stay valid.

## Rejected alternatives
- A. Serve media from PostgreSQL. Rejected: scalability and backup issues.
- B. Use client-supplied S3 paths. Rejected: security, ownership, and lifecycle control loss.

## Migration path
1. Add `ObjectStorage` interface with `LocalObjectStorage` implementation.
2. Migrate `Storage.resolve()` to `Storage.get_url()` and `Storage.put_stream()`.
3. Add S3/MinIO adapter behind `MEDIA_STORAGE_BACKEND`.
4. Run media tests against both adapters.
