# Local Pre-Alpha Data Model and Jobs

**Status:** Approved planning baseline

## 1. Conventions

- SQLite uses foreign keys and WAL mode.
- IDs are UUID strings.
- Mutable analysis/timeline outputs are versioned rather than overwritten.
- JSON is used for rapidly changing feature vectors and render settings;
  transcript segments, scenes, moments and jobs remain queryable rows.
- Filesystem paths are stored relative to an approved storage root.

## 2. Core tables

```text
projects(
  id, title, season, episode, known_characters_json,
  target_duration_seconds, rights_accepted_at,
  created_at, updated_at, archived_at
)

source_assets(
  id, project_id, relative_path, filename, size_bytes,
  duration_seconds, container, content_hash, imported_at
)

analysis_runs(
  id, project_id, source_asset_id, pipeline_version,
  config_hash, status, current_stage, started_at, completed_at
)

transcript_segments(
  id, analysis_run_id, start_ts, end_ts, text,
  source, confidence, language
)

scenes(
  id, analysis_run_id, start_ts, end_ts,
  deterministic_features_json, vlm_features_json
)

hot_moments(
  id, analysis_run_id, start_ts, end_ts,
  core_start_ts, core_end_ts, base_score,
  score_breakdown_json, summary, characters_json,
  spoiler_level, reasons_json
)

focus_prompt_runs(
  id, project_id, analysis_run_id, prompt_text,
  formula_version, created_at
)

moment_rankings(
  id, focus_prompt_run_id, hot_moment_id,
  prompt_relevance, final_score, rank
)

moment_selections(
  id, project_id, hot_moment_id,
  decision, order_index, trim_in, trim_out, locked, note
)

timeline_versions(
  id, project_id, version_number, parent_version_id,
  target_duration_seconds, captions_enabled,
  clips_json, created_at
)

render_artifacts(
  id, timeline_version_id, relative_path,
  width, height, codec, duration_seconds,
  render_config_hash, created_at
)
```

## 3. Operational tables

```text
jobs(
  id, project_id, job_type, status, current_stage,
  progress, idempotency_key, cancel_requested,
  attempt_count, failure_code, failure_detail,
  queued_at, started_at, finished_at
)

job_checkpoints(
  id, job_id, stage, status, artifact_refs_json,
  stage_config_hash, started_at, completed_at
)

media_assets(
  id, project_id, asset_type, storage_root,
  relative_path, size_bytes, retention_expires_at,
  trashed_at, deleted_at, created_at
)

audit_events(
  id, project_id, action, target_type,
  target_id, details_json, created_at
)
```

## 4. Constraints

- One current successful analysis per `(source_hash, pipeline_version,
  config_hash)`.
- Timeline version numbers are unique per project.
- Selection is unique per `(project_id, hot_moment_id)`.
- `trim_in < trim_out` and both remain within the moment/source range.
- Character values from VLM must be in the project allowlist or `unknown`.
- All resolved asset paths must stay under their declared storage root.

## 5. Job states

```text
QUEUED
RUNNING
PAUSE_REQUESTED
PAUSED
CANCEL_REQUESTED
CANCELLED
SUCCEEDED
FAILED
```

The worker honors pause/cancel between checkpoints. A failed job retains every
valid completed checkpoint. Retry increments `attempt_count` and starts at the
first missing or invalid checkpoint.

## 6. Job types

- `ANALYZE_SOURCE`
- `RERANK_MOMENTS`
- `GENERATE_PREVIEWS`
- `PROPOSE_TIMELINE`
- `RENDER_ROUGH_CUT`
- `CLEAN_CACHE`
- `EMPTY_EXPIRED_TRASH`
- `BACKUP_METADATA`

Only one GPU-heavy job runs at a time.

