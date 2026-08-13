# Claude Prompt — Sequential Worker and Source Preflight

Prepared on 2026-07-26. Copy the complete prompt below into Claude.

---

Continue working in the existing project:

`C:\Users\birka\Desktop\youtube automation`

The previous data-integrity and Moment Explorer work was independently verified:

- Ruff passes.
- Django system check passes.
- Migration check is clean.
- 35 tests pass.
- AnalysisRun project/source integrity is enforced.
- Demo seeding is transactional and idempotent.
- Moment decision summaries update correctly from authoritative server data.

Preserve all current behavior and tests.

Before changing code, read the current versions of:

- `docs/pre-alpha-local/00-index.md`
- `docs/pre-alpha-local/01-product-requirements.md`
- `docs/pre-alpha-local/02-local-architecture.md`
- `docs/pre-alpha-local/03-media-ai-pipeline.md`
- `docs/pre-alpha-local/04-data-model-and-jobs.md`
- `docs/pre-alpha-local/05-media-lifecycle.md`
- `docs/pre-alpha-local/06-security-privacy.md`
- `docs/pre-alpha-local/07-testing-evaluation.md`
- `docs/pre-alpha-local/08-implementation-milestones.md`
- `docs/pre-alpha-local/09-development-workflow.md`
- `content_studio/settings.py`
- `studio/models.py`
- `studio/services.py`
- `studio/views.py`
- `studio/forms.py`
- `scripts/poc/transcribe_sample.py`
- `tests/`

Architecture constraints:

- Native Windows 11 only.
- Python 3.12 and Django 5.2.
- SQLite WAL.
- One sequential database-backed worker.
- No Celery, Redis, PostgreSQL, Docker, WSL, Linux or Bash dependency.
- Source library: `G:\Youtube kaynak videolar`
- Source files are strictly read-only.
- Never copy, move, rename, overwrite or delete source videos.
- Generated data belongs under `G:\AIContent`.
- Models belong under `D:\AIContent\models`.
- Bind web access to `127.0.0.1` only.
- Do not call cloud services.
- Do not download or load Whisper, Qwen or another AI model.
- PyTorch may only be used to inspect CUDA/device/VRAM availability.
- Do not run `start-app.ps1`, `start-app.cmd`, Waitress, runserver or another
  persistent process during this task.
- Use Django test client and management-command tests.
- Do not change Git configuration or create a commit.
- Do not modify the approved local static-file route.

Implement only the first safe stage of the sequential worker: source preflight.

## 1. Extend operational persistence

Review the existing Job model against
`docs/pre-alpha-local/04-data-model-and-jobs.md`.

Add only fields required by this worker, such as:

- `attempt_count`
- `cancel_requested`, if needed for checkpoint-safe cancellation
- updated timestamps if required for claiming/recovery

Add a JobCheckpoint model with at least:

- UUID primary key
- Job foreign key
- stage
- status
- artifact/result JSON
- stage configuration hash
- started_at
- completed_at
- failure_code
- failure_detail

Requirements:

- A stage must be unique per job and stage configuration hash.
- Completed valid checkpoints must be reusable.
- Do not silently overwrite completed checkpoint results.
- Use explicit status choices.
- Create the required Django migration.
- Do not modify or replace existing migrations.

Add a JSON metadata field to SourceAsset only if needed to persist structured
FFprobe information. Continue using existing `duration_seconds` and `container`
fields for their canonical values.

## 2. Implement a race-safe sequential job claim

Create a worker service module, for example:

`studio/worker.py`

Implement a function that claims the oldest eligible queued
`ANALYZE_SOURCE` job.

Requirements:

- Claim at most one job.
- Use a short atomic database transaction.
- Use a conditional update from QUEUED to RUNNING so two worker processes
  cannot both claim the same job.
- Do not hold a database transaction open while FFprobe, filesystem or CUDA
  checks run.
- Increment `attempt_count` when a job is successfully claimed.
- Set `started_at` and `current_stage`.
- Return `None` when no eligible job exists.
- The design must work with SQLite's limitations; do not assume PostgreSQL row
  locking.
- Add concurrency/claim tests that prove a second claim does not receive the
  already claimed job.

The Pre-Alpha still supports only one intended worker, but the claim must not
be trivially duplicateable.

## 3. Implement source-path validation

Build the absolute source path only from:

- `settings.SOURCE_LIBRARY_ROOT`
- `SourceAsset.relative_path`

Requirements:

- Resolve the canonical path.
- Verify it remains inside the approved source root.
- Verify the file exists and is a regular supported MP4, MKV or MOV file.
- Reject path traversal.
- Reject an absolute path stored as `relative_path`.
- Reject unsupported extensions.
- Do not follow a resolved path outside the allowlisted root.
- Never open the source for writing.
- Never change permissions or timestamps.
- Never create files beside the source video.

Reuse existing safe path helpers where appropriate rather than duplicating
inconsistent logic.

## 4. Implement native Windows FFprobe discovery

Do not assume FFprobe is globally available in PATH.

Resolution order:

1. Explicit `FFPROBE_PATH` environment/config setting.
2. `shutil.which("ffprobe")`.
3. A narrowly scoped search under the current user's WinGet package directory
   for the installed Gyan FFmpeg Shared package.

Known installation family:

`%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg.Shared_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-*\bin\ffprobe.exe`

Requirements:

- Do not hard-code the currently installed FFmpeg version.
- Validate that the resolved path exists and is a file.
- Return a structured `FFPROBE_NOT_FOUND` failure if not found.
- Do not modify PATH globally.
- Do not install or download FFmpeg.

## 5. Implement safe FFprobe inspection

Run FFprobe using `subprocess` with:

- argument list, not a shell command string;
- `shell=False`;
- hidden-window/no-console flags where supported on Windows;
- a bounded timeout;
- JSON output;
- captured stdout/stderr;
- no user-controlled shell interpolation.

Collect and normalize:

- container/format name
- duration
- file size
- video codec
- width
- height
- frame rate
- audio codec
- audio sample rate
- audio channel count
- subtitle stream count
- stream counts
- FFprobe executable/version if available

Handle:

- process timeout
- non-zero exit
- malformed JSON
- missing video stream
- missing/invalid duration
- inaccessible file

Do not store the entire unbounded raw FFprobe output. Persist only the
normalized metadata and limited diagnostic information.

Update SourceAsset only after successful validation:

- `duration_seconds`
- `container`
- normalized probe metadata

## 6. Implement Pre-Alpha limits

Enforce:

- maximum source size: 20 GB
- maximum duration: 180 minutes
- supported containers/extensions: MP4, MKV and MOV
- at least one video stream

Use settings for thresholds rather than duplicating magic numbers.

Return structured failure codes, for example:

- `SOURCE_NOT_FOUND`
- `SOURCE_OUTSIDE_LIBRARY`
- `SOURCE_TYPE_UNSUPPORTED`
- `SOURCE_TOO_LARGE`
- `FFPROBE_NOT_FOUND`
- `FFPROBE_TIMEOUT`
- `FFPROBE_FAILED`
- `FFPROBE_INVALID_JSON`
- `SOURCE_DURATION_INVALID`
- `SOURCE_TOO_LONG`
- `VIDEO_STREAM_MISSING`

User-facing failure details must be actionable and must not include secrets or
unbounded FFprobe output.

## 7. Implement storage preflight

Use `shutil.disk_usage()` against configured roots or their nearest existing
parent.

Check:

- `G:\AIContent` generated-media root
- `D:\AIContent\models` model root

Use configurable minimum-free-space settings.

If the planning documents already define thresholds, use them. If no exact
threshold exists, add clearly named settings with conservative documented
Pre-Alpha defaults:

- generated-media free space: 20 GB
- model-drive free space: 10 GB

Return normalized values:

- root
- total bytes
- used bytes
- free bytes
- required free bytes
- pass/fail

Failure codes:

- `GENERATED_STORAGE_UNAVAILABLE`
- `GENERATED_STORAGE_LOW`
- `MODEL_STORAGE_UNAVAILABLE`
- `MODEL_STORAGE_LOW`

Do not create, delete or clean files during preflight.

## 8. Implement CUDA/VRAM preflight without loading a model

Use installed PyTorch only.

Collect:

- torch version
- CUDA runtime version
- `torch.cuda.is_available()`
- GPU name
- total VRAM
- free VRAM where supported

Requirements:

- Do not download or load an AI model.
- Do not allocate a large test tensor.
- Catch CUDA initialization errors.
- Add configurable `MIN_FREE_VRAM_BYTES`, default 8 GB for the full-GPU
  Pre-Alpha workflow.
- If CUDA is unavailable or VRAM is below the threshold, fail gracefully with
  an actionable structured reason.
- Do not terminate games or other GPU applications.
- Do not attempt to free memory owned by other processes.

Failure codes:

- `CUDA_UNAVAILABLE`
- `CUDA_INSPECTION_FAILED`
- `GPU_VRAM_LOW`

## 9. Implement checkpointed preflight execution

Suggested stages:

- `SOURCE_PATH`
- `SOURCE_LIMITS`
- `FFPROBE`
- `STORAGE`
- `CUDA`

For each stage:

- Mark checkpoint RUNNING.
- Store bounded normalized results.
- Mark SUCCEEDED or FAILED.
- Do not rerun a valid SUCCEEDED checkpoint with the same configuration hash.
- Stop at the first failed stage.
- Copy the failure code/detail to Job.
- Set Job status FAILED when preflight fails.

When every preflight stage succeeds:

- Do not mark the complete `ANALYZE_SOURCE` job SUCCEEDED, because transcript,
  scene analysis and moment ranking do not exist yet.
- Set Job status PAUSED.
- Set `current_stage` to a clear value such as:
  `Preflight complete; transcript stage not implemented`
- Store progress as an honest early-pipeline percentage.
- Do not claim the job again while PAUSED.

Respect `cancel_requested` between stages:

- If cancellation is requested, stop before starting the next stage.
- Mark the job CANCELLED.
- Preserve completed checkpoints.

## 10. Add a native management command

Add:

`python manage.py run_worker --once`

Required behavior:

- Claims at most one eligible queued `ANALYZE_SOURCE` job.
- Runs only the preflight stages above.
- Exits after that job or immediately when no job exists.
- Produces concise structured console output.
- Does not loop indefinitely.
- Does not start a web server.
- Does not start/download an AI model.
- Returns a non-zero exit code for an unexpected worker crash.
- Expected job validation failures should be recorded in the database and
  handled cleanly rather than dumping an uncontrolled traceback.

Do not implement continuous monitoring in this task.

## 11. Update the UI honestly

On Project Detail, show:

- queued
- running preflight
- failed with structured actionable reason
- paused after successful preflight
- completed preflight metadata summary:
  duration, resolution, video/audio codec, file size and GPU name/free VRAM

Do not claim that full analysis completed.

Do not add a Transcript, Moments Ready or Analysis Complete state until those
stages genuinely exist.

## 12. Testing requirements

All existing 35 tests must continue to pass.

Add tests for at least:

- only one queued job is claimed
- second claim returns `None`
- non-`ANALYZE_SOURCE` jobs are ignored
- `attempt_count` increments once
- database transaction is not held during external inspection
- safe relative source path resolves correctly
- absolute/path-traversal paths are rejected
- unsupported extension is rejected
- missing source is rejected
- FFprobe executable resolution from explicit setting
- FFprobe successful normalized parsing
- FFprobe timeout
- FFprobe non-zero exit
- FFprobe malformed JSON
- missing video stream
- invalid duration
- 20 GB size limit
- 180-minute duration limit
- generated storage low
- model storage low
- CUDA unavailable
- VRAM below threshold
- no AI model is loaded
- successful checkpoints are reused
- failure stops later stages
- cancellation preserves previous checkpoints
- successful preflight leaves `ANALYZE_SOURCE` PAUSED, not SUCCEEDED
- failure records failure_code/failure_detail on Job
- `run_worker --once` exits cleanly with no jobs
- Project Detail renders successful preflight metadata honestly

Mock FFprobe, disk usage and torch/CUDA in unit tests. Do not require the real
sample video, real GPU or installed FFprobe for the automated test suite.

## 13. Verification

Run:

- `ruff check .`
- `python manage.py check`
- `python manage.py makemigrations --check --dry-run`
- `pytest`

Do not run:

- `start-app.ps1`
- `start-app.cmd`
- `runserver`
- Waitress
- a continuous worker
- the real queued job against the sample video
- Whisper or Qwen

Use Django test client for HTTP verification.

If pytest needs a custom temp directory, use a narrowly scoped directory and
remove it afterward.

At completion report:

- Exact files changed.
- Migration created.
- Job/JobCheckpoint schema changes.
- Worker claim algorithm.
- Preflight stages.
- Failure-code list.
- FFprobe discovery behavior.
- Example normalized FFprobe result.
- Example checkpoint result.
- Test count and results.
- Confirmation that no model loaded/downloaded.
- Confirmation that no source video was modified.
- What remains for transcript/Whisper integration.

Implement and verify the work. Do not stop at a written plan.

