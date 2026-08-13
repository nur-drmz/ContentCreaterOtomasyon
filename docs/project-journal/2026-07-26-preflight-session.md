# Session Journal — 2026-07-26 — Source Preflight

## Purpose

This record continues `2026-07-26-session.md`. It contains independently
verified work from the sequential-worker/source-preflight stage. No external
Claude handoff existed; the prepared brief was implemented locally.

## Implemented

Operational persistence now includes:

- `Job.attempt_count`
- `Job.cancel_requested`
- `Job.updated_at`
- `SourceAsset.probe_metadata`
- `JobCheckpoint` with explicit status, normalized result, configuration hash,
  timestamps and structured failure fields
- a uniqueness constraint over job, stage and stage configuration hash

Migration:

`studio/migrations/0003_job_attempt_count_job_cancel_requested_and_more.py`

The migration was applied successfully to the local SQLite database.

The worker now provides:

- oldest-first queued `ANALYZE_SOURCE` claim;
- a short atomic transaction and conditional `QUEUED` to `RUNNING` update;
- one successful claimant at most;
- attempt count increment on a successful claim;
- no database transaction around filesystem, FFprobe, disk or CUDA inspection;
- checkpoint reuse for successful stages with the same configuration hash;
- checkpoint-safe cancellation between stages;
- stop-on-first-failure behavior;
- `PAUSED`, not `SUCCEEDED`, after successful preflight because transcript
  processing is not implemented.

Management command:

`python manage.py run_worker --once`

It claims at most one job, emits concise JSON and exits. There is no continuous
mode.

## Preflight stages

1. `SOURCE_PATH`
2. `SOURCE_LIMITS`
3. `FFPROBE`
4. `STORAGE`
5. `CUDA`

Successful completion pauses at 25% with:

`Preflight complete; transcript stage not implemented`

## Source and FFprobe safety

- Source paths are built only from the configured library root and stored
  relative path.
- Absolute paths, traversal, alternate-data-stream style components, path
  escapes, missing files and unsupported extensions are rejected.
- Resolved files must remain regular files inside the allowlisted source root.
- Source size is checked again immediately before FFprobe to reduce
  time-of-check/time-of-use risk.
- FFprobe validates the actual reported container, duration and presence of a
  video stream.
- FFprobe receives a native argument array with `shell=False`, hidden-window
  flags on Windows, a timeout and captured JSON output.
- Persisted probe data is normalized; raw FFprobe JSON is not stored.
- User-visible FFprobe failures do not copy raw stderr or absolute source
  paths.

FFprobe discovery order is:

1. configured `FFPROBE_PATH`;
2. `shutil.which("ffprobe")`;
3. the version-independent Gyan Shared WinGet package pattern.

Normal Windows-permission inspection verified that the installed FFprobe is
discovered at its current WinGet location. A sandboxed inspection initially
could not read that AppData package directory; this was a tool sandbox
restriction, not an application discovery defect.

## Normalized metadata

The automated success test uses a bounded normalized result equivalent to:

```json
{
  "container": "mov,mp4,m4a,3gp,3g2,mj2",
  "duration_seconds": 1328.599,
  "size_bytes": 214073347,
  "video_codec": "h264",
  "width": 1920,
  "height": 1080,
  "frame_rate": 23.976024,
  "audio_codec": "aac",
  "audio_sample_rate": 44100,
  "audio_channels": 2,
  "subtitle_stream_count": 0,
  "stream_count": 2
}
```

This is a mocked unit-test fixture based on the previously verified sample
metadata. It is not a claim that the new worker has run against the real
source.

## Resource checks

- Generated-media minimum free space: 20 GB.
- Model-drive minimum free space: 10 GB.
- Minimum free VRAM: 8 GB.
- Storage checks inspect the configured root or nearest existing parent and
  never create or clean files.
- CUDA inspection uses PyTorch device APIs only. It does not load a model,
  allocate a large tensor, terminate another process or attempt to reclaim
  another process's VRAM.

Read-only environment inspection at the end of this session found:

- D: approximately 89.6 GB free.
- G: approximately 650.8 GB free.
- RTX 3080 total VRAM: 10,240 MiB.
- RTX 3080 free VRAM at inspection: 3,761 MiB.

Because free VRAM was below the approved 8 GB threshold, no real source
preflight job was queued or run.

## Failure codes

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
- `GENERATED_STORAGE_UNAVAILABLE`
- `GENERATED_STORAGE_LOW`
- `MODEL_STORAGE_UNAVAILABLE`
- `MODEL_STORAGE_LOW`
- `CUDA_UNAVAILABLE`
- `CUDA_INSPECTION_FAILED`
- `GPU_VRAM_LOW`

## UI

Project Detail now distinguishes queued, running preflight, structured failure
and paused-after-preflight states. A successful FFprobe plus CUDA checkpoint
shows duration, resolution, video/audio codec, file size, GPU and free VRAM.
The page explicitly says full analysis has not run.

Paused preflight jobs are treated as active so repeated button presses do not
silently create duplicate analysis jobs.

## Tests and verification

Verified after the final changes:

- Ruff: passed.
- Django system check: passed.
- Migration drift check: no changes.
- pytest: 62 passed.

Tests cover job claim behavior, second-claim exclusion, ignored job types,
attempt count, external work outside the claim transaction, path containment,
FFprobe discovery and normalization, FFprobe failures, source limits, actual
container validation, storage thresholds, CUDA/VRAM failures, checkpoint
reuse, cancellation, stop-on-failure, honest paused completion, idle worker
command and Project Detail metadata.

No AI model was loaded or downloaded. No source video was modified. No
persistent server or continuous worker was launched.

## Files changed

- `content_studio/settings.py`
- `studio/models.py`
- `studio/preflight.py`
- `studio/worker.py`
- `studio/views.py`
- `studio/management/commands/run_worker.py`
- `studio/migrations/0003_job_attempt_count_job_cancel_requested_and_more.py`
- `templates/studio/project_detail.html`
- `tests/test_worker.py`
- `docs/pre-alpha-local/08-implementation-milestones.md`
- `docs/project-journal/README.md`
- this journal file

## Continuation point

1. Wait until at least 8 GB VRAM is free; do not terminate the user's GPU
   applications.
2. Queue one real sample analysis job through the existing CSRF-protected UI.
3. Run only `python manage.py run_worker --once`.
4. Verify all five real checkpoints and the honest Project Detail summary.
5. Keep Whisper disabled during that verification.
6. After real preflight succeeds, design and implement the transcript cascade:
   embedded English subtitle, matching sidecar SRT, then local Whisper.
7. Git still has no baseline commit because user name/email are not configured;
   do not invent them.
