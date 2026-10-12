# SourceService

> Introduced in Milestone 3, the second application service. It owns the source
> media attached to a project and is the single writer of the `source` block of
> the `project` artifact.

## Why this service came before `ProjectService`

Milestone 2 left `EditorService` holding two `PipelineService` bound methods
(`source_path`, `source_duration_sec`) — a façade back-reference hidden behind
lambdas that no import guard could see. Those two facts are *source* knowledge,
not project lifecycle, so extracting the source boundary removed the last
façade dependency from the editor. A `ProjectService` would not have.

## Responsibilities

| Method | Responsibility |
|---|---|
| `get_descriptor()` | the `source` block of the project artifact, or `None` |
| `get_source_path()` | resolve the attachment to an existing **local** path, or raise |
| `get_duration()` | measured duration in seconds, or `None` when unknown |
| `get_fingerprint()` | the sampled digest approvals are bound to |
| `store_upload(...)` | attach an owned upload from bytes |
| `store_upload_stream(...)` | attach an owned upload from a stream, without buffering |
| `attach_downloaded_source(...)` | attach an already-downloaded file (trend-ingest hand-off) |

`get_source_path` + `get_duration` + `get_fingerprint` are exactly the
`ports.SourceQuery` contract that `EditorService`, `AnalysisService`,
`VideoAnalysisService` (Milestone 10) and — since Milestone 11 —
`MomentService` depend on. MomentService uses `get_source_path` to build the
validation `EDLClip` and `get_duration` as the clip-bounds upper bound —
`None` stays `None` (no fabricated cap).

### Deliberately outside

| Concern | Stays in | Why |
|---|---|---|
| HTTP body streaming, `Content-Length`, disconnects, upload size limit | `creator_console/api.py` | transport owns the wire; the service starts from a valid stream + expected length |
| Source **assessment** (`run_source_assessment`) | `AnalysisService` (Milestone 4) | it consumes `hot_moments`, reads a provider credential and feeds the processing plan — assessment/planning workflow |
| Trend discovery, candidate probe, rights decision, download | `PipelineService` ingest flow | research and rights are not source ownership |
| Video analysis (`hot_moments` production) | `VideoAnalysisService` (Milestone 10) — it *reads* the source through `SourceQuery`, never attaches one | analysis consumes the source, it does not own it |
| Moment selection + clip bounds | `MomentService` (Milestone 11) — reads the source through `SourceQuery`, never attaches one | selection consumes the source, it does not own it |
| Transcript, script, TTS | `PipelineService` | not source concerns |
| Source-library root policy | `ConsoleApp.import_source_from_library` | which server directory may be imported from is operator configuration, enforced before any stream reaches the service |
| Path traversal / namespace rules | `ports.ProjectNamespaceStore` | one implementation of the security rules, never a second copy |
| Fingerprint + probe algorithms | `local_pipeline` (`source_strategy`, `render_utils`) | deterministic media logic |
| Project lifecycle (rename, reset, rewind, delete, archive) | `PipelineService` | its own milestone |

## Source metadata authority

| Fact | Authoritative source | Fallback | Error behaviour |
|---|---|---|---|
| **path** | `project.source.stored_path`, resolved through the namespace store | none | missing attachment → `PipelineServiceError`; manifest present but file gone → `ArtifactProblem`; corrupt project artifact → `ArtifactProblem` |
| **duration** | `source_assessment.source.duration_sec`, written by `AnalysisService` (what ffprobe measured in Phase 0) | a `probe_media` re-probe of the attached file — repaired in Milestone 8, see below | probe failure → `None`; never a default, because an invented duration mis-scales every clip band |
| **technical metadata** (width/height/audio/codec) | `source_assessment.source` via `source_strategy.probe_source` | none | not re-derived by this service |
| **fingerprint** | `project.source.fingerprint_sha256`, computed by `source_strategy.source_fingerprint_sha256` over `(basename, size, head, tail)` | none | `None` when no source is attached |
| **content digest** | `project.source.content_sha256` for uploads; **`None`** for ingests | none | never faked — a multi-GB ingest is not read into memory to hash it |
| **assessment** | the `source_assessment` artifact | none | corrupt → `ArtifactProblem` from the artifact repository |

### The duration fallback (repaired in Milestone 8)

When the assessment cannot answer — absent artifact, a `source` block with no
recorded `duration_sec`, a `0` duration, or an artifact that fails validation —
`get_duration` probes the attached file through the canonical call:

```python
ffmpeg_bin, ffprobe_bin = self._binaries.resolve()
info, _backend = render_utils.probe_media(
    ffmpeg_bin, ffprobe_bin, self.get_source_path())
```

This is the same `local_pipeline` function Phase 0 uses — ffprobe when
available, otherwise parsing `ffmpeg -i` output — so the fallback measures the
same file the same way. Deterministic probing stays in `local_pipeline`.

**History.** The fallback was dead code from Milestone 3 through 7: the call
was `probe_media(path, ffprobe)` — wrong arity *and* wrong order — and the
resulting `TypeError` was swallowed by `except Exception`, so assessment-less
projects always reported `None`. Milestone 8 repaired the call and narrowed
the exception boundary.

**Post-fix error semantics.**

| Failure | Result |
|---|---|
| no project / no registered source / file deleted (`PipelineServiceError`, `ArtifactProblem`) | `None` |
| both probe strategies fail, or no ffmpeg exists (`RuntimeError` — `probe_media`'s documented failure) | `None` |
| binary unspawnable / file unreadable at OS level (`OSError`) | `None` |
| probe result without `format.duration` | `None` |
| unexpected programming error (`TypeError`, …) | **propagates** — no longer disguised as "duration unknown" |

`None` still means *unknown*, never zero or a substitute: a `None` duration
leaves clip-bounds editing without an upper bound, exactly as before, while a
probed duration now enforces it. Consumers enumerated in `ANALYSIS_SERVICE.md`
→ "Consumers affected" now observe the real value.

`AnalysisService` is the only writer of the assessment, so duration authority
now has a single owner. `SourceService` still reads that artifact directly and
never calls `AnalysisService`, which keeps the two services acyclic.

## Storage and materialization

Local mode, today:

```
SourceService.get_source_path()
    → ProjectNamespaceStore.resolve(ownership, stored_path)
    → real local path → local_pipeline / ffmpeg
```

Uploads are streamed in 8 MiB chunks to a hidden `.<name>.part` sibling, fsynced,
then `os.replace`d into place — so a crashed or disconnected upload never leaves
a half-file that looks like a valid source. The partial file is removed on any
exception, including `BaseException`.

Future object storage (ADR-004) changes only the first hop:

```
SourceService → ObjectStore.materialize(ownership, key)
    → temporary local path → local_pipeline / ffmpeg → cleanup on exit
```

`local_pipeline` must never learn about buckets, signed URLs or credentials.
`SourceService` is the seam where that swap happens; nothing above it changes.

## Rights boundary

```
Owned / local source (upload, source library)
    → SourceService.store_upload*          # no rights gate needed: the operator owns it

External / trend candidate
    → discover_source_candidates           # research
    → probe_source_candidate               # metadata only
    → rights decision artifact             # hash-bound human decision
    → run_source_ingest  (gate enforced)   # refuses without an approved decision
    → download + ingest_manifest
    → SourceService.attach_downloaded_source
```

`attach_downloaded_source` performs **no rights check of its own** and is
documented as reachable only after the gate has passed. It cannot be used to
turn a third-party URL into an approved production source: it takes an
already-downloaded path plus the ingest manifest. The `source.registered` audit
event for the ingest path is still recorded by the ingest flow (with `job_id`
and licence detail), so the attach step does not double-record it.

## Dependencies

- `ports.ArtifactRepository` — the `project` and `source_assessment` artifacts
- `ports.AuditSink` — `source.registered`
- `ports.ProjectNamespaceStore` — every path resolution
- `application.media_binaries.MediaBinaries` — shared lazy ffmpeg/ffprobe resolver
- `application.project_record` — the shared project-record factory
- `local_pipeline.source_strategy` / `render_utils` / `video_analysis`
- `safe_filename`, `new_id` — injected console identity helpers
- `tracker_factory(activity, *, track)` — injected progress factory

### Injected seams

**`progress`** is `ports.ProgressFactory`, implemented by
`infrastructure/progress/local/LocalProgressFactory` and constructed from
`(storage, ownership)` alone.

> **Resolved in Milestone 4.** Milestone 3 injected `tracker_factory` here as a
> `PipelineService` bound method (`_upload_tracker`). That passed every import
> guard while still being a dependency on the façade, so ADR-001 now forbids it
> and `test_no_application_service_takes_a_facade_owned_callable` enforces it.
> The cancel signal moved into the factory, which the job runtime binds via
> `bind_cancel_check`; the façade forwards its own call so cancellation behaves
> identically regardless of which layer created the tracker.

**`safe_filename` / `new_id`** come from `creator_console.ids`. They are pure
functions injected rather than imported so the application layer keeps no
import edge into the console package.

## Composition root

```python
class ConsoleApp:
    namespace_store_cls      = LocalFilesystemStorage
    artifact_repository_cls  = LocalArtifactRepository
    source_service_cls       = SourceService       # new
    editor_service_cls       = EditorService
    ...
    self.media_binaries = MediaBinaries(config.ffmpeg_bin, config.ffprobe_bin)
```

`service(ownership)` passes `binaries=self.media_binaries`, `source_cls` and
`editor_cls` into `PipelineService`, which constructs the source service and
then hands it to the editor as its `SourceQuery`. One binary resolver, one
audit sink and one artifact repository per project. A bare
`PipelineService(storage, ownership, secrets)` still builds every default
itself, so notebooks and tests are unaffected.

## Fault-injection note

`creator_console/faults.py` patches `atomic_write_json` on a **manual list** of
modules. `SourceService` writes media bytes with raw `open`/`os.replace` (as the
façade always did) and persists its descriptor through
`LocalArtifactRepository`, which is already a registered holder — so **no new
registration was required** and ENOSPC / permission-denied / partial-write
faults still reach the source write path. This is proven by
`tests/test_source_facade.py::TestSourceWriteFaultCoverage` rather than assumed.
