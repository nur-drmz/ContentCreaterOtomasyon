# Persistence Ports

> Introduced in Milestone 1; corrected in Milestone 2. These ports describe the
> operations the application **actually performs today**, derived from the
> current local implementation. They are not projections of a future database
> schema.
>
> Milestone 2 correction: `ProjectRepository` was renamed
> **`ProjectNamespaceStore`** — its methods are namespace/path-safety
> operations, not project-metadata persistence, and the old name falsely
> implied the future PostgreSQL `projects` table. `ArtifactRepository` is now
> bound to a real adapter (`LocalArtifactRepository`) instead of being
> satisfied incidentally by `PipelineService`.

## Why protocols, not base classes

The ports live in `application/ports/` and are declared with `typing.Protocol`
(`@runtime_checkable`). The existing local implementations already satisfy them
**structurally**, so no inheritance edge was added and no call site changed. That
is what makes this milestone behaviour-free.

Conformance is asserted in `tests/test_persistence_ports.py`:

```python
assert isinstance(storage, ProjectNamespaceStore)
assert isinstance(JobStore(storage, ownership), JobRepository)
assert isinstance(AuditLog(storage, ownership), AuditSink)
assert isinstance(LocalObjectStore(storage), ObjectStore)
assert isinstance(LocalArtifactRepository(storage, ownership, audit), ArtifactRepository)
assert JobRepository not in JobStore.__mro__      # no inheritance was forced
```

## Why not a generic repository

`Repository.get()/save()/delete()` for every entity was explicitly rejected. It
would discard the semantics that are the entire value of this codebase:

| Would be lost | Port that keeps it |
|---|---|
| idempotent submission returns the existing job | `JobRepository.submit` |
| single claim / compare-and-set | `JobRepository.claim` |
| lease ownership and expiry | `JobRepository.claim` / `heartbeat` |
| recovery only for genuinely dead owners | `JobRepository.recover_abandoned` |
| cooperative cancellation | `JobRepository.request_cancel` |
| closed audit vocabulary | `AuditSink.record` |
| append-only trail survives invalidation | `AuditSink` |
| namespace isolation, loud path rejection | `ProjectNamespaceStore.resolve` |
| honest artifact reads (`problem`, never a default) | `ArtifactRepository.load_artifact` |
| bytes vs structured state separation | `ObjectStore` vs the repositories |

### `ProgressFactory` / `ProgressTracker` — `application/ports/progress.py`

> Added in Milestone 4. An **application** contract, not persistence.

| Method | Guarantee |
|---|---|
| `ProgressFactory.create(activity, *, job_id, attempt, stages, track)` | returns a tracker; `track=False` returns a NO-OP tracker, never `None` |
| `ProgressTracker.{start,stage,count,note,finish,fail}` | writing progress must never raise into the pipeline — losing a progress record must not lose a render (`note` added in Milestone 12 for the transcript carry-over/archive messages) |

Cancellation: `stage()`/`finish()` call `raise_if_cancelled()`, which stops the
record and raises `OperationCancelled` at a safe boundary. The cancel signal
belongs to the job runtime and is bound into the factory by the composition
root — an application service never holds it.

**Implementation:** `infrastructure/progress/local/LocalProgressFactory`,
constructed from `(storage, ownership)` and wrapping the existing
`creator_console.progress.ProgressTracker` / `NullTracker`. This port exists to
remove the façade-owned tracker callable that Milestone 3 injected into
`SourceService`; see `ANALYSIS_SERVICE.md` and `SOURCE_SERVICE.md`.
Milestone 12 added `TranscriptService` (`run_transcript` →
`create("transcript")`) as a consumer.

### `SourceQuery` — `application/ports/source_query.py`

> Added in Milestone 3; extended in Milestone 5 (`get_fingerprint`). Not a
> persistence port: a read-only **application** contract, listed here because
> it is declared alongside the others.

| Method | Guarantee |
|---|---|
| `get_source_path()` | an existing LOCAL path (so ffmpeg can read it), or a raise — never a plausible missing path |
| `get_duration()` | measured seconds, or `None` when genuinely unknown; `None` is never replaced by a default |
| `get_fingerprint()` | the digest source-rights answers bind to, or `None` when nothing is attached; must change on source swap |

**Implementation:** `application.services.SourceService` (structurally).
**Consumers:** `EditorService`, `AnalysisService`, `VideoAnalysisService`
(Milestone 10 — `get_source_path` is the only media edge analysis needs),
`MomentService` (Milestone 11 — `get_source_path` for the bounds-validation
`EDLClip`, `get_duration` as the upper bound).

Why a port rather than handing `SourceService` to `EditorService`: the editor
needs two facts, while the source service can also attach and replace sources.
A read contract denies consumers authority they must never use, and makes a
test double trivial. See `SOURCE_SERVICE.md`.

### `ReviewGate` — `application/ports/review_gate.py`

> Added in Milestone 5. An **application** contract: the read-only view of the
> three human review checklists that `ApprovalService` consults as
> preconditions before the hash gate runs.

| Method | Guarantee |
|---|---|
| `copyright_review_state()` | source-rights checklist, bound to the source fingerprint |
| `render_review_state(slot)` | per-slot render checklist, bound to the video digest |
| `voice_review_state()` | voiceover checklist, bound to the audio digest |

Semantics: `complete` is True only when every required item was ticked against
the **current** content digest; `open_revision` blocks even a complete list;
the gate may never manufacture a satisfied precondition.

**Implementation:**
`infrastructure/review/local/LocalReviewGate`, wrapping the canonical console
checklist modules (`copyright_review`, `render_review`, `voice_review`) — the
read/orchestration side moved there so approval and the UI share one answer.
Checklist **mutation** is not in this port; it stays with the façade methods
(which reuse the gate's `load_or_new_*` helpers).

### `ApprovalInvalidator` — `application/ports/approval_gate.py`

> Added in Milestone 5. The narrow capability `AnalysisService` needs when a
> plan edit drops a live approval — services may not import siblings
> (ADR-001), so the dependency is expressed as a port.

| Method | Guarantee |
|---|---|
| `record_invalidation(kind, *, artifact, actor, reason, previous_approval, slot)` | emits `approval.invalidated` iff the prior block was approved; **writes no artifact** (the caller already persisted the edit); never deletes the original grant |

**Implementation:** `application.services.ApprovalService` (structurally).
The approval *reset* itself is a pure artifact transformation inside
`build_processing_plan`, persisted by the one write the edit already performs —
so the port exposes only the record, nothing more.

### `ApprovalQuery` — `application/ports/approval_gate.py`

> Added in Milestone 6. The **read-only** half of `ApprovalService` that
> publication preconditions need — siblings may not be imported (ADR-001),
> and `PipelineService.approval_chain` may not be injected as a bound method,
> so the capability is expressed as a port.

| Method | Guarantee |
|---|---|
| `state(kind, slot=1)` | live approval validity for ONE kind, re-derived from current content (`present`/`approved`/`valid`/`reason` + review fields) — added in Milestone 12 |
| `chain(slot=1)` | the dependency-aware validity map (`effective_valid`, `reason`, …) for the content-approval chain at a slot |

**Implementation:** `application.services.ApprovalService` (structurally).
**Consumers:** `PublishService.create_intent` — it refuses an intent while
`chain()["render"].effective_valid` is false — and `TranscriptService`
(Milestone 12), whose `transcript_view` reports `state("transcript", slot)`
to the caption screen. The port exposes no granting or invalidation:
publication and review must read content approval, never write it.

### `WorkflowGateQuery` — `application/ports/workflow_gate.py`

> Added in Milestone 9. An **application** contract: the read-only
> eligibility and slot-scope view of the processing plan that future
> production services need — asking it through `PipelineService` would import
> the whole façade.

| Method | Guarantee |
|---|---|
| `plan_gate(phase)` | `(allowed, reason)` — reports, never throws for an ordinary no; `ValueError` on unknown phase |
| `require_phase(phase)` | raises `PipelineServiceError` with the gate's reason |
| `plan_decision()` | the plan's `decision` block, `{}` when no plan |
| `slot_count()` / `slot_indexes()` | plan-defined output slots (1 when no plan) |
| `require_slot(slot)` | validated slot index or `PipelineServiceError` |
| `slot_moment_rank(slot)` | the plan entry's pre-assigned rank, or `None` |

Semantics: read-only — a gate query must never write, audit, grant approval or
advance state; `plan_gate` reports while `require_phase` enforces; denial
reasons are the operator-facing strings, not error codes.

**Implementation:** `application.services.WorkflowGate` (structurally).
**Consumers:** the `PipelineService` slot/gate delegates,
`VideoAnalysisService` (Milestone 10) for `require_phase("video_analysis")`,
and `MomentService` (Milestone 11) for `require_phase("moment_selection")`,
`require_slot`, `slot_indexes` and `slot_moment_rank`, and
`TranscriptService` (Milestone 12) for `require_phase("transcript")`,
`require_slot` and `plan_gate`. The remaining
production services (`WORKFLOW_GATE.md` → readiness) follow the same
injection pattern.

### `RevisionArchiver` — `application/ports/revision_archiver.py`

> Added in Milestone 11. An **infrastructure** contract: recoverable
> `.revisions/` snapshots — "move these namespace-relative paths into a
> restorable revision" — shared by the façade (rewind, render-profile edits,
> review flows) and `MomentService` (clip-bounds invalidation). Extracted as
> a port because a bound `PipelineService._archive_revision` would be a
> façade-owned callable (ADR-001), while the mechanics are pure namespace
> operations, not moment semantics.

| Method | Guarantee |
|---|---|
| `archive_revision(paths, *, action, actor, target=None)` | MOVE — artifacts land under `.revisions/<id>/` with a `project_revision` manifest; `recoverable: True`; parent paths absorb their children |
| `render_slot_archive_paths(slot=1, *, preserve=None)` | exactly THIS slot's render output — sibling `output_NN/` directories are never included; `RENDER_ARCHIVE_PRESERVED` names the inputs (moment selection, transcript) that survive inside an archived slot directory |

**Implementation:** `infrastructure/persistence/local/LocalRevisionArchive`.
**Consumers:** `MomentService.save_clip_bounds`/`reset_clip_bounds`,
`TranscriptService` (the `action="retranscribe"` snapshot that preserves a
hand-edited transcript a re-run replaces), plus the
façade's `_archive_revision`/`_render_slot_archive_paths` delegates (rewind,
render-profile, review edits) — one implementation for all.

### `ArtifactRepository.write_artifact_path` — extension (Milestone 11)

> Path-level counterpart of `load_artifact_path`: atomically writes a JSON
> document that is NOT a catalog artifact — `results/clip_bounds/
> output_NN.json` is the first consumer. Deliberately emits **no**
> `artifact.written` event: the caller records its own domain event
> (`clip_bounds.saved`), matching the behavior the write had when it called
> `atomic_write_json` directly. Implemented by `LocalArtifactRepository`,
> consumed by `MomentService.save_clip_bounds`, delegated by
> `PipelineService.write_artifact_path` to preserve port conformance.

## The five persistence ports

### `ProjectNamespaceStore` — `application/ports/namespace_store.py`

Namespace resolution and project lifecycle. (Named `ProjectRepository` in
Milestone 1; renamed in Milestone 2 — see the header note.)

| Method | Guarantee |
|---|---|
| `namespace_root(ownership)` | deepest namespace this ownership may touch |
| `ensure_namespace(ownership)` | create if absent |
| `resolve(ownership, *parts, create_parent=False)` | traversal / absolute / drive-letter / UNC / symlink escape **rejected**, not sanitised |
| `relative_to_namespace(ownership, path)` | POSIX relative string; absolute host paths are never exposed |
| `exists(ownership, relative)` | a violation returns `False` rather than raising |
| `list_dir(ownership, relative)` | sorted names; empty list when absent |
| `trash_namespace(ownership)` | recoverable "delete"; returns a recovery token |

Project *metadata* is not duplicated here: it is persisted as the `"project"`
artifact through `ArtifactRepository`. Inventing a second project-metadata
contract would have created two sources of truth.

**Local implementation:** `creator_console.storage.LocalFilesystemStorage`
(aliased as `LocalProjectNamespaceStore`).

### `ArtifactRepository` — `application/ports/artifact_repository.py`

JSON artifact persistence keyed by **logical artifact name**.

| Method | Guarantee |
|---|---|
| `artifact_relative(key, slot)` | `ARTIFACTS` paths and the `results/render/output_NN/` per-slot layout are compatibility surface |
| `load_artifact(key, required, slot)` | reports trustworthiness; corrupt/incompatible yields `data=None` + actionable `problem`, never a default |
| `load_artifact_path(relative)` | raw JSON read by relative path, `None` when absent/unreadable |
| `write_artifact(key, data, ...)` | atomic write, returns relative path, records `artifact.written` |

`ArtifactRecord` is a `TypedDict` mirroring the existing return shape
(`key, slot, present, data, problem, relative_path`) — the shape the API already
returns.

**Local implementation:**
`infrastructure/persistence/local/artifact_repository.py`
(`LocalArtifactRepository`) — a real adapter, introduced in Milestone 2 so
`EditorService` can depend on the port without a
`PipelineService → EditorService → ArtifactRepository → PipelineService`
cycle. It depends only on the namespace store, the audit sink and
`application.artifact_catalog`; it must never import
`creator_console.pipeline_service` (guarded). `PipelineService` keeps its
public artifact methods as delegates to this adapter, so the read/write rules
exist in exactly one place and the façade still satisfies the port
structurally.

### `JobRepository` — `application/ports/job_repository.py`

Durable job state. The docstring enumerates every semantic a PostgreSQL adapter
must reproduce.

| Method | Guarantee |
|---|---|
| `submit(job_type, idempotency_key, ...)` | returns `(record, created)`; a duplicate key schedules **no** work |
| `get(job_id)` | raises rather than returning a plausible empty job |
| `list(statuses, job_type, limit)` | newest first; a corrupt record is **surfaced**, not skipped |
| `claim(job_id, worker_id)` | compare-and-set; second claimant raises `JobConflict` while the lease lives; records owner pid/host |
| `heartbeat(job_id, worker_id)` | extends lease; rejects a non-holder |
| `finish(job_id, status, ...)` | terminal only; `error` is scrubbed before persisting |
| `request_cancel(job_id, ...)` | queued → cancelled immediately; running stays `running` with `cancel_requested_at` |
| `cancel_requested(job_id)` | cheap checkpoint; `False` for unknown |
| `request_retry(job_id, ...)` | terminal non-successful only, within the attempt budget |
| `recover_abandoned(...)` | only expired lease **or** provably dead owner on this host |

`status` (execution) and `artifact_status` (what the pipeline wrote) remain
separate fields.

**Local implementation:** `creator_console.jobs.JobStore`
(aliased as `LocalJobRepository`).

### `AuditSink` — `application/ports/audit_sink.py`

Append-only audit trail. Deliberately **not** a generic logging abstraction —
audit, domain events and telemetry are three different concepts (ADR-012).

| Method | Guarantee |
|---|---|
| `record(event_type, actor, detail, job_id, artifact)` | event outside the closed vocabulary **raises**; `detail` scrubbed |
| `events(limit, job_id)` | newest first; a truncated trailing line must not hide the rest |

**Local implementation:** `creator_console.audit.AuditLog`
(aliased as `LocalAuditSink`).

### `ObjectStore` — `application/ports/object_store.py`

Binary object storage, separate from the repositories.

| Method | Guarantee |
|---|---|
| `put_bytes(ownership, key, payload, ...)` | atomic; returns `ObjectStat` |
| `put_file(ownership, key, source_path, ...)` | publishes a locally produced render back into the store |
| `open(ownership, key)` | context manager; **raises** when absent |
| `stat(ownership, key)` | `exists=False` when absent instead of raising; size + SHA-256 |
| `exists` / `delete` | isolation-safe; a violating key is `False`, not a crash |
| `materialize(ownership, key)` | context manager yielding a **local path** for ffmpeg |

See ADR-004 for the materialize lifecycle diagram. The local adapter's
`materialize()` yields the stored path and cleans up nothing; a future cloud
adapter downloads to a temp copy and deletes it on exit. `local_pipeline` is never
required to understand cloud storage.

**Local implementation:** `infrastructure/storage/local/object_store.py`
(`LocalObjectStore`). It delegates
path safety to `LocalFilesystemStorage` rather than reimplementing the traversal
rules, so there is no second drifting copy of the security logic.

## Composition root and the injection seam

`creator_console.app.ConsoleApp` is the single place that chooses adapters:

```python
class ConsoleApp:
    namespace_store_cls     = LocalFilesystemStorage   # ports.ProjectNamespaceStore
    artifact_repository_cls = LocalArtifactRepository  # ports.ArtifactRepository
    job_repository_cls      = JobStore                 # ports.JobRepository
    audit_sink_cls          = AuditLog                 # ports.AuditSink
    object_store_cls        = LocalObjectStore         # ports.ObjectStore
    editor_service_cls      = EditorService            # application.services
```

Used by `__init__` (storage, object_store), `job_store(ownership)`, the
`audit_sink(ownership)` accessor, and `service(ownership)` — which builds one
audit sink and one artifact repository per project and injects them into
`PipelineService` together with `editor_cls`. Defaults are the current local
classes, so every existing construction pattern keeps working; a bare
`PipelineService(storage, ownership, secrets)` still builds the local adapters
itself for tests and notebooks.

This was chosen over adding constructor parameters to many classes: the
construction graph audit showed `ConsoleApp` already builds storage, job stores
and services, so it is the smallest seam that allows a later backend swap.

### Transitional import direction (stated honestly)

`infrastructure/` currently imports from `creator_console/`:

- `infrastructure/persistence/local/` aliases `LocalFilesystemStorage`, `JobStore`, `AuditLog`.
- `infrastructure/storage/local/object_store.py` imports `creator_console.storage`
  for `atomic_write_bytes` and `StorageViolation`.

That is the reverse of the eventual arrow, and it is deliberate: those three
classes **are** the infrastructure today, and physically moving them would be a
large, risky diff that changes import paths for every `verify_*.py` script. The
architecture guard therefore forbids `infrastructure` from importing `services` or
`apps` (real layering violations) while allowing `creator_console` during the
transition. When the classes move, the alias module is the only thing that changes.

### Known remaining direct constructions

`AuditLog(...)` is still constructed directly in `creator_console/api.py`,
`creator_console/automation.py` and `creator_console/pipeline_service.py`. These
were left alone on purpose: routing them through the seam is churn with no benefit
until a second backend exists, and each edit is a behavioural risk today. Inside
`creator_console/app.py` the six existing constructions were switched to
`self.audit_sink(ownership)`, which is the same class with the same arguments.

## What these milestones deliberately did not do

- No PostgreSQL, SQLAlchemy, Alembic, connection config, tables or migrations.
- No boto3/MinIO/signed URLs/multipart upload.
- No dual-write (see the corrected ADR-003).
- No Turbo/Long-form changes.
- No identity/authorization changes.
- `resolve_edl_revision` was not exposed (ADR-013 stands).
- `PipelineService` decomposition is incremental: Milestone 2 extracted the
  EDL/editor use cases (`EditorService`), Milestone 3 the source media
  (`SourceService`), Milestone 4 Phase 0 assessment + processing plan
  (`AnalysisService`) and Milestone 5 the content-approval lifecycle
  (`ApprovalService` + `ports.ReviewGate`/`ApprovalInvalidator`). Every other
  façade method is untouched — project lifecycle, video analysis,
  transcript/script/TTS, render, publish and phase gates in particular.
- No `AssessmentRepository` port was invented: assessment and plan state are
  ordinary artifacts, so `AnalysisService` uses `ArtifactRepository`.
- No `SourceRepository` port was invented. Source state is entirely represented
  by the `project` artifact's `source` block plus bytes in the namespace, so
  `SourceService` depends on the existing `ArtifactRepository` and
  `ProjectNamespaceStore`. A DB-shaped source repository with no distinct
  entity behind it would have been premature.
