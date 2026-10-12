# ApprovalService

> Extracted in Milestone 5. `application/services/approval_service.py` owns the
> content-approval lifecycle; `local_pipeline/approval.py` remains the canonical
> deterministic engine for digests, review descriptions and approval-state
> validation. The façade methods are thin delegates — every existing caller,
> API route and notebook keeps working unchanged.

## What it owns

| Responsibility | Method | Notes |
|---|---|---|
| Granting | `approve(kind, *, reviewed_sha256, approved_by, notes, slot)` | Hash-bound gate + rights/checklist preconditions; `approval.granted` / `approval.rejected` audit |
| Live state | `state(kind, slot)` | Re-derived from current content every call — never cached |
| Dependency-aware validity | `chain(slot)` | Per-artifact validity **plus** upstream problems (plan → moment_selection → script/voiceover or transcript → render), including render↔transcript drift |
| Stale sweep | `invalidated()` | Reports granted-but-no-longer-binding approvals across all slots; deletes nothing |
| Invalidation recording | `record_invalidation(kind, *, artifact, actor, reason, previous_approval, slot)` | The `ports.ApprovalInvalidator` contract; emits `approval.invalidated`, writes no artifact |

## What deliberately stayed outside

- **Digest/canonicalization/review description/approval-state math** —
  `local_pipeline.approval` (`content_hash_for`, `describe_for_review`,
  `approve_artifact`, `check_approval`). The service never re-implements a
  hash.
- **Checklist mutation** — `set_*_review_item`, `request_*_revision`,
  `clear_*_revision` remain façade methods. They reuse the review gate's
  `load_or_new_*` helpers so "bound to the current digest" has one definition.
- **Publication approvals** — `approve_publication_metadata`,
  `approve_publish_intent`, `approve_publish_visibility` are separate state
  machines (`metadata_approval`, publish intent/visibility) owned by
  `PublishService` since Milestone 6 (`PUBLISH_SERVICE.md`). They are *not* in
  `APPROVAL_ARTIFACTS` and cannot be reached through the content-approval
  route (Invariant F).
- **Phase gates** — `plan_gate`/`require_phase` belong to `WorkflowGate`
  (Milestone 9); they are workflow orchestration consumed by
  render/script/TTS, not approval
  lifecycle.
- **Slot validation arithmetic** — moved to neutral `application/slot_policy.py`
  (pure functions over a loaded plan), shared by the façade and the service so
  "how many Shorts does this plan define" has one answer.

## Supported approval kinds

`application/approval_catalog.py` is the single registry (the façade re-exports
it as `APPROVAL_ARTIFACTS`):

| Kind | Artifact key | Scope | Extra precondition |
|---|---|---|---|
| `processing_plan` | `processing_plan` | project | **rights**: copyright checklist complete, no open revision |
| `script` | `selected_script` | project | hash gate only |
| `voiceover` | `selected_voiceover` | project | voice checklist complete, no open revision |
| `transcript` | `moment_transcript` | per-slot | hash gate only |
| `render` | `render_manifest` | per-slot | render checklist complete, no open revision |

Preconditions **add to** the canonical hash gate; they never replace it.

## SHA-256 binding (Invariant B — unchanged)

`local_pipeline/approval.py` decides which fields participate:

- `processing_plan` → canonical JSON hash of the `decision` block
- `script` → SHA-256 of `script_text`
- `transcript` → canonical JSON hash of words/cues incl. timings + exclusion flags
- `voiceover` → SHA-256 of the audio file bytes
- `render` → SHA-256 of the final video file bytes

The caller echoes a digest; `approve_artifact` re-derives it from current
content. A wrong or stale echo is refused with `approval.rejected` recorded and
nothing written.

## Invalidation (Invariant C)

Two complementary mechanisms, both preserved:

1. **Content-bound invalidation** — `state()`/`chain()` recompute validity from
   current content, so an edit that changes the digest invalidates the grant
   *without anyone deleting anything*. The `human_approval` block and the
   `approval.granted` audit event stay: "no longer binds" ≠ "never happened".
2. **Recorded invalidation** — when a service performs an edit that drops a
   live approval (today: `AnalysisService.update_processing_plan`), the
   approval owner emits `approval.invalidated` via `record_invalidation`. The
   artifact reset itself is a pure transformation performed by the edit
   (`build_processing_plan` rebuilds the approval block), persisted by the
   **one** write the edit already performs — the port contract forbids a
   second write, and no event is recorded when the previous state was
   unapproved (exactly-once).

`chain()` additionally marks downstream approvals `effective_valid=False` when
an upstream approval broke or a `moment_selection` went stale, and flags
render↔transcript drift when the manifest's recorded transcript digest no
longer matches the current transcript.

### Invalidation triggers found in the repository

| Mutation path | Mechanism |
|---|---|
| `update_processing_plan` | artifact rebuild (approval reset) + `approval.invalidated` event via `ApprovalInvalidator` |
| any artifact write that changes a digest (script text edit, transcript correction, re-render, new voiceover) | content-bound: `check_approval` no longer validates; downstream `chain()` marks `upstream_invalid` |
| moment selection change / hot-moments regeneration | `chain()` validates selection against `hot_moments` → `upstream_invalid` |
| transcript edited after render | render↔transcript drift check in `chain()` |
| stage rewind | `REWIND_PATHS` archives downstream artifacts + checklists (physical removal, unchanged) |
| source replacement | source fingerprint changes → copyright checklist no longer binds → plan approval precondition fails |

## Rights enforcement (Invariant E)

`processing_plan` approval requires `copyright_review_state()["complete"]` and
no `open_revision`, consulted through `ports.ReviewGate` — an adapter
(`infrastructure/review/local/review_gate.py`) that reads the stored checklist
and binds it to the **source fingerprint** via `ports.SourceQuery
.get_fingerprint()`. The gate can only report what the checklist says; it can
never manufacture a positive answer. Render/voiceover approvals similarly
require their per-content checklists, bound to the video/audio digests.

## Human vs machine approval (Invariant A)

Nothing in `ApprovalService` is reachable from a generation path. `approve()`
requires the caller to echo the digest shown in review output — a machine
cannot "successfully generate" its way to a grant, and no parameter exists to
bypass the check.

## Reviewer policy (future — not implemented)

ADR-006 defines `solo_allowed` / `distinct_reviewer` / `role_reviewer`. **This
milestone preserves local solo behaviour only**: `approved_by` is recorded
verbatim and is *not* an authenticated identity — no maker-checker policy is
enforced on spoofable strings. When server-side auth lands, `approve()` is the
single seam where a trusted actor context replaces the request-supplied
string, and the workspace policy lookup plugs in before the write — the call
shape does not change.

## Content approval vs publication approval (Invariant F)

Approving content never authorizes an upload. The publication family
(`publication_metadata` → `publish_intent` → `publish_manifest`) keeps its own
digests and state machines inside `PublishService` (Milestone 6);
`approve()` refuses publication kinds as unknown. `PublishService` consults
this service read-only through `ports.ApprovalQuery` (`chain()`), the single
edge from publication back into content approval — content approval is
necessary for an intent, never sufficient.

## API and persistence boundaries

- Public façade surface is unchanged: `approve`, `approval_state`,
  `approval_chain`, `invalidated_approvals`, `render_video_digest`,
  `render_review_state`, `voice_audio_digest`, `voice_review_state`,
  `source_fingerprint`, `copyright_review_state`, `render_review_relative` are
  delegates; checklist mutation methods unchanged.
- Persistence stays through `ports.ArtifactRepository` (one write per grant,
  `artifact.written` then `approval.granted` — durable before the event).
- Errors are unchanged: `PipelineServiceError` for gate/precondition refusals,
  `ArtifactProblem` for missing/corrupt artifacts.
- `AuditLog` ordering is unchanged: `artifact.written` → `approval.granted`,
  and on refusal `approval.rejected` with the reason.

## Audit behavior

| Event | When | Detail |
|---|---|---|
| `approval.granted` | hash gate + preconditions pass, artifact persisted | `kind`, `slot`, `approved_content_sha256` |
| `approval.rejected` | `approve_artifact` refuses the digest | `kind`, `reason`, `slot` — also used by revision *requests* (a note, not a grant attempt) |
| `approval.invalidated` | an edit dropped a live approval | `kind`, `reason`, `previous_approved_content_sha256` (+ `slot` for per-slot kinds) |

## Dependency wiring

```
ConsoleApp ──► PipelineService ──┬──► ApprovalService ──┬─► ports.ArtifactRepository
                                 │                      ├─► ports.AuditSink
                                 │                      ├─► ports.ReviewGate ─► LocalReviewGate ─► creator_console.{copyright,render,voice}_review
                                 │                      ├─► application.{approval_catalog,slot_policy,artifact_catalog}
                                 │                      └─► local_pipeline.{approval,video_analysis,render_utils,source_strategy}
                                 ├──► AnalysisService ──► ports.ApprovalInvalidator (= ApprovalService)
                                 ├──► SourceService ────► ports.SourceQuery (consumed by LocalReviewGate for the fingerprint)
                                 └──► EditorService
```

`AnalysisService` never imports `ApprovalService`: it depends on the narrow
`ApprovalInvalidator` port (one method — `record_invalidation`), which
`ApprovalService` satisfies structurally. No sibling import, no cycle.

## Fault-injection note

`LocalReviewGate` binds **no** write function (reads + builds review records
only). Approval writes go through `LocalArtifactRepository` and checklist
writes through the façade `_save_*` methods — both already in
`faults._ATOMIC_WRITE_HOLDERS`, so no registry entry was needed.
`TestApprovalWriteFaultCoverage` proves ENOSPC/permission/partial-write faults
still reach approval persistence and that a crashed write is never reported as
granted.
