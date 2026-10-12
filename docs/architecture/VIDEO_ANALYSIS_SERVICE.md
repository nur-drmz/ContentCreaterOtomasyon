# VideoAnalysisService

> Introduced in Milestone 10 — the extraction that gives the `hot_moments`
> artifact an application-level owner. The Phase 1 orchestration (gate →
> progress → source → engine → artifact) moved out of `PipelineService`
> unchanged; every algorithm stayed canonical in `local_pipeline`.

## Why this boundary exists

`run_video_analysis` was the first production phase that could move cleanly:
its only façade dependencies were `require_phase` (now `ports.WorkflowGateQuery`),
`source_path` (now `ports.SourceQuery`), `tracker` (now `ports.ProgressFactory`),
`binaries` (`MediaBinaries`), `storage.resolve` (`ports.ProjectNamespaceStore`)
and artifact I/O (`ports.ArtifactRepository`). Extracting it establishes the
`hot_moments` ownership boundary that `MomentService` (extracted in
Milestone 11 — see `MOMENT_SERVICE.md`) needs — the
selection phase's prerequisite now has a producer with a name.

## Responsibilities

| Method | Responsibility |
|---|---|
| `run_analysis(...)` | the whole Phase 1 use case: gate check, progress lifecycle, assessment-duration read, range resolution, scope dispatch, engine call, `hot_moments` write, terminal progress |

`run_analysis` is one public method because the two scopes share one
signature, one job type (`video_analysis`), one progress activity and one
output artifact. The scope-specific work stays in private helpers
(`_run_window`, `_run_full_source`), exactly where it was in the façade.

### Parameters (unchanged façade contract)

`clip_start_sec`, `clip_duration_sec`, `focus_region`,
`clip_duration_min`, `custom_start_min`, `analysis_scope` (`"window"` |
`"full_source"`), `whisper_model_size`, `scene_detector`, `top_n`,
`job_id`, `actor`. `PipelineService.run_video_analysis` delegates verbatim —
job dispatch (`app.py` `JOB_EXECUTORS`), notebooks and verify scripts are
untouched.

## Dependencies (all injected)

| Constructor arg | Port / type | Used for |
|---|---|---|
| `artifacts` | `ports.ArtifactRepository` | read `source_assessment` (`source.duration_sec`); write `hot_moments` |
| `namespace` | `ports.ProjectNamespaceStore` | resolve `runtime/` and `results/moment_previews/` inside this project |
| `ownership` | `Ownership` | namespace scoping |
| `source` | `ports.SourceQuery` | `get_source_path()` — the attached, bytes-verified local path |
| `binaries` | `application.media_binaries.MediaBinaries` | FFmpeg/ffprobe resolution |
| `progress` | `ports.ProgressFactory` | the `video_analysis` tracker; cancellation arrives through `bind_cancel_check` on the shared factory |
| `workflow` | `ports.WorkflowGateQuery` | `require_phase("video_analysis")` — denial strings unchanged |

**Not injected:** no audit sink (the repository emits `artifact.written`),
no secrets resolver (the analysis is fully local — no provider call exists
in this path), no approval handle (a finished analysis records
`status="review_required"` candidates only).

## Actual pipeline

`local_pipeline.video_analysis` remains the canonical engine. For the
`window` scope (`run_video_analysis`):

1. Extract the requested clip + audio with FFmpeg (`extract_clip_and_audio`).
2. Transcribe with faster-whisper (`transcribe_audio`). An empty transcript
   is **not** an error — it downgrades to energy+scene scoring and is
   recorded as `transcript_available=False`.
3. Detect scenes with PySceneDetect (`detect_scenes`); compute audio peaks
   (`compute_audio_peaks`).
4. Build candidate windows (`adaptive_candidate_windows=True`), expand to
   moment boundaries (`expand_moment_boundaries`), score and rank
   (`score_hot_moments`) — audio energy, scene density, speech density,
   emotion density, transcript confidence, lexical diversity.
5. Generate preview clips (`generate_preview_clips`) into
   `results/moment_previews/`.
6. Build the manifest (`build_hot_moments_manifest`) — always
   `status="review_required"`. Zero candidates is fatal
   (`RuntimeError("hicbir hot-moment adayi bulunamadi …")`).

The service then tags `analysis_scope="window"` and, when `focus_region`
was used, records `candidate_request` (requested region/minutes plus the
resolved seconds) before persisting the artifact.

### Full-source scope (`run_full_source_analysis`)

A two-pass scan of the *entire* source — this is the mode that produces N
genuinely different Shorts from a long film:

1. **Full scan**: extract full-length audio, compute an energy profile
   (`scan_audio_energy_profile`), select candidate regions
   (`select_candidate_regions`). Falls back to evenly distributed regions
   when audio scanning fails. The full-length WAV is deleted in `finally`.
2. **Region analysis**: each region runs the same window pipeline
   (clip+audio → whisper → scenes → peaks → candidates), moments are merged
   on the absolute source timeline (`_merge_region_moments`), ranked
   film-wide, previews generated.
3. The manifest gains `analysis_scope="full_source"` and a
   `full_source_scan` block (`regions_scanned`, …) consumed by the
   progress-finish message.

`FULL_SOURCE_ANALYSIS_STAGES` (`full_scan → region_analysis → scoring →
previews`) moved with the service; `pipeline_service.
FULL_SOURCE_ANALYSIS_STAGES` still resolves as a compatibility re-export.

## Progress, cancellation, errors

- Activity `video_analysis`, job-typed `video_analysis`; window runs use
  the declared stage set (`preparation → audio → whisper → scenes →
  scoring → previews`), full-source runs use their own honest set.
- `tracker.start` happens *after* the gate — a denied run writes no
  progress record. Every exception inside the run calls
  `tracker.fail("Type: message")` and propagates; `OperationCancelled`
  propagates the same way so the job store can record `cancelled`.
- Cancellation reaches the engine through `raise_if_cancelled` on the
  tracker, bound by `LocalProgressFactory.bind_cancel_check` — the job
  runtime binds once on the façade, every service-built tracker sees it.

## `hot_moments` ownership and read boundary

**Producer:** `VideoAnalysisService` — the only application code that
writes `results/hot_moments.json`. `PipelineService.write_artifact`
remains usable by tests and repair paths, but no other service produces
this artifact.

**Consumers (unchanged):** `MomentService` (Milestone 11 — selection,
`selected_moment`, `clip_bounds_view`), `TranscriptService` (Milestone 12 —
`run_transcript` loads it `required=True` and the canonical engine validates
the selection against it), `outputs`/`batch_state`, script
generation (`validate_moment_selection`), render, the moments panel in
`app.py` — all read through `ArtifactRepository.load_artifact("hot_moments")`.
The artifact is the contract; no dedicated `HotMomentsQuery` port was added
because every consumer already reads through the repository, and
MomentService needs the raw manifest plus its canonical validators
(`validate_hot_moments_schema`, `build_moment_selection`,
`validate_moment_selection`, `hot_moments_content_sha256` — all in
`local_pipeline.video_analysis`).

**Binding semantics:** `moment_selection` records
`hot_moments_content_sha256`; rerunning analysis changes the manifest hash
and `validate_moment_selection` reports
`"hot_moments.json degisti; moment secimi yeniden incelenmeli"` — a rerun
can never silently validate an old selection.

## Invalidation

Rewinding or resetting at `"analysis"` clears `results/hot_moments.json`,
`results/moment_previews`, `results/moment_selection.json`,
`results/selected_script.json`, `results/selected_voiceover.json`,
`results/tts`, `results/voice_review.json`, `results/render`,
`results/render_review.json`, `results/automation.json` and `progress` —
the same `REWIND_PATHS["analysis"]` set as before. A rerun *overwrites*
`hot_moments` in place; downstream artifacts are not deleted eagerly —
they become stale through the content-hash binding described above.

## Mode and provider notes

- `direct_clip_series` and `narrated_theory` both allow `video_analysis`
  (the gate's mode policy is unchanged). No script/TTS prerequisite was
  added to either path.
- The analysis path makes **no provider call** — FFmpeg, faster-whisper,
  PySceneDetect and numpy-style audio math only. Whisper models load from
  the local Hugging Face cache (`tiny`, `small` present on this machine);
  the first download, if a model is absent, is the only network touch and
  happens inside faster-whisper, unchanged.

## Performance notes

- Nothing new reads media into RAM: clip extraction is a bounded FFmpeg
  slice; the full-source scan extracts *audio only* for the energy pass
  and deletes the WAV in `finally`; region analysis reuses the same
  bounded clip machinery.
- The refactor adds one Python delegation hop — no extra process, pass or
  file I/O. Real-media smoke: 6-second fixture, `whisper_model_size=
  "tiny"`, 2 moments + 2 previews in ~4.4 s, matching the pre-extraction
  timing (`verify_console.py` exercises the same path).

## Known limitations (unchanged)

- `window` scope without `focus_region` defaults to the first 240 s —
  unchanged; `full_source` is the mode for genuinely long sources.
- Synthetic/silent audio produces `transcript_available=False` and
  energy+scene-only scoring — by design, not a failure.
- `source_assessment` is the only duration authority for `full_source`
  and `focus_region`; a missing assessment raises before any work
  (window scope without `focus_region` needs no duration and proceeds).
