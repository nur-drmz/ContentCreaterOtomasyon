# DEPENDENCY_MAP — PROMPT 0

> Import and caller graph extracted from source on 2026-09-23. Excludes `node_modules`, `data/`, `__pycache__`.

## 1. Python modules by directory

| Directory | File count | Main exports / responsibilities |
|---|---|---|
| `local_pipeline/` | 34 | `render_utils`, `scene_editor`, `publish_youtube`, `google_ai`, `tts`, `turbo`, `long_form`, `stage_machine`, `video_analysis`, `source_strategy`, `provider_adapters` |
| `creator_console/` | 30 | `ConsoleApi`, `PipelineService`, `ConsoleApp`, `JobStore`, `Storage`, `Audit`, `Worker`, `TurboStudio`, `LongFormStudio`, `TTSRunner` |
| `services/api/` | 3 | FastAPI `main.py` with `/health` and `ConsoleApi` delegation |
| `tests/` | 8 | pytest unit/integration suite |
| `tools/` | 4 | colab bundle, notebook builder |

## 2. `PipelineService` callers

Files that construct or call `PipelineService` (or `service` in `api.py`):

| File | Usage |
|---|---|
| `creator_console/api.py` | `service = app.pipeline_service` inside `ConsoleApi.handle` for every project-scoped route |
| `creator_console/app.py` | `ConsoleApp.pipeline_service` property wires storage, ownership, audit |
| `creator_console/jobs.py` | `_run_job_for_project` calls `PipelineService` methods per `job_type` |
| `creator_console/diagnostics.py` | Builds `PipelineService` for end-to-end diagnostics |
| `creator_console/faults.py` | Uses `PipelineService` to read project state for fault reports |
| `verify_*.py` (many) | Test harnesses construct `ConsoleApp` and access `.pipeline_service` |

## 3. Direct `local_pipeline` imports outside `pipeline_service.py`

Files in `creator_console/` that import `local_pipeline` directly (not through `PipelineService`):

| File | Imports | Risk |
|---|---|---|
| `creator_console/turbo_studio.py` | `from local_pipeline import turbo, stage_machine` | **Boundary leak** — production media logic inside a console module. |
| `creator_console/long_form_studio.py` | `from local_pipeline import long_form, stage_machine` | **Boundary leak** — same as above. |
| `creator_console/tts_runner.py` | `from local_pipeline import tts` | Used by console TTS job wrapper; still a leak. |
| `creator_console/render_profile.py` | `from local_pipeline import render_utils` | Reads render constants, no execution. |
| `creator_console/status.py` | `from local_pipeline import turbo, source_strategy` | Reads `MODE_PIPELINE_MODE`/`SOURCE_STRATEGY_LABELS`; no execution. |
| `creator_console/fixtures.py` | `from local_pipeline import google_ai` | Test/demo fixture generation. |
| `creator_console/voice_review.py` | `from local_pipeline import tts` | Uses `tts_chunks` for review helper. |

Assessment: the strict "only `PipelineService`" rule is violated by `turbo_studio.py` and `long_form_studio.py`, which drive the stage machine directly. For SaaS, these should be refactored behind `PipelineService` or converted to job types.

## 4. Provider SDK construction

All provider SDK clients are constructed in `local_pipeline/` or `creator_console/secrets_ref.py`:

| Provider | Construction point | Notes |
|---|---|---|
| Google GenAI | `local_pipeline/google_ai.py:324` | `genai.Client(api_key=credential.value)` lazy import |
| OpenAI | `local_pipeline/provider_adapters.py` | Adapter receives an `openai.OpenAI`-compatible client; does **not** construct its own |
| Groq | `local_pipeline/provider_adapters.py` | OpenAI-compatible chat completion wrapper |
| NVIDIA NIM | `local_pipeline/provider_adapters.py` | OpenAI-compatible chat completion wrapper |
| ElevenLabs | `local_pipeline/tts.py` | HTTP call with `XI-Api-Key` header |
| YouTube Data API | `local_pipeline/publish_youtube.py` | Uses injected `httpx`/`requests` sessions; no SDK object persisted |
| OAuth token storage | `creator_console/secrets_ref.py` | `SecretRef` with `kind`; values never returned in API |

No direct `openai.OpenAI()` construction inside `creator_console/`, `tests/`, or `apps/web/`.

## 5. File I/O paths

All persistent project data flows through `creator_console/storage.py:Storage.resolve(...)`, which enforces `CONSOLE_ROOT` and namespace isolation. Direct `Path` / `open()` usage exists but is limited to:

- `creator_console/pipeline_service.py` — uses `Storage.resolve()`; some `shutil.rmtree`/`mkdir` for runtime directories.
- `creator_console/turbo_studio.py` — event log append/read.
- `creator_console/long_form_studio.py` — same.
- `creator_console/diagnostics.py` — writes to `tempfile.mkdtemp` diagnostic scratch.
- `creator_console/server.py` — `__file__` upload endpoints (`__raw__`/`__file__` payloads).
- `creator_console/api.py` — `STATIC_DIR` file serving.
- `creator_console/audit.py` — log append to `audit.log`.

## 6. Approval state transitions

Source: `local_pipeline/approval.py`.

The approval model is not a finite state machine with transitions. It is a **content-hash-bound checklist**:

1. Each approval (`render`, `voice`, `copyright`, `publish_intent`, `publish_visibility`) records:
   - `kind`
   - `decision`
   - `actor` (approver)
   - `artifact_digests` (mapping of artifact relative path → SHA-256)
   - `timestamp`
2. `approval.is_valid_for(...)` checks whether the current artifact digests match the recorded digests.
3. If any referenced artifact changed, the approval is **automatically invalidated**.
4. `PipelineService.approve(...)` writes the approval and records `approval.granted` audit.
5. `PipelineService.reject(...)` records `approval.rejected`.

No explicit `self_approval` check. The actor must be different from the original requester by policy, not by code.

## 7. Publish path

```
creator_console/api.py  POST /publish/intent
       ↓
creator_console/pipeline_service.py  create_publish_intent()
       ↓
post_approval required (approval kind = publish_intent)
       ↓
creator_console/api.py  POST /publish/upload
       ↓
creator_console/pipeline_service.py  execute_publish_upload()
       ↓
local_pipeline/publish.py  →  local_pipeline/publish_youtube.py
       ↓
resumable `videos.insert` with privacyStatus=private
       ↓
creator_console/api.py  POST /publish/visibility/approve
       ↓
local_pipeline/publish_youtube.py  set_visibility(...)
```

Visibility values: `private`, `unlisted`, `public`, `scheduled`.
`scheduled` requires `privacyStatus=private` + `publishAt`.

## 8. Job lifecycle

```
ConsoleApi / automation / PipelineService  →  JobStore.submit(idempotency_key)
       ↓
worker.py  →  _next_claimable()  →  store.claim(job_id, worker_id)
       ↓
ConsoleApp._execute_job(job_type)
       ↓
PipelineService method for job_type
```

`JOB_TYPES`: `source_ingest`, `source_assessment`, `video_analysis`, `transcript`, `script_generation`, `tts`, `render`, `metadata_generation`, `publish_upload`, `visibility_change`.

## 9. React fetch map

Source: `apps/web/src/screens/*.jsx`.

| Screen | Route | Method | Purpose |
|---|---|---|---|
| `TrendRadarScreen` | `/api/projects/{id}/source-candidates` | POST | trend search |
| `SceneEditorScreen` | `/api/projects/{id}/edl` | POST | build EDL |
| `SceneEditorScreen` | `/api/projects/{id}/edl/clips/0` | PATCH | update clip |
| `SceneEditorScreen` | `/api/projects/{id}/edl/clips` | POST | add clip |
| `SceneEditorScreen` | `/api/projects/{id}/edl/preview` | POST | preview command |
| `SceneEditorScreen` | `/api/projects/{id}/edl/render` | POST | real render |
| `RenderAtelierScreen` | `/api/projects/{id}/edl` | GET/POST | load/build |
| `RenderAtelierScreen` | `/api/projects/{id}/edl/clips/{i}` | PATCH | clip update |
| `RenderAtelierScreen` | `/api/projects/{id}/edl/clips` | POST | add clip |
| `RenderAtelierScreen` | `/api/projects/{id}/edl/preview` | POST | preview |
| `RenderAtelierScreen` | `/api/projects/{id}/edl/render` | POST | render |
| `RenderAtelierScreen` | `/api/projects/{id}/edl/revision` | POST | request revision |
| `PublishScreen` | `/api/projects/{id}/publish/state` | GET | status |
| `PublishScreen` | `/api/projects/{id}/metadata/generate` | POST | generate metadata |

All `API_ROOT` defaults to relative; Vite proxy forwards to FastAPI.

## 10. Tests and verification scripts

| Category | Files | Notes |
|---|---|---|
| Unit / integration | `tests/test_*.py` | 128 pass, fast, mock providers |
| API contract | `services/api/tests/test_contract.py` | 5 pass |
| Scenario / E2E | `verify_*.py` (35+) | Many require real providers; ran `verify_clip_bounds.py` (47/47) and `verify_revision_flow.py` (39/39) successfully with demo providers |
| Browser | `verify_browser.py`, `verify_turbo_browser.py`, `verify_lifecycle_browser.py` | Selenium / browser automation |

## 11. Cross-boundary calls to watch

```
apps/web/src → (HTTP) → services/api/main.py → creator_console/api.py
                                                    ↓
creator_console/pipeline_service.py → local_pipeline/*
```

The only direct `local_pipeline` consumer outside `creator_console/` is:

- `tests/test_scene_editor.py` — tests `local_pipeline.scene_editor` directly.
- `tests/test_publish.py`, `test_subtitle_edit.py`, etc. — test `local_pipeline` modules.
- `verify_*.py` scripts — many test `local_pipeline` directly.

For SaaS, the production boundary is `services/api/main.py → creator_console/api.py → PipelineService → local_pipeline`.
