# MomentService

> Introduced in Milestone 11 — the extraction that gives `moment_selection`
> and the per-slot `clip_bounds` documents an application-level owner. The
> Phase 2 orchestration (gate → rank ownership → canonical builder → artifact)
> moved out of `PipelineService` unchanged; every algorithm stayed canonical
> in `local_pipeline`.

## Why this boundary exists

Selection was the first consumer of `hot_moments` and the first per-slot
artifact (`results/render/output_NN/moment_selection.json` for slots 2+).
Until this milestone the only thing keeping "one rank, one Short" and "a
stale analysis can't validate an old pick" honest was a private method on a
3,300-line façade. `MomentService` is now the single application writer of
`moment_selection` and the owner of the clip-bounds read/write surface that
the editor panel and the render prerequisite consume.

## Responsibilities

| Method | Responsibility |
|---|---|
| `record_moment_selection(*, selected_rank, reviewed_by, rejected_ranks=None, notes="", require_preview=True, slot=1)` | the ONLY application write path for `moment_selection`: `require_phase` + `require_slot` gates, rank-conflict rejection, canonical `build_moment_selection` call, keyed artifact write under the reviewer's name |
| `moment_rank_owner(rank, slot)` | which OTHER slot owns `rank`, or None — scans every plan-declared slot |
| `selected_moment(slot=1)` | the hot_moments entry the selection points at; `{}` on missing/corrupt artifacts — never guessed |
| `clip_bounds_relative(slot=1)` | `results/clip_bounds/output_NN.json` — gated by `require_slot` |
| `clip_bounds(slot=1)` | effective bounds dict: persisted override when `edited`, else the moment's `source_start/end_sec` |
| `clip_bounds_override(slot=1)` | `{source_start_sec, source_end_sec}` or `None` — what `_run_render` consumes |
| `clip_bounds_view(slot=1)` | editor-panel state: availability, editability, duration, `min_duration_sec`, `transcript_will_be_invalidated`; never breaks the page on a missing plan |
| `save_clip_bounds(*, start, end, actor, slot=1)` | validate → unchanged short-circuit → archive slot render (+ transcript in direct mode) → write `clip_bounds` doc → `clip_bounds.saved` audit |
| `reset_clip_bounds(*, actor, slot=1)` | snap back to the moment's range — implemented as a `save_clip_bounds` to the moment bounds, so invalidation and audit are identical |
| `source_duration_sec()` | `SourceQuery.get_duration()` pass-through (kept on the service because `save_clip_bounds`/`clip_bounds_view` need it internally) |

### What deliberately stayed outside

- **`hot_moments` production** — `VideoAnalysisService` remains the sole
  writer. MomentService only READS it through `ports.ArtifactRepository`.
- **Canonical algorithms** — `build_moment_selection`,
  `validate_moment_selection`, `validate_hot_moments_schema`,
  `hot_moments_content_sha256` live in `local_pipeline.video_analysis`;
  `EDLClip`/`set_clip_source_bounds`/`MIN_CLIP_DURATION_SEC` live in
  `local_pipeline.scene_editor`. The service calls them; it reimplements none.
- **Approval** — a selection records `human_review` facts; it never grants an
  approval row and never makes anything publishable (`ApprovalService` owns
  that).
- **Revision mechanics** — the `.revisions/` snapshot code moved to
  `LocalRevisionArchive` (`ports.RevisionArchiver`), an infrastructure
  adapter, because rewind, render-profile edits and review flows share it.
  The service receives the PORT, not a façade callable.

## Dependencies (all injected)

| Constructor arg | Port / type | Used for |
|---|---|---|
| `artifacts` | `ports.ArtifactRepository` (+ `write_artifact_path`) | `moment_selection`/`hot_moments`/`processing_plan`/`project` reads; keyed `moment_selection` write; path-level `clip_bounds` write |
| `source` | `ports.SourceQuery` | `get_source_path()` for the validation `EDLClip` and the view's editability check; `get_duration()` for the upper bound |
| `workflow` | `ports.WorkflowGateQuery` | `require_phase("moment_selection")`, `require_slot`, `slot_indexes` (rank-owner scan), `slot_moment_rank` |
| `archiver` | `ports.RevisionArchiver` | `render_slot_archive_paths` + `archive_revision` for the bounds-edit invalidation snapshot |
| `audit` | `ports.AuditSink` | `clip_bounds.saved` events |

No progress factory, no job handle, no secrets resolver: selection is a
synchronous call with no provider interaction, exactly as before.

## Artifact authority

| Artifact | Writer | Reader |
|---|---|---|
| `hot_moments` (slot-less) | VideoAnalysisService ONLY | MomentService (read), script/render gates, panels |
| `moment_selection` (per-slot) | `MomentService.record_moment_selection` ONLY | `selected_moment`, rank scan, approval chain, script/render prerequisites, outputs/batch board |
| `clip_bounds/output_NN.json` | `MomentService.save_clip_bounds` ONLY | `clip_bounds*`, `_run_render` via `clip_bounds_override` |
| `.revisions/<id>` snapshots | `LocalRevisionArchive` (shared) | `revisions()`, restore flows |

The audit found exactly one `write_artifact("moment_selection")` call site in
the old façade; a guard test (`test_moment_selection_has_a_single_writer`)
keeps it that way.

## Rank ownership and slot isolation

- `moment_rank_owner` iterates `WorkflowGateQuery.slot_indexes()` (the PLAN's
  slots, not the filesystem's) and skips the caller's own slot — reselecting
  in the same slot is a legal "change my mind", selecting a rank another slot
  holds raises `PipelineServiceError("rank N zaten M. cikti yuvasi icin
  secilmis; her Short farkli bir moment kullanmali")`.
- `clip_bounds` files are per-slot (`output_NN.json`) and never in the
  artifact catalog — the `EDL` is project-wide, so slot bounds could never
  live there.
- `save_clip_bounds` archives ONLY this slot's render output:
  `render_slot_archive_paths` enumerates `results/render/output_NN/` children
  for slots 2+ and excludes sibling `output_*` directories when archiving
  slot 1. The moment selection and (outside direct mode) the transcript are
  preserved inside an otherwise-archived slot directory.

## Digest binding, staleness and invalidation

`build_moment_selection` embeds `hot_moments_content_sha256` and the selected
preview's byte hash. `validate_moment_selection` (used by script generation
and downstream gates) reports `hot_moments.json degisti` when a re-analysis
changes the manifest — the old pick can never be silently re-validated.
`selected_moment`/`clip_bounds*` treat missing or corrupt artifacts as `{}`/not
edited rather than fabricating values.

A bounds save archives the slot's rendered output under action
`clip_bounds_edited`/`target="render"`; in `original_audio_subtitles` mode it
additionally archives `moment_transcript` (dropped from `preserve`, appended
via `artifact_relative` so the slot-2+ path is correct) because the approved
caption text no longer matches the spoken range. `reset_clip_bounds`
invalidates identically — it IS a bounds change. (Milestone 12 note: this
invalidation decision still lives HERE — `TranscriptService` owns transcript
writes, MomentService owns the bounds-edit archive; the two never import
each other.)

## Error behavior (unchanged)

- Unknown rank / schema-invalid hot_moments → `ValueError` from the canonical
  builder (`bulunamadi`, `semasi gecersiz`).
- Missing hot_moments → `ArtifactProblem`; unapproved plan →
  `PipelineServiceError` from `require_phase`; rank conflict →
  `PipelineServiceError`.
- Bounds validation → `scene_editor` `ValueError` re-raised as
  `PipelineServiceError` with the same Turkish messages
  (`asamaz`, `buyuk olmali`, `en az`, `negatif olamaz`).
- No selected moment → `PipelineServiceError("Once bir moment secilmeli…")`.
- Failed writes propagate (`OSError`/`PermissionError`) — no success result,
  no `clip_bounds.saved`/`artifact.written` event.

## Job / progress behavior

Moment selection is synchronous: no job type, no progress record, no
cancellation path existed and none was added. `record_moment_selection` is
dispatched directly from the API layer, not through `JOB_EXECUTORS`.

## Fault injection

The `clip_bounds` write moved from a façade-local `atomic_write_json` call to
`LocalArtifactRepository.write_artifact_path` — the same module the injector
already patches. `LocalRevisionArchive` binds `atomic_write_json` at module
level, so `infrastructure.persistence.local.revision_archive` was registered
in `creator_console.faults._ATOMIC_WRITE_HOLDERS`. Negative tests prove a
disk-full write yields no artifact, no success result and no audit event.

## Known limitations

- `selected_moment` reports `{}` on corrupt input rather than surfacing the
  `problem` string to the API — preserved façade behavior, not redesigned.
- `moment_rank_owner` reads each sibling's artifact on every call — O(slots);
  fine at current slot counts, worth revisiting only if plans grow large.
- `clip_bounds` remains outside the artifact catalog, so `load_artifact`
  required-field validation does not apply to it — preserved behavior.

## Test coverage

`tests/test_moment_facade.py`: 31 characterization tests pinned BEFORE the
extraction (selection happy path, missing/corrupt/schema-invalid hot_moments,
unknown rank, gate denial, rank ownership/conflict/release, per-slot
isolation, read purity, stale binding, all clip-bounds cases incl. direct vs
narrated transcript invalidation and unknown-duration), 4 façade-parity
classes comparing a standalone `MomentService` to the façade, and explicit
negatives (failed writes report no success, no downstream artifacts/approval
granted, service runs without `PipelineService`). Architecture guards cover
the port wiring, the single-writer rule and the fault-holder registry.
