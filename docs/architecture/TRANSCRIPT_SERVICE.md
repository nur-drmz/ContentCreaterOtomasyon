# TranscriptService

> Introduced in Milestone 12 — the extraction that gives `moment_transcript`
> an application-level owner. Generation (the local-ASR orchestration), the
> caption-screen read surface and the whole edit lifecycle moved out of
> `PipelineService` unchanged; every algorithm stayed canonical in
> `local_pipeline`.

## Why this boundary exists

`moment_transcript` is the artifact a human reviewer actually fixes: five
write paths (generation, segment/full-text edit, exclusion, corrections,
restore-from-ASR), a revision history, a raw-ASR snapshot, glossary
suggestions and a per-slot path scheme all lived as private methods on the
façade. TranscriptService is now the single application writer of
`moment_transcript` — the approval hash that the render gate checks can only
be changed through this boundary.

## Responsibilities

| Method | Responsibility |
|---|---|
| `run_transcript(*, subtitle_whisper_model_size="small", subtitle_language="auto", caption_theme="auto", job_id=None, actor="console", slot=1)` | the ONLY generation path: `require_phase("transcript")` + `require_slot` gates, hot_moments + moment_selection trust check, canonical `render_utils.run_moment_transcription` call, correction carry-over, replaced-manual-edit archiving, `transcript` progress lifecycle (start/note/finish/fail + cancellation) |
| `transcript_view(slot=1)` | caption-screen projection: availability, problem, phase eligibility, approval state, segments, full text, limits, glossary + suggestions, up-to-2000 timed words |
| `transcript_segments(slot=1)` | one editable row per caption line (`build_segments` at the canonical low-confidence threshold); raises `ArtifactProblem` when the transcript is missing/corrupt |
| `save_transcript_edits(*, segments=None, full_text=None, edited_by, slot=1)` | exactly-one-of validation → `validate_segment_texts` → `apply_segment_texts` → unchanged-no-op short-circuit → rebuild + `append_revision` → write → detailed `artifact.written` → glossary suggestions |
| `set_transcript_exclusions(*, segment_ids, excluded, actor, slot=1)` | keep reviewable, drop from captions (`subtitle_exclusion`/`subtitle_inclusion` revisions) |
| `restore_transcript_from_asr(*, actor, slot=1)` | put the raw recogniser output back (`restore_asr` revision); refuses when no `asr` snapshot exists |
| `set_transcript_corrections(*, corrections, edited_by, slot=1)` | replace the word-correction list and re-derive text from the ORIGINAL `asr` snapshot (80-char/100-item limits); returns the rebuilt artifact, not the view |
| `transcript_view` internals | `plan_gate` + `ApprovalQuery.state("transcript")` + `ensure_v2` read-upgrade |

### What deliberately stayed outside

- **`moment_selection` ownership** — `MomentService` remains the sole writer.
  TranscriptService only READS it (trust check in `run_transcript`,
  `moment_rank` display in `transcript_view`).
- **`hot_moments` production** — `VideoAnalysisService` remains the sole
  writer; the transcript path only validates a selection against it through
  the canonical `validate_moment_selection` inside the engine.
- **Canonical algorithms** — `run_moment_transcription` /
  `build_moment_transcript_artifact` / `LOW_CONFIDENCE_WORD_THRESHOLD` live in
  `local_pipeline.render_utils`; `ensure_v2`/`build_segments`/
  `apply_segment_texts`/`apply_segment_exclusions`/`parse_full_text`/
  `append_revision`/`content_sha256`/`revision_summary` live in
  `local_pipeline.transcript_edit`; suggestion scoring lives in
  `local_pipeline.glossary`. The service reimplements none.
- **Approval lifecycle** — `ApprovalService` owns grants/invalidation. An
  edit drops approval by changing the digest the grant binds to; the service
  never writes approval state and never calls the invalidator itself.
- **Revision mechanics** — `ports.RevisionArchiver` (the shared
  `.revisions/` snapshot machinery) performs the replaced-transcript archive.
- **Glossary management API** — `glossary()`/`glossary_view()`/
  `set_glossary_terms`/`accept_glossary_suggestion` stay on the façade; the
  service only reads the glossary artifact and appends suggestion proposals.

## Dependencies (all injected)

| Constructor arg | Port / type | Used for |
|---|---|---|
| `artifacts` | `ports.ArtifactRepository` | `moment_transcript` reads/writes, `hot_moments`/`moment_selection` trust reads, shared `glossary` read + suggestion write |
| `source` | `ports.SourceQuery` | `get_source_path()` — the engine transcribes the selected interval of the project's ONE source |
| `workflow` | `ports.WorkflowGateQuery` | `require_phase("transcript")`, `require_slot`, `plan_gate` for the view's eligibility flag |
| `approvals` | `ports.ApprovalQuery` (extended with `state`) | the live approval state in `transcript_view` — read-only |
| `archiver` | `ports.RevisionArchiver` | `archive_revision` for the re-run replacement snapshot |
| `progress` | `ports.ProgressFactory` (extended `ProgressTracker.note`) | the `transcript` activity tracker; cancellation via the bound cancel check inside `stage`/`finish` |
| `namespace`, `ownership` | `ports.ProjectNamespaceStore` + `Ownership` | the ASR engine's `.runtime/` scratch dir and the empty-glossary ownership stamp |
| `audit` | `ports.AuditSink` | the detail-bearing `artifact.written` events and `transcript.manual_edits_archived` |

## Workflow modes and eligibility

`require_phase("transcript")` enforces the plan's gate: in
`direct_clip_series` the caption step is legal once the plan is approved and
a moment is selected; in `narrated_theory` the phase is denied outright (the
narration is generated, not recognised). `transcript_view` reports
`phase_allowed`/`phase_reason` via `plan_gate` without raising — a reviewer
must be able to LOOK at a transcript even when re-running it is not allowed.

## Generation pipeline

1. `require_phase("transcript")`, `require_slot(slot)` — gate before work.
2. `tracker.start("Transkript uretimi basladi")`.
3. Load `hot_moments` + per-slot `moment_selection`, both `required=True`;
   any `problem` becomes `ArtifactProblem` — nothing is fabricated.
4. Load the previous transcript for correction carry-over.
5. Resolve `.runtime/` through the namespace store; call the canonical
   engine — selected-interval audio extraction + local faster-whisper, no
   provider.
6. Re-apply surviving word-level `corrections` onto the untouched
   recognition (`_apply_transcript_corrections` rebuilds from `asr`,
   preserving revisions/edited-segment-ids/glossary context).
7. If the previous artifact carries `edited_segment_ids` or `revisions`,
   archive it via `RevisionArchiver` (`action="retranscribe"`,
   `target="transcript_slot_N"`) and annotate the new artifact with
   `replaced_manual_edit` (revision id, archive path, previous sha).
8. `write_artifact("moment_transcript")` — only THEN the
   `transcript.manual_edits_archived` event fires: the archive MOVE plus a
   failed write must never leave the slot empty-looking.
9. `tracker.finish` (word count or "konusma yok"); any exception →
   `tracker.fail(f"{type}: {exc}")` and re-raise — cancellation included.

## Editing pipeline

All edits share one shape: load + `ensure_v2` → canonical transform →
`build_moment_transcript_artifact` (carrying `source`, `language`,
`moment_rank`, `moment_selection_sha256`, `whisper_model_size`,
`initial_prompt_used`, `corrections`, `asr`, `revisions`,
`edited_segment_ids`, `glossary_context`) → `append_revision` → write →
audit (identifiers and counts only — transcript text never enters the audit
log) → `transcript_view`.

- **Unchanged input is a no-op**: `save_transcript_edits` and
  `set_transcript_exclusions` return the view without writing — a revision
  would claim an edit that did not happen and would drop a valid approval.
- **Corrections re-derive from raw ASR**: removing a correction really
  removes it; the `asr` snapshot is never overwritten, which is what makes
  `restore_transcript_from_asr` possible.
- **Glossary teaching**: word-for-word segment fixes feed
  `suggest_terms_from_correction`; adjacent changed words collapse into one
  multi-word proposal (`_contiguous_runs`).

## Selection binding, staleness and clip-bounds invalidation

The artifact embeds `moment_selection_sha256`; the engine validates the
selection against `hot_moments` (`validate_moment_selection`), so a stale
pick fails loudly instead of producing a "valid" transcript. Approval hashes
cover the words, so any edit invalidates by construction.

A `clip_bounds` change (`MomentService.save_clip_bounds`) archives the
slot's render output AND — in `original_audio_subtitles` mode — the
`moment_transcript` inside the archived slot directory, because the approved
caption text no longer matches the spoken range. That contract lives in
MomentService's `render_slot_archive_paths(preserve=...)` call and was NOT
moved; TranscriptService owns transcript writes, MomentService owns the
bounds-edit invalidation decision.

## Approval and downstream behaviour

- Generation produces `status: "review_required"` — never approved.
- Every write goes through `ArtifactRepository.write_artifact` (which emits
  `artifact.written`) plus, for edits, a second detail-bearing event — the
  pre-existing double-event shape is preserved verbatim.
- `transcript_view["approval"]` is `ApprovalService.state("transcript",
  slot)`, and the render gate's chain validity keeps flowing through
  `ApprovalService.chain` — untouched.
- A transcript operation never emits `provider.call`/`job.submitted`/
  `approval.granted` and never produces `selected_script`,
  `selected_voiceover`, `render_manifest` or `publish_intent`.

## Progress, jobs and cancellation

`run_transcript` is dispatched as `job_type: "transcript"`; the tracker is
`ProgressFactory.create("transcript", job_id=...)`, so cancellation is the
job runtime's bound signal — `stage`/`finish` raise `OperationCancelled` and
the `except` arm records `failed` before re-raising. The engine receives the
same tracker and calls `count()` for extraction progress.

## Error behavior (unchanged)

- Missing `moment_selection`/`hot_moments` or artifact problems →
  `ArtifactProblem`; disallowed phase/slot → `PipelineServiceError` from the
  gate.
- Edit validation → `PipelineServiceError` with the canonical Turkish
  messages (`ikisi birden degil`, `segment id'si eksik`, 80-char/100-item
  limits); `TranscriptEditError` from exclusions is re-raised as
  `PipelineServiceError`.
- ASR/extraction failures propagate (`RuntimeError`, `OperationCancelled`),
  the tracker is failed and nothing is written.
- Write failures propagate (`OSError`/`PermissionError`/
  `InterruptedWrite`): no success result, no `artifact.written`, no
  approval, progress `failed`; a partial write is rejected by the reader,
  never parsed.

## Fault injection

No new module-level `atomic_write_json` binding was introduced — every write
goes through `LocalArtifactRepository` (already an `_ATOMIC_WRITE_HOLDERS`
entry) and the replacement archive through `LocalRevisionArchive` (registered
in Milestone 11). Tests cover ENOSPC, permission denied and a 48-byte partial
write on the generation path, plus ENOSPC on the edit path.

## Known limitations

- `transcript_view` reads the glossary artifact on every call (unchanged
  behaviour — suggestions must reflect terms accepted since the last edit).
- `transcript_segments` does not `require_slot` — an out-of-plan slot simply
  reports `ArtifactProblem` on the missing artifact (unchanged).
- `run_transcript` keeps `subtitle_whisper_model_size="small"` as the
  default; model upgrades are out of scope by milestone rule.
- A re-run archives the hand-edited transcript but does not replay segment
  edits onto new timings — documented in the `replaced_manual_edit` note.
