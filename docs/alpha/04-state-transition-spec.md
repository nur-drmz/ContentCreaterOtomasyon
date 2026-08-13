# 04 — Project State Transition Specification

**Formalizes:** `the plan.txt` §22 (Full Project State Machine), §15 (Publication states referenced in Stage 15).
**Status:** Draft
**Approval needed from:** Eng, Security

---

## 1. States

Verbatim from §22 (35 states): `DRAFT, CONCEPT_APPROVED, UPLOAD_PENDING, UPLOADING, SOURCE_UPLOADED, SOURCE_VALIDATED, ANALYSIS_QUEUED, ANALYZING, ANALYZED, MOMENTS_REVIEW, MOMENTS_SELECTED, PROMPT_DRAFT, PROMPT_APPROVED, ROUGH_CUT_GENERATING, ROUGH_CUT_READY, ROUGH_CUT_APPROVED, SCRIPT_DRAFT, SCRIPT_APPROVED, VOICE_GENERATING, VOICE_READY, VOICE_APPROVED, EDITING, EDIT_APPROVED, RENDERING, RENDERED, FINAL_REVIEW, FINAL_APPROVED, UPLOAD_TO_YOUTUBE_QUEUED, UPLOADED_PRIVATE, PUBLISH_APPROVAL_PENDING, PUBLISH_APPROVED, PUBLIC, ARCHIVED, FAILED, CANCELLED`.

## 2. Transition table

| From | To | Trigger | Required permission | Guard |
|---|---|---|---|---|
| DRAFT | CONCEPT_APPROVED | User approves channel/concept/prompt-profile selection | `project.create` (implicit on creator) | Channel + concept must exist and be active |
| CONCEPT_APPROVED | UPLOAD_PENDING | User starts upload | `media.upload` | Rights declaration present |
| UPLOAD_PENDING | UPLOADING | Upload session opened | `media.upload` | Valid signed upload session, not expired |
| UPLOADING | SOURCE_UPLOADED | Upload completes | system | All chunks received, hash computed |
| SOURCE_UPLOADED | SOURCE_VALIDATED | Validation job passes | system | Format supported (MP4/MKV/MOV), not corrupt, not duplicate-without-consent |
| SOURCE_UPLOADED | FAILED | Validation job fails | system | Corruption/format/duplicate-block detected |
| SOURCE_VALIDATED | ANALYSIS_QUEUED | Auto-enqueue | system | — |
| ANALYSIS_QUEUED | ANALYZING | Worker picks up job | system | `analysis.start` was implicitly granted at queue time |
| ANALYZING | ANALYZED | All Stage-4 sub-jobs complete | system | Every required artifact (proxy, transcript, scenes, features) present |
| ANALYZING | FAILED | Any required sub-job exhausts retries | system | Failure reason recorded |
| ANALYZED | MOMENTS_REVIEW | Hot Moment ranking completes | system | 20–40 moments produced (or documented exception) |
| MOMENTS_REVIEW | MOMENTS_SELECTED | User confirms selection | `moment.select` | At least 1 moment selected |
| MOMENTS_SELECTED | PROMPT_DRAFT | User enters Prompt Studio | `prompt.create` | — |
| PROMPT_DRAFT | PROMPT_APPROVED | User approves prompt | `prompt.create` | Prompt resolved to a supported content mode |
| PROMPT_APPROVED | ROUGH_CUT_GENERATING | Auto-enqueue | system | — |
| ROUGH_CUT_GENERATING | ROUGH_CUT_READY | Rough cut job completes | system | Timeline JSON valid against schema |
| ROUGH_CUT_READY | ROUGH_CUT_APPROVED | User approves | `rough_cut.approve` | — |
| ROUGH_CUT_APPROVED | SCRIPT_DRAFT | Auto-enqueue script generation | system | — |
| SCRIPT_DRAFT | SCRIPT_APPROVED | User approves a script variant | `script.approve` | Theory Guard has no unresolved blocking warnings, or user explicitly overrides with recorded justification |
| SCRIPT_APPROVED | VOICE_GENERATING | Auto-enqueue | system | — |
| VOICE_GENERATING | VOICE_READY | TTS job completes | system | All segments generated |
| VOICE_READY | VOICE_APPROVED | User approves | `voice.approve` | — |
| VOICE_APPROVED | EDITING | User opens timeline editor | `video.edit` | — |
| EDITING | EDIT_APPROVED | User approves edit | `video.approve` | — |
| EDIT_APPROVED | RENDERING | Auto-enqueue | system | — |
| RENDERING | RENDERED | Render job completes | system | Output file passes integrity check |
| RENDERED | FINAL_REVIEW | Auto transition | system | Viral score + view forecast computed |
| FINAL_REVIEW | FINAL_APPROVED | User approves final video | `video.approve` | — |
| FINAL_APPROVED | UPLOAD_TO_YOUTUBE_QUEUED | Auto-enqueue | system | Channel OAuth token valid |
| UPLOAD_TO_YOUTUBE_QUEUED | UPLOADED_PRIVATE | YouTube upload job completes (as private/unlisted) | system | YouTube returns success + video ID |
| UPLOADED_PRIVATE | PUBLISH_APPROVAL_PENDING | User requests public review | `publication.approve_public` (to request) | Metadata (title/description/thumbnail) complete |
| PUBLISH_APPROVAL_PENDING | PUBLISH_APPROVED | Authorized approver confirms | `publication.approve_public` | Explicit confirmation UI action recorded (§3.1) |
| PUBLISH_APPROVED | PUBLIC | Publish job executes | `publication.publish` | Distinct from approval action — two calls, not one (see note below) |
| Any non-terminal state | CANCELLED | User cancels | role-appropriate `*.cancel`/owning role | — |
| Any state | ARCHIVED | User archives after completion or cancellation | `project.archive` | Only from a terminal-ish state (`PUBLIC`, `CANCELLED`, `FAILED`) |
| Any queued/running state | FAILED | Job exhausts retries | system | Failure reason recorded, retry path offered where applicable |

**Critical design rule (§3.1, §32.2):** `PUBLISH_APPROVED → PUBLIC` must be a separate, explicitly confirmed action from `UPLOADED_PRIVATE → PUBLISH_APPROVAL_PENDING`. No single user action may traverse both `PUBLISH_APPROVAL_PENDING → PUBLISH_APPROVED → PUBLIC` atomically; the spec requires the approval action and the publish action to be independently invokable and independently audited, even if UX chooses to present them as one confirmation dialog with two distinct recorded events.

## 3. Server-side validation rule

All transitions must be validated server-side against this table (§22, closing line). No client is trusted to enforce ordering. Every transition attempt — allowed or rejected — is a candidate audit event (see `13-security-threat-model.md`).

## 4. Re-entrancy / regeneration

Several MVP acceptance criteria (US-8, US-9, US-10 in `02-user-stories-acceptance.md`) require partial regeneration without a full state rollback:
- Editing a locked-free rough-cut section while in `ROUGH_CUT_READY`/`ROUGH_CUT_APPROVED` re-enters `ROUGH_CUT_GENERATING` for the affected sections only, then returns to the prior approved state, requiring re-approval only if content the user had approved actually changed.
- Same pattern applies to `SCRIPT_*` (single-sentence regeneration) and `VOICE_*` (single-segment regeneration).
- This "partial regeneration" behavior is modeled as a sub-state or dirty-flag on the timeline/script/voice artifact, not as new top-level states — exact modeling is an implementation detail for `05-domain-model.md` / `06-database-schema.md`.

## 5. Open decisions

- Whether `CANCELLED` projects can be resumed (currently modeled as terminal, requiring a new project) — not specified in source doc, flagged for Product.
- Exact retry-count/timeout thresholds before `FAILED` — depends on job runtime, deferred to `08-job-queue-spec.md`.

## Approval needed from
Eng (transition correctness), Security (publish-gate separation).
