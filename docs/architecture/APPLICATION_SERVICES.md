# Application Services

> Introduced in Milestone 2; extended in Milestones 3, 4, 5 and 6. This
> document defines what an application service is in this repository, how the
> existing services are wired, and the pattern every future extraction must
> follow.
>
> Services so far: **`EditorService`** (EDL/editor), **`SourceService`**
> (source media — `SOURCE_SERVICE.md`), **`AnalysisService`** (Phase 0
> assessment + processing plan — `ANALYSIS_SERVICE.md`),
> **`ApprovalService`** (content-approval lifecycle — `APPROVAL_SERVICE.md`)
> and **`PublishService`** (publication orchestration — `PUBLISH_SERVICE.md`).

## Layer responsibilities

| Layer | Owns | Must not own |
|---|---|---|
| Transport (`services/api/`, `creator_console/api.py`) | HTTP parsing, status codes, request→call mapping | pipeline decisions, `local_pipeline` imports |
| Façade (`creator_console/pipeline_service.py`) | backward-compatible public method surface | new orchestration — it delegates |
| **Application services** (`application/services/`) | use-case orchestration: load → domain op → persist → audit | HTTP, provider SDKs, job leasing, render execution |
| Ports (`application/ports/`) | persistence/storage contracts | implementations, `local_pipeline` types |
| Infrastructure (`infrastructure/`) | local adapters satisfying the ports | transport, the façade |
| `local_pipeline/` | deterministic media/domain logic | persistence policy, audit, ownership |

An application service is **ownership-scoped**: one instance per project per
request, built at the composition root — matching the existing
`PipelineService` lifetime.

## EditorService — the first extraction

`application/services/editor_service.py` owns the EDL editor use cases:

| Method | What it orchestrates |
|---|---|
| `create_edl` | default source resolution → `scene_editor.build_edl` → artifact write → `edl.created` |
| `load_edl` | artifact read + trust check → `ArtifactProblem` on corruption → `edl_from_artifact` |
| `update_clip` | index guard → bounds/crop/zoom/caption/B-roll ops → write → `edl.updated` |
| `add_clip` | append via `add_clip_to_edl` → write → `edl.clip_added` |
| `set_music` | `set_music_track` → write → `edl.music_set` |
| `build_preview_command` | load + validate → compile ffmpeg argv (no execution) |
| `request_revision` | notes/actor/version bump → write → `edl.revision_requested` |
| `resolve_revision` | clear revision state, version bump → write → `edl.revision_resolved` (internal; no route — ADR-013) |

### Dependencies (all injected)

- `artifacts` — `ports.ArtifactRepository` (`LocalArtifactRepository`)
- `audit` — `ports.AuditSink` (`AuditLog`)
- `namespace` + `ownership` — `ports.ProjectNamespaceStore`, used only to
  resolve the `results/render/` preview output directory
- `source` — `ports.SourceQuery` (`SourceService`): the read-only
  `get_source_path()` / `get_duration()` view of the attached source

> **Milestone 3 change.** Milestone 2 injected two `PipelineService` bound
> methods here (`source_path`, `source_duration`). That was a façade
> back-reference hidden behind lambdas, invisible to the import guards. It is
> now the `SourceQuery` port, and
> `test_editor_service_takes_no_facade_owned_source_callables` fails if any
> such parameter returns.

### What deliberately stayed outside

- **All edit math** — `set_clip_source_bounds`, `crop_clip`, `zoom_clip`,
  `set_caption_style`, `add_b_roll`, `add_clip_to_edl`, serialization and the
  preview filtergraph remain in `local_pipeline.scene_editor`. The service
  orchestrates; the engine calculates.
- **`render_edl_preview`** — stays on the façade. It resolves the ffmpeg binary
  and spawns a subprocess; that is render execution and belongs to a future
  `RenderService`. Only the deterministic *command compilation*
  (`build_preview_command`) moved into `EditorService`.
- **`clip_bounds*` family** — edits the per-slot `clip_bounds` document and
  invalidates the slot's render, not the EDL. Different use case, different
  artifact; it moved with the selection ownership into `MomentService`
  (Milestone 11), not the editor.
- **Typed command objects** — deferred. The update surface is already named,
  optional kwargs parsed by the API layer, not ambiguous dicts; dataclasses
  would add ceremony without information.

## SourceService (Milestone 3)

`application/services/source_service.py` owns the source media attached to a
project and is the single writer of the project artifact's `source` block.
Full detail — metadata authority, materialization, the rights boundary and the
duration fallback (repaired in Milestone 8) — is in **`SOURCE_SERVICE.md`**.
Summary:

- **Methods:** `get_descriptor`, `get_source_path`, `get_duration`,
  `get_fingerprint`, `store_upload`, `store_upload_stream`,
  `attach_downloaded_source`.
- **Satisfies** `ports.SourceQuery` structurally — the contract the editor uses.
- **Stayed outside:** HTTP streaming (transport), source assessment (façade,
  pending an Analysis service), trend research + rights gate (façade),
  source-library root policy (`ConsoleApp`), path rules (namespace port),
  probe/fingerprint algorithms (`local_pipeline`).

## AnalysisService (Milestone 4)

`application/services/analysis_service.py` owns Phase 0 and is the only writer
of `source_assessment` and `processing_plan`. Full detail in
**`ANALYSIS_SERVICE.md`**. Summary:

- **Methods:** `assess_source`, `get_assessment`, `create_processing_plan`,
  `update_processing_plan`, `get_processing_plan`, `describe_plan_review`.
- **Stayed outside:** deterministic scoring/plan construction
  (`local_pipeline.source_strategy`), video analysis and everything downstream,
  approval *granting* and the copyright gate, phase gates, job mechanics.
- **No provider call at all** — only the boolean
  `secrets.reference("google_api").configured`.
- **Approval invalidation goes through a port** (Milestone 5): a plan edit
  drops a live approval, and the *recording* of that is approval semantics —
  so `AnalysisService` depends on `ports.ApprovalInvalidator` (one method,
  `record_invalidation`), satisfied by `ApprovalService`. No sibling import.

## ApprovalService (Milestone 5)

`application/services/approval_service.py` owns the content-approval
lifecycle: hash-bound granting, the rights/checklist preconditions consulted
before a grant, live state, dependency-aware chain validity, the stale-approval
sweep, and recording invalidations that other services' edits imply. Full
detail — invariants, the dependency matrix, the publication boundary and the
future reviewer-policy seam — is in **`APPROVAL_SERVICE.md`**. Summary:

- **Methods:** `approve`, `state`, `chain`, `invalidated`,
  `record_invalidation`.
- **Dependencies:** `ArtifactRepository`, `AuditSink`, `ports.ReviewGate`
  (`LocalReviewGate`, which wraps the console checklist modules),
  `application.{approval_catalog,slot_policy}`, and
  `local_pipeline.{approval,video_analysis,render_utils,source_strategy}` —
  the digest engine stays canonical in `local_pipeline.approval`.
- **Stayed outside:** digest math, checklist *mutation*, publication
  approvals (`approve_publication_*`/`approve_publish_*` — separate state
  machines on the façade, pending a PublishService), phase gates, render
  execution.
- **Two new ports:** `ports.ReviewGate` (read-only view of the three human
  checklists approval consults) and `ports.ApprovalInvalidator` (the narrow
  capability `AnalysisService` needs when an edit drops a live approval).

## PublishService (Milestone 6)

`application/services/publish_service.py` owns the publication use cases:
metadata generation/manual writing/selection/approval, the lazy YouTube
provider and its credential/OAuth surface, publish-intent creation and
approval, the one authorized **private** upload per intent, publish-manifest
persistence, visibility approval and application, and the publish-state view
model. Full detail — state machine, digest binding, private-first invariant,
crash-recovery window — is in **`PUBLISH_SERVICE.md`**. Summary:

- **Methods:** `generate_metadata`, `write_manual_metadata`,
  `select_metadata`, `approve_metadata`, `publish_state`, `create_intent`,
  `approve_intent`, `run_upload`, `approve_visibility`, `apply_visibility`,
  `credential_state`, `authorization_url`, `complete_authorization`,
  `provider`.
- **Dependencies:** `ArtifactRepository`, `AuditSink`,
  `ProjectNamespaceStore`, `ProgressFactory`, `ports.ApprovalQuery`
  (implemented by `ApprovalService` — read-only chain consult inside
  `create_intent`), `secrets` (capability check only), an injected
  `local_pipeline.publish.PublishProvider` (lazy real YouTube provider
  otherwise), and an optional `google_client_factory`.
- **Stayed outside:** all publication policy (`local_pipeline.publish`:
  private-first, digests, preconditions), all YouTube wire protocol
  (`local_pipeline.publish_youtube`: OAuth, resumable upload, redaction),
  content approval (`ApprovalService`), and job leasing (`creator_console.jobs`).
- **One new port:** `ports.ApprovalQuery` — the read-only half of
  `ApprovalService` that publication preconditions need (`chain()`). The
  façade never injects `approval_chain` as a bound method.

## WorkflowGate (Milestone 9)

`application/services/workflow_gate.py` owns the plan-derived workflow
eligibility and slot scope — everything a production phase asks before it
runs. Full detail — eligibility matrix, mode policy, error semantics, the
future-service readiness assessment — is in **`WORKFLOW_GATE.md`**. Summary:

- **Methods:** `plan_gate`, `require_phase`, `plan_decision`, `slot_count`,
  `slot_indexes`, `require_slot`, `slot_moment_rank` — all read-only.
- **Dependencies:** `ports.ArtifactRepository` only. The eligibility policy
  itself stays canonical in `local_pipeline.source_strategy`; slot arithmetic
  stays in `application.slot_policy`. The gate binds both to the stored plan.
- **Satisfies** `ports.WorkflowGateQuery` structurally — the contract future
  production services (VideoAnalysis, Moment, Transcript, Script, Voice,
  Render) will receive instead of `PipelineService`.
- **Stayed outside:** approval lifecycle (`ApprovalService`), rights
  checklists (`ports.ReviewGate`), per-phase artifact prerequisites (each
  `run_*` runner), job semantics, publishing.

## VideoAnalysisService (Milestone 10)

`application/services/video_analysis_service.py` owns Phase 1 video-content
analysis and is the application-level producer of `hot_moments`. Full detail
— pipeline stages, long-source scanning, artifact contract, invalidation —
is in **`VIDEO_ANALYSIS_SERVICE.md`**. Summary:

- **Method:** `run_analysis(...)` — same parameter surface as the façade's
  `run_video_analysis`, which remains a thin delegate so job dispatch,
  notebooks and verify scripts are untouched.
- **Dependencies:** `ports.ArtifactRepository`, `ports.ProjectNamespaceStore`,
  `ports.SourceQuery` (the attached source path), `MediaBinaries`,
  `ports.ProgressFactory`, `ports.WorkflowGateQuery` — the first production
  consumer of the Milestone 9 gate port. No audit sink (the repository emits
  `artifact.written`), no secrets resolver (the analysis is fully local —
  FFmpeg, faster-whisper, PySceneDetect, audio-energy math).
- **`hot_moments` boundary:** this service is the only producer; all readers
  keep consuming through `load_artifact` — no speculative query port was
  added. `FULL_SOURCE_ANALYSIS_STAGES` moved with it (re-exported from the
  façade for compatibility).
- **Stayed outside:** moment selection / `moment_selection` (extracted in
  Milestone 11 — see below), transcript (extracted in Milestone 12 — see
  below), script/TTS/render, source assessment
  (`AnalysisService` — read-only dependency via the artifact), job
  scheduling and approvals.

## MomentService (Milestone 11)

`application/services/moment_service.py` owns Phase 2 — which analysed moment
becomes which Short — and is the **only application writer of
`moment_selection`** plus the owner of the per-slot `clip_bounds` documents
(`results/clip_bounds/output_NN.json`). Full detail — rank ownership, digest
binding, slot isolation, invalidation — is in **`MOMENT_SERVICE.md`**.
Summary:

- **Methods:** `record_moment_selection` (the single write path),
  `moment_rank_owner`, `selected_moment`, `clip_bounds_relative`,
  `clip_bounds`, `clip_bounds_override`, `clip_bounds_view`,
  `save_clip_bounds`, `reset_clip_bounds`, `source_duration_sec` — all
  delegated verbatim from the façade, signatures and error strings unchanged.
- **Dependencies:** `ports.ArtifactRepository` (extended with
  `write_artifact_path` for the non-catalog clip_bounds document),
  `ports.SourceQuery`, `ports.WorkflowGateQuery` (`require_phase`,
  `require_slot`, `slot_indexes`, `slot_moment_rank`), `ports.AuditSink`
  (`clip_bounds.saved`), and the new **`ports.RevisionArchiver`** —
  implemented by `LocalRevisionArchive`, which now also hosts the
  `.revisions/` mechanics the façade's rewind/render-profile paths delegate
  to. One implementation, no façade-owned callable, no cycle.
- **Boundary:** `VideoAnalysisService` remains the sole `hot_moments`
  producer; MomentService reads it and calls the canonical
  `local_pipeline.video_analysis` builders/validators — never reimplements
  them. A selection grants no approval and starts no downstream job.
- **Stayed outside:** transcript/script/TTS/render, publishing, approvals
  (`ApprovalService`), job scheduling — and the revision-snapshot code itself
  (shared infrastructure, not moment semantics).

## TranscriptService (Milestone 12)

`application/services/transcript_service.py` owns the `moment_transcript`
lifecycle — the **only application writer** across all five write paths.
Full detail — generation/edit pipelines, revision archive, clip-bounds
invalidation contract — is in **`TRANSCRIPT_SERVICE.md`**. Summary:

- **Methods:** `run_transcript` (the only generation path: gate →
  hot_moments/selection trust check → canonical
  `render_utils.run_moment_transcription` → correction carry-over →
  replaced-edit archive → write → progress finish/fail),
  `transcript_view`, `transcript_segments`, `save_transcript_edits`,
  `set_transcript_exclusions`, `restore_transcript_from_asr`,
  `set_transcript_corrections` — all delegated verbatim from the façade.
- **Dependencies:** `ports.ArtifactRepository`, `ports.SourceQuery`,
  `ports.WorkflowGateQuery`, `ports.ApprovalQuery` (**extended with `state`**
  for the view's live approval read), `ports.RevisionArchiver`,
  `ports.ProgressFactory` (**`ProgressTracker` extended with `note`**),
  `ports.ProjectNamespaceStore` + `ownership` (the `.runtime/` scratch dir
  and the empty-glossary stamp), `ports.AuditSink`.
- **Boundary:** `MomentService` still owns `moment_selection`;
  `VideoAnalysisService` still owns `hot_moments`; the ASR engine and
  transcript-edit math stay in `local_pipeline`. An edit drops approval by
  changing the bound digest — never by writing approval state — and never
  triggers script/TTS/render/publish.
- **Stayed outside:** the glossary management API (`glossary_view`,
  `set_glossary_terms`, `accept_glossary_suggestion` remain façade methods —
  the service only reads the artifact and appends suggestion proposals) and
  script/TTS/render/publish.

## The façade-owned-callable rule (Milestone 4)

Milestone 3 shipped `SourceService(tracker_factory=self._upload_tracker)` — a
`PipelineService` bound method. It passed every import guard while still being a
dependency on the façade. ADR-001 now forbids it outright, and two guards
enforce it because import analysis cannot see a callable argument:

- `test_no_application_service_takes_a_facade_owned_callable` — scans every
  class in `application.services.__all__` for `tracker_factory`, `facade`,
  `pipeline_service`, `source_path`, `source_duration` parameters.
- `test_application_services_do_not_depend_on_each_other_concretely` — services
  share a *port*, never import a sibling.

Injected *infrastructure* values stay acceptable: `safe_filename`/`new_id`
(pure functions), `probe_fn` (test seam), `secrets` and `MediaBinaries` (both
independently constructed at the composition root).

## Shared policy modules

Neutral `application/` modules exist so the façade, the services and the
adapters can share one implementation without an import edge between them:

| Module | Holds | Consumers |
|---|---|---|
| `artifact_catalog.py` | `ARTIFACTS`, `PER_SLOT_ARTIFACTS`, `REQUIRED_FIELDS`, `SUPPORTED_SCHEMA_VERSIONS`, `PRODUCTION_READY_ALLOWED`, `RESULTS_DIRNAME`, `SOURCE_DIRNAME`, `RUNTIME_DIRNAME`, `artifact_relative()` | façade (re-export, so `pipeline_service.ARTIFACTS` still resolves), `LocalArtifactRepository`, `LocalRevisionArchive`, `SourceService`, `MomentService`, `TranscriptService` (`RUNTIME_DIRNAME`), `PublishService` (OAuth token path) |
| `approval_catalog.py` | `APPROVAL_ARTIFACTS`, `CHECKLIST_GATED_KINDS`, `RIGHTS_GATED_KIND` — which artifacts the content-approval gate covers | façade (re-export as `APPROVAL_ARTIFACTS`), `ApprovalService` |
| `slot_policy.py` | `slot_count`/`slot_indexes`/`require_slot` as pure functions over a loaded plan | `WorkflowGate` (façade slot delegates), `ApprovalService` (per-slot approvals, stale sweep) |
| `errors.py` | `PipelineServiceError`, `ArtifactProblem` — the API maps these exact types to error codes | façade (re-export), all services |
| `project_record.py` | `new_project_record()`, `create_project()` | façade `create_project`, `SourceService` bootstrap |
| `media_binaries.py` | `MediaBinaries` — lazy, cached ffmpeg/ffprobe resolution | façade `binaries()`, `SourceService` |
| `time_utils.py` | `now_iso()` | `project_record`, `SourceService`, `MomentService`, `LocalRevisionArchive` |

`project_record.py` is **not** a `ProjectService`. Attaching a source to a
project with no `project.json` must bootstrap one, which both attach paths have
always done via `create_project`. Isolating that single factory kept the record
shape and the `project.created` event in one place instead of turning this
milestone into a project-lifecycle extraction. Rename/reset/rewind/delete/
archive remain on the façade.

## Composition root

`ConsoleApp` chooses the adapter classes; `PipelineService` wires them:

```python
class ConsoleApp:
    namespace_store_cls      = LocalFilesystemStorage  # ProjectNamespaceStore
    artifact_repository_cls  = LocalArtifactRepository # ArtifactRepository
    job_repository_cls       = JobStore                # JobRepository
    audit_sink_cls           = AuditLog                # AuditSink
    object_store_cls         = LocalObjectStore        # ObjectStore
    progress_factory_cls     = LocalProgressFactory    # ProgressFactory
    source_service_cls       = SourceService           # application.services
    editor_service_cls       = EditorService           # application.services
    analysis_service_cls     = AnalysisService         # application.services
    approval_service_cls     = ApprovalService         # application.services
    publish_service_cls      = PublishService          # application.services
    video_analysis_service_cls = VideoAnalysisService  # application.services
    moment_service_cls       = MomentService           # application.services
    transcript_service_cls   = TranscriptService       # application.services
    revision_archive_cls     = LocalRevisionArchive    # ports.RevisionArchiver
    review_gate_cls          = LocalReviewGate         # ports.ReviewGate

    self.media_binaries = MediaBinaries(config.ffmpeg_bin, config.ffprobe_bin)
```

`service(ownership)` builds one audit sink, one artifact repository and one
progress factory per project, then passes them into
`PipelineService(..., audit=, artifacts=, binaries=, progress=, source_cls=,
editor_cls=, analysis_cls=, approval_cls=, publish_cls=, review_gate_cls=,
workflow_cls=, video_analysis_cls=, moment_cls=, transcript_cls=,
revision_archive=)`.
`PipelineService` constructs the workflow gate first (it needs only the
artifact repository), then the source service, the review gate and the
approval service (the gate needs `SourceQuery` for the source fingerprint the
copyright checklist binds to), then hands the approval service to the analysis
service **as its `ApprovalInvalidator` port** — the narrow capability a plan
edit needs, not a sibling import — to the publish service **as its
`ApprovalQuery` port** (the read-only `chain()` the intent gate consults), and
the source service to the editor as its `SourceQuery`. It forwards `bind_cancel_check` into the progress factory so
cancellation does not depend on which layer built the tracker. Constructing
`PipelineService(storage, ownership, secrets)` directly — as tests and
notebooks do — still works: it builds every local default itself, and each
service is independently constructible without a façade (demonstrated by the
`_direct_*_service` helpers in the test suite).

## Dependency direction (enforced by `tests/test_architecture_guards.py`)

```
ConsoleApi
    │
    ▼
PipelineService ───┬───► SourceService ──┬─► ports.ArtifactRepository ─► LocalArtifactRepository
    │              │                     ├─► ports.ProjectNamespaceStore ─► LocalFilesystemStorage
    │              │                     ├─► ports.ProgressFactory ─► LocalProgressFactory
    │              │                     ├─► MediaBinaries
    │              │                     └─► local_pipeline.{source_strategy,render_utils}
    │              │
    │              ├───► EditorService ──┬─► ports.SourceQuery  (= SourceService)
    │              │                     ├─► ports.ArtifactRepository
    │              │                     └─► local_pipeline.scene_editor
    │              │
    │              ├───► AnalysisService ┬─► ports.SourceQuery  (= SourceService)
    │              │                     ├─► ports.ArtifactRepository / AuditSink
    │              │                     ├─► ports.ProgressFactory
    │              │                     ├─► ports.ApprovalInvalidator (= ApprovalService)
    │              │                     ├─► MediaBinaries + probe_fn + secrets
    │              │                     └─► local_pipeline.{source_strategy,approval}
    │              │
    │              ├───► ApprovalService ┬─► ports.ArtifactRepository / AuditSink
    │              │                     ├─► ports.ReviewGate ─► LocalReviewGate
    │              │                     ├─► application.{approval_catalog,slot_policy}
    │              │                     └─► local_pipeline.{approval,video_analysis,
    │              │                                         render_utils,source_strategy}
    │              │
    │              ├───► PublishService ─┬─► ports.ArtifactRepository / AuditSink
    │              │                     ├─► ports.ProjectNamespaceStore
    │              │                     ├─► ports.ProgressFactory
    │              │                     ├─► ports.ApprovalQuery (= ApprovalService)
    │              │                     ├─► local_pipeline.{publish,metadata_studio,
    │              │                     │                  publish_youtube,google_ai}
    │              │                     └─► PublishProvider ─► YouTubeProvider
    │              │
    │              ├───► WorkflowGate ───┬─► ports.ArtifactRepository
    │              │                     ├─► application.slot_policy
    │              │                     └─► local_pipeline.source_strategy
    │              │                         (plan_allows_phase / MODE_PHASE_POLICY)
    │              │
    │              ├───► VideoAnalysisService ┬─► ports.WorkflowGateQuery (= WorkflowGate)
    │              │                          ├─► ports.SourceQuery (= SourceService)
    │              │                          ├─► ports.ArtifactRepository
    │              │                          ├─► ports.ProjectNamespaceStore
    │              │                          ├─► ports.ProgressFactory + MediaBinaries
    │              │                          └─► local_pipeline.video_analysis
    │              │                              (canonical engine — never reimplemented)
    │              │
    │              ├───► MomentService ───────┬─► ports.WorkflowGateQuery (= WorkflowGate)
    │              │                          ├─► ports.SourceQuery (= SourceService)
    │              │                          ├─► ports.ArtifactRepository (+ write_artifact_path)
    │              │                          ├─► ports.AuditSink (= AuditLog)
    │              │                          ├─► ports.RevisionArchiver ─► LocalRevisionArchive
    │              │                          │                            (shared .revisions/ impl;
    │              │                          │                             facade rewind delegates too)
    │              │                          └─► local_pipeline.{video_analysis,scene_editor}
    │              │                              (canonical builders — never reimplemented)
    │              │
    │              └───► TranscriptService ───┬─► ports.WorkflowGateQuery (= WorkflowGate)
    │                                        ├─► ports.SourceQuery (= SourceService)
    │                                        ├─► ports.ArtifactRepository
    │                                        ├─► ports.ApprovalQuery (= ApprovalService; state+chain)
    │                                        ├─► ports.RevisionArchiver ─► LocalRevisionArchive
    │                                        ├─► ports.ProgressFactory ─► LocalProgressFactory
    │                                        ├─► ports.ProjectNamespaceStore ─► LocalFilesystemStorage
    │                                        ├─► ports.AuditSink (= AuditLog)
    │                                        └─► local_pipeline.{render_utils,transcript_edit,
    │                                                            glossary}
    │                                            (canonical ASR + edit math — never reimplemented)
    │
    └─► render execution, script, TTS, trend ingest
        (not yet extracted)
```

No service imports a sibling; `EditorService` and `AnalysisService` both reach
source facts through the `SourceQuery` port, `AnalysisService` reaches
approval through the `ApprovalInvalidator` port, and every phase's "may this
run" question goes through the `WorkflowGateQuery` port that `WorkflowGate`
satisfies. `AnalysisService` writes the assessment that `SourceService` later
reads for duration, which is an artifact-mediated relationship, not a call
cycle. `LocalReviewGate` is the documented transitional `infrastructure →
creator_console` edge: it wraps the console checklist modules
(`copyright_review`, `render_review`, `voice_review`) rather than duplicating
their logic.

- `application/` imports no transport, no `creator_console`, no `infrastructure`.
- `application/ports/` additionally imports no `local_pipeline`.
- `infrastructure/` + `application/` never import `creator_console.pipeline_service`
  or `creator_console.api` — the `PipelineService → service → adapter →
  PipelineService` cycle is structurally impossible.
- `EditorService.__init__` may not regain a `source_path`/`source_duration`/
  `facade`-shaped parameter: a guard asserts the signature, because a lambda
  closing over the façade is invisible to import analysis.

## Migration pattern for future services

1. Map the responsibility in source first; categorize each operation
   (orchestration vs domain vs render execution vs transport).
2. Write characterization tests against the façade *before* moving code.
3. Extract shared policy into a neutral `application/` module if both the
   façade and an adapter need it.
4. Build the real local adapter; make the façade delegate (never duplicate).
5. Add the service to the composition root as a `*_cls` seam.
6. Extend the architecture guards for the new edge.
7. Run the full suite; update this document and the compatibility matrix.
