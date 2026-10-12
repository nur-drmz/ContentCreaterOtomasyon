# Implementation Sequence

> Dependency-aware order. Do not start a milestone before the prior acceptance criteria pass.

## 1. Repository interfaces + local filesystem implementation

**Objective**
Introduce repository and storage interfaces without changing runtime behavior.

**Prerequisites**
- PROMPT 0 and PROMPT 1 complete.
- Architecture docs accepted.

**Exact modules to change/create**
- `infrastructure/repositories.py` — `ProjectRepository`, `ArtifactRepository`, `JobRepository`, `ApprovalRepository`, `AuditRepository` interfaces.
- `infrastructure/storage.py` — `ObjectStorage` interface + `LocalObjectStorage`.
- `creator_console/storage.py` — keep `StorageAdapter`, add `ObjectStorage` bridge.
- `creator_console/jobs.py` — keep `JobStore`, make it use `JobRepository`.
- `creator_console/audit.py` — keep `AuditLog`, make it use `AuditRepository`.

**Compatibility risks**
- `Storage.resolve()` API must stay stable.
- Local project paths must remain identical.

**Tests required**
- `pytest tests/ -q`
- `python verify_clip_bounds.py`
- `python verify_revision_flow.py`
- New repository tests for local filesystem implementation.

**Rollback/fallback**
- If interface changes break `verify_*.py`, keep current implementation and add interfaces as wrappers.

---

## 2. Application service scaffold behind `PipelineService` façade

> **Status (in progress):** `application/services/editor_service.py` done first —
> EDL/editor orchestration extracted, façade methods are thin delegates,
> `LocalArtifactRepository` is the real artifact adapter, and the former
> `ProjectRepository` port was renamed `ProjectNamespaceStore`.
> `application/services/source_service.py` followed (Milestone 3): source
> media, descriptor, duration and fingerprint moved behind it, and the editor's
> façade-owned source callables were replaced by `ports.SourceQuery`.
> `application/services/analysis_service.py` followed (Milestone 4): Phase 0
> assessment and the processing plan moved behind it, and `ports.ProgressFactory`
> retired the last façade-owned callable.
> `application/services/approval_service.py` followed (Milestone 5): the
> content-approval lifecycle (granting, rights/checklist preconditions, state,
> chain validity, invalidation records) moved behind it via
> `ports.ReviewGate` + `ports.ApprovalInvalidator`; `local_pipeline.approval`
> remains the canonical digest engine.
> `application/services/publish_service.py` followed (Milestone 6): publication
> metadata, publish intents, private-first uploads and visibility approval
> moved behind it. Milestone 7 then repaired visibility-job dispatch and added
> the durable `upload_attempt` marker so an uncertain remote outcome cannot be
> re-uploaded automatically (at-least-once, not exactly-once — the
> check-then-write marker is not atomic across processes and remains a release
> blocker before multi-worker publishing).
> Milestone 8 repaired `SourceService.get_duration`'s probe fallback: it now
> calls `probe_media(ffmpeg_bin, ffprobe_bin, path)` with the canonical
> signature instead of the wrong-arity call that always died in a swallowed
> `TypeError`, so assessment-less projects report their real duration and
> editor/clip-bounds validation enforces it.
> `application/services/workflow_gate.py` followed (Milestone 9): plan-derived
> workflow eligibility (`plan_gate`, `require_phase`, `plan_decision`) and
> slot scope moved behind `ports.WorkflowGateQuery`, establishing the boundary
> future production services consume without importing the façade — the
> phases themselves are still on the façade pending their own extractions.
> `application/services/video_analysis_service.py` followed (Milestone 10):
> Phase 1 analysis (`run_analysis`, both scopes) and `hot_moments` production
> moved behind it — the first production consumer of `WorkflowGateQuery`.
> `application/services/moment_service.py` followed (Milestone 11): Phase 2
> moment selection (`record_moment_selection`, rank ownership, read surface)
> and the per-slot `clip_bounds` writes moved behind it — the sole writer of
> `moment_selection` — and the shared `.revisions/` mechanics moved to
> `LocalRevisionArchive` behind `ports.RevisionArchiver`, which the façade's
> rewind/render-profile paths now also delegate to.
> `application/services/transcript_service.py` followed (Milestone 12): the
> `moment_transcript` lifecycle — `run_transcript` (the only generation path),
> `transcript_view`, `transcript_segments` and every edit path
> (`save_transcript_edits`, `set_transcript_exclusions`,
> `set_transcript_corrections`, `restore_transcript_from_asr`) plus the
> `retranscribe` replacement archive — moved behind it, the sole writer of
> `moment_transcript`. `ports.ApprovalQuery` gained the read-only `state`
> and `ports.ProgressTracker` gained `note` for it.
> See `APPLICATION_SERVICES.md`, `WORKFLOW_GATE.md`, `VIDEO_ANALYSIS_SERVICE.md`,
> `MOMENT_SERVICE.md`, `TRANSCRIPT_SERVICE.md`, `SOURCE_SERVICE.md`,
> `ANALYSIS_SERVICE.md`, `APPROVAL_SERVICE.md` and `PUBLISH_SERVICE.md`. The
> remaining services below are still future work.

**Objective**
Move `PipelineService` responsibilities into `application/` services, one-by-one, without breaking tests.

**Prerequisites**
- Repository interfaces from milestone 1.

**Exact modules to change/create**
- `application/project_service.py`
- `application/services/source_service.py` — **done**
- `application/services/editor_service.py` (EDL) — **done**
- `application/services/analysis_service.py` (Phase 0) — **done**
- `application/services/approval_service.py` (content approval) — **done**
- `application/services/publish_service.py` — **done** (M6); publishing
  reliability fixes — `job_id` on the visibility job path and the
  fail-closed `upload_attempt` marker — **done** (M7)
- `creator_console/pipeline_service.py` — each method becomes a 1-line delegate.

**Compatibility risks**
- `verify_*.py` call `PipelineService` directly.
- All moved methods must keep the same signature and return value.

**Tests required**
- All tests from milestone 1.
- `python verify_console.py` (smoke)
- `python verify_turbo_studio.py` (with demo providers)

**Rollback/fallback**
- Keep `PipelineService` methods as re-exports if a service is not ready.

---

## 3. PostgreSQL repository implementation

**Objective**
Add a PostgreSQL backend behind a feature flag.

**Prerequisites**
- Repository interfaces and application services.

**Exact modules to change/create**
- `infrastructure/postgres_repositories.py`
- `infrastructure/postgres_connection.py`
- `alembic/` migrations.
- `ConsoleApp` can choose `LocalFilesystem` vs `PostgreSQL`.

**Compatibility risks**
- JSONB schema mismatch.
- Local filesystem projects must remain openable.

**Tests required**
- Repository tests run against PostgreSQL test container.
- `pytest tests/ -q` with `USE_POSTGRES=1`.

**Rollback/fallback**
- Feature flag `USE_POSTGRES=0` returns to filesystem.

---

## 4. Object storage abstraction

**Objective**
Media in object storage (or local filesystem adapter), metadata in PostgreSQL.

**Prerequisites**
- PostgreSQL repository for artifact metadata.

**Exact modules to change/create**
- `infrastructure/object_storage.py`
- `LocalObjectStorage`, `S3ObjectStorage`, `MinIOObjectStorage`.
- `Storage.resolve()` for media returns a temporary local path when needed.

**Compatibility risks**
- `local_pipeline` expects a filesystem path.

**Tests required**
- `python verify_real_preview.py`
- `python verify_short_poc.py`

**Rollback/fallback**
- `MEDIA_STORAGE_BACKEND=local` is always the default.

---

## 5. Authentication, tenant identity and RBAC

**Objective**
Add real authn context; `Ownership` comes from token, not body/header.

**Prerequisites**
- PostgreSQL for users/workspaces/membership.

**Exact modules to change/create**
- `services/api/main.py` — JWT/session middleware.
- `application/auth_service.py`
- `infrastructure/user_repository.py`
- `creator_console/api.py` — `Ownership` from middleware.

**Compatibility risks**
- Local console needs synthetic identity.
- `verify_*.py` may need auth bypass mode.

**Tests required**
- `python -m pytest services/api/tests/test_contract.py -q`
- RBAC unit tests.

**Rollback/fallback**
- `LOCAL_DEV_BYPASS_AUTH=1` for existing workflows.

---

## 6. Distributed job execution

**Objective**
Jobs in PostgreSQL, workers as separate processes/containers.

**Prerequisites**
- Auth, object storage.

**Exact modules to change/create**
- `infrastructure/job_queue.py`
- `creator_console/worker.py` — container-friendly.
- `application/job_service.py`

**Compatibility risks**
- Lease semantics must remain identical.

**Tests required**
- `python verify_long_form_studio.py` (demo providers)
- `python verify_turbo_studio.py` (demo providers)

**Rollback/fallback**
- `USE_QUEUE=file` keeps in-process workers.

---

## 7. Turbo / Long-form stage job-ification

**Objective**
Each stage becomes a `JobStore` job; studio drivers become state-machine readers.

**Prerequisites**
- Distributed jobs.

**Exact modules to change/create**
- `application/studios.py`
- `creator_console/turbo_studio.py` delegate.
- `creator_console/long_form_studio.py` delegate.
- New `JOB_TYPES` for stages.

**Compatibility risks**
- Stage state machine must be preserved.
- Event log format should not change.

**Tests required**
- `python verify_turbo_studio.py`
- `python verify_long_form_studio.py`

**Rollback/fallback**
- Keep synchronous stage execution under `STAGES_SYNC=1`.

---

## 8. EDL v3

**Objective**
Additive next-generation editor model.

**Prerequisites**
- Object storage, application `EditorService`.

**Exact modules to change/create**
- `local_pipeline/scene_editor_v3.py`
- `application/editor_service.py` v3 support.
- `POST /edl/v3/*`.

**Compatibility risks**
- Existing v2 EDL projects must remain renderable.

**Tests required**
- `pytest tests/test_scene_editor.py`
- Browser tests for Render Atelier.

**Rollback/fallback**
- v2 remains default until v3 parity is proven.

---

## 9. Connected accounts, secrets, YouTube OAuth

**Objective**
Production secret and OAuth management.

**Prerequisites**
- Auth, object storage.

**Exact modules to change/create**
- `infrastructure/secrets_manager.py`
- `application/connected_account_service.py`
- `local_pipeline/publish_youtube.py` token refresh.

**Tests required**
- `python verify_publish.py` (manual/real OAuth)
- Unit tests for token refresh.

**Rollback/fallback**
- Env-based secrets remain for local dev.

---

## 10. Automation policy

**Objective**
Workspace automation templates and runner.

**Prerequisites**
- Connected accounts, jobs, approval policy.

**Exact modules to change/create**
- `application/automation_service.py`
- `application/approval_service.py` policy.
- `infrastructure/automation_repository.py`.

**Tests required**
- `python verify_short_poc.py` autopilot template.

**Rollback/fallback**
- Default to `manual_all`.

---

## 11. UI consolidation

**Objective**
React reaches feature parity; Creator Console screens retire.

**Prerequisites**
- All prior milestones functional.

**Exact modules to change/create**
- New React screens for Turbo, Long-form, Workspace, Admin.
- Remove retired `creator_console/static` screens.

**Tests required**
- `python verify_browser.py`
- `python verify_turbo_browser.py`
- `python verify_lifecycle_browser.py`

**Rollback/fallback**
- Creator Console kept as a hidden `/console/local` route.

---

## Exact execution order

1. Repository interfaces + local filesystem
2. Application services behind `PipelineService`
3. PostgreSQL
4. Object storage
5. Auth + tenant + RBAC
6. Distributed jobs
7. Turbo/Long-form stage job-ification
8. EDL v3
9. Connected accounts + secrets
10. Automation policy
11. UI consolidation
