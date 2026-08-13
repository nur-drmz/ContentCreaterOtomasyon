# Local Pre-Alpha Media Lifecycle

**Status:** Approved planning baseline

## 1. Storage roots

```text
G:\Youtube kaynak videolar
G:\AIContent\cache\<project-id>
G:\AIContent\outputs\<project-id>
G:\AIContent\trash
D:\AIContent\models
D:\AIContent\backups
```

`G:\Youtube kaynak videolar` is a user-managed, read-only source root from the
application's perspective. The other G: roots are application-managed.

G: and H: are partitions on the same physical HDD; H: must not be described as
a backup destination. Large-video backups require a future separate physical
disk and are not included in Pre-Alpha.

## 2. Source registration

- Only regular files discoverable under `G:\Youtube kaynak videolar` may be
  registered as project sources.
- Supported containers: MP4, MKV, MOV.
- Reject sources over 20 GB or 180 minutes.
- Compute a content hash before or during import.
- Store a path relative to the source-library root.
- Do not copy, move, rename, trash or delete a source file.
- Detect a missing or externally renamed source and show a relink action.

## 3. Retention

| Asset | Retention |
|---|---|
| Original source | User-managed; application never deletes it |
| Selected rough cuts | Until explicit user deletion |
| Timeline JSON / captions | Persistent with project |
| Transcript / scenes / scores | Persistent with project |
| Proxy / keyframes / sampled frames | 14 days after last use |
| Failed partial artifacts | 24 hours unless needed for retry |
| Trash | 7 days, recoverable |
| Metadata backups | 7 daily copies |

Cleanup may never delete an original source or selected rough cut.

## 4. Safe deletion

- Project deletion removes database records and application-managed artifacts
  only after confirmation; it never deletes the source file.
- Application-generated outputs may be moved to the application trash after a
  separate confirmation.
- Restore is available until output-trash expiry.
- Expired trash deletion records an audit event and verifies the resolved path
  is still inside the application trash root.

## 5. Disk guards

- Below 50 GB free on G:: show a persistent warning.
- Below 20 GB free: block new import, proxy generation and render.
- Existing metadata browsing and cleanup remain available.
- Before each media stage, estimate required temporary space and fail early
  with a structured `INSUFFICIENT_DISK_SPACE` reason.

## 6. Backups

Daily backup includes:

- SQLite database using a consistent backup operation;
- application settings excluding secrets;
- timeline manifests and caption files.

Backup does not include source/proxy/render media. Restore must be tested before
the Pre-Alpha is considered complete.
