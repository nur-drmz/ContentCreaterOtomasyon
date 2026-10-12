# CURRENT_STATE — Repository Truth Audit

> Generated during PROMPT 0. This document describes the repository as it is, not as the older README/CONSOLE/ROADMAP documents claim.

## 1. Repository layout

```
YoutubeOtomationv0/
├── local_pipeline/          # canonical, deterministic media processing
├── creator_console/         # project/artifact/job orchestration + HTTP API
├── services/api/            # FastAPI adapter; thin transport layer
├── apps/web/                # Vite + React screens (trend, scene, publish, atelier)
├── tests/                   # pytest unit/integration (128 pass)
├── verify_*.py              # 35+ end-to-end scripts, many using demo providers
├── docs/proje/              # 14-file detailed Turkish documentation
└── tools/                   # colab bundle, notebook builder, payload refresher
```

## 2. Runtime boundaries

| Boundary | File | Responsibility |
|---|---|---|
| HTTP entry | `creator_console/api.py` | `ConsoleApi.handle(method, path, body, headers) → (status, payload)`; 83 `rest ==` routes + 18 special patterns |
| FastAPI adapter | `services/api/main.py` | `/health`, CORS-free, forwards all requests to `ConsoleApi` |
| Console app | `creator_console/app.py` | `ConsoleApp` lifespan, storage, worker, audit wiring |
| Pipeline service | `creator_console/pipeline_service.py` | **Only** console boundary into `local_pipeline`; 4300+ lines, single `PipelineService` class |
| Media logic | `local_pipeline/*.py` | `render_utils`, `scene_editor`, `publish_youtube`, `google_ai`, `tts`, `turbo`, `long_form`, etc. |
| Web frontend | `apps/web/src/*.jsx` | 4 screens; all API calls via `fetch` to `/api/projects/${projectId}/...` |
| Worker | `creator_console/worker.py` | Independent OS process; claims jobs with compare-and-set + lease |
| Storage | `creator_console/storage.py` | `Storage.resolve(...)`; all file access is supposed to go through here |

## 3. Key invariants (source-verified)

| Invariant | Where enforced | Status |
|---|---|---|
| `local_pipeline` is canonical media logic | `creator_console/pipeline_service.py` imports and calls `local_pipeline` functions; no other console module imports `local_pipeline` except `turbo_studio.py`, `long_form_studio.py`, `tts_runner.py`, `render_profile.py` (all inside console boundary). | **Holds** with documented exceptions below. |
| All console → media calls go through `PipelineService` | `api.py`, `automation.py`, `app.py` use `PipelineService`. | **Holds**. `diagnostics.py` uses `google_ai`/`render_utils` directly for diagnostic fixtures (acceptable, not production flow). |
| Approval is SHA-bound and invalidated on content mutation | `local_pipeline/approval.py`; `creator_console/pipeline_service.py` computes artifact digests before approval. | **Holds**. `PRODUCTION_READY_ALLOWED` requires prior gates. |
| No self-approval | `approval.py` stores `requested_by` and `approved_by`; no explicit actor==approver check found. | **Risk**: policy is not enforced in code (see MIGRATION_RISKS). |
| Private-first YouTube upload | `local_pipeline/publish_youtube.py:26` — `INITIAL_PRIVACY` hardcoded; `upload_private` refuses public insert. | **Holds**. Visibility change requires a separate approval gate. |
| Rights gate before third-party ingest | `creator_console/api.py`: `POST /rights` is required before `POST /ingest` (enforced by workflow). | **Holds** at workflow level. |
| Idempotent job submission | `creator_console/jobs.py`: `idempotency_key` derived from `(project, job_type, inputs)`; duplicate key returns existing job. | **Holds**. |
| Worker single-claim | `creator_console/worker.py` + `jobs.py` `claim()` uses compare-and-set + lease. | **Holds**. |
| No quota evasion by account rotation | `creator_console/quota_signals.py` tracks quota; no hardcoded account switcher. | **Holds**. |
| No raw secrets in API responses | `creator_console/secrets_ref.py`, `creator_console/redaction.py`, `api.py` POST `/api/settings/secrets` returns only `kind`. | **Holds**. |
| Long-running work is a job, not HTTP | `PipelineService` submits jobs for `video_analysis`, `transcript`, `script_generation`, `tts`, `render`, `publish_upload`, etc. | **Holds** for production phases. `/edl/preview` and `/edl/render` in Scene Editor run synchronously as exceptions. |

## 4. Artifact schema (22 artifacts)

Source: `creator_console/pipeline_service.py:ARTIFACTS`.

| Artifact | Relative path | Notes |
|---|---|---|
| `project` | `project.json` | project metadata |
| `source_candidates` | `results/source_candidates.json` | trend research output |
| `rights_decision` | `results/rights_decision.json` | rights gate record |
| `ingest_manifest` | `results/ingest_manifest.json` | third-party download record |
| `processing_plan` | `results/processing_plan.json` | Faz 0 plan |
| `source_assessment` | `results/source_assessment.json` | technical assessment |
| `hot_moments` | `results/hot_moments.json` | analysis output |
| `moment_selection` | `results/moment_selection.json` | selected moments |
| `selected_script` | `results/selected_script.json` | finalized script |
| `selected_voiceover` | `results/selected_voiceover.json` | voice/audio track |
| `subtitle_track` | `results/subtitle_track.json` | subtitle cues |
| `moment_transcript` | `results/moment_transcript.json` | transcript per moment |
| `glossary` | `results/glossary.json` | proper names |
| `edl` | `results/edl/edl.json` | Scene Editor EDL (newest, 6 edl audit events) |
| `long_form_plan` | `results/long_form_plan.json` | Long-form plan |
| `turbo_brief` | `results/turbo/brief.json` | Turbo brief |
| `turbo_workflow` | `results/turbo/workflow.json` | Turbo stage machine state |
| `turbo_timeline` | `results/turbo/timeline.json` | Turbo timeline |
| `render_manifest` | `results/render/render_manifest.json` | render output manifest |
| `publication_metadata` | `results/publication_metadata.json` | title/description variants |
| `publish_intent` | `results/publish_intent.json` | upload decision |
| `publish_manifest` | `results/publish_manifest.json` | final upload record |

`PER_SLOT_ARTIFACTS` are dynamically resolved per `slot` (Short output yuvası).

## 5. Job types

Source: `creator_console/jobs.py:JOB_TYPES`.

- `source_ingest`
- `source_assessment`
- `video_analysis`
- `transcript`
- `script_generation`
- `tts`
- `render`
- `metadata_generation`
- `publish_upload`
- `visibility_change`

`JobStore.submit(idempotency_key=...)` guarantees one in-flight/finished record per key.

## 6. React API surface

Source: `apps/web/src/screens/*.jsx`.

| Screen | Calls |
|---|---|
| `TrendRadarScreen` | `POST /source-candidates` |
| `SceneEditorScreen` | `POST /edl`, `POST /edl/clips`, `PATCH /edl/clips/0` (crop/zoom/caption), `POST /edl/preview`, `POST /edl/render` |
| `RenderAtelierScreen` | `GET /edl`, `POST /edl`, `POST /edl/clips`, `PATCH /edl/clips/{i}`, `POST /edl/preview`, `POST /edl/render`, `POST /edl/revision` |
| `PublishScreen` | `GET /publish/state`, `POST /metadata/generate` |

All use `API_ROOT = import.meta.env.VITE_API_ROOT ?? ''` (relative, Vite proxy).

## 7. Test baseline

| Suite | Command | Result |
|---|---|---|
| Unit / integration | `pytest tests/ -q` | **128 passed** |
| API contract | `python -m pytest services/api/tests/test_contract.py -q` | **5 passed** |
| `verify_clip_bounds.py` | `python verify_clip_bounds.py` | **47/47** (mock providers) |
| `verify_revision_flow.py` | `python verify_revision_flow.py` | **39/39** (mock providers) |

No production code was modified in this milestone. Failures, if any in later real-provider verify scripts, should be recorded in `MIGRATION_RISKS.md`.

## 8. Known source-of-truth gaps

- `docs/proje/` is comprehensive but untracked; it reflects the working tree, not the last commit.
- `creator_console/diagnostics.py` and `creator_console/fixtures.py` write to both `Storage` and `tempfile`/`shutil` paths directly for diagnostic/test purposes.
- `creator_console/turbo_studio.py` and `long_form_studio.py` import `local_pipeline` modules directly, not through `PipelineService`. They are still inside the console boundary but break the strict "only `PipelineService`" rule.
- `resolve_edl_revision` exists in `pipeline_service.py` but is not wired to `api.py`.
