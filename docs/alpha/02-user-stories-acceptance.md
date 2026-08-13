# 02 — User Stories and Acceptance Criteria

**Formalizes:** `the plan.txt` §6 (Main User Workflow, Stages 1–16), §23 (MVP Scope).
**Status:** Draft
**Approval needed from:** Product, Eng

---

Each story maps to one MVP workflow stage. Acceptance criteria are written to be testable (see `16-testing-plan.md` for how).

## US-1 Opportunity discovery (Stage 1)
**As** a creator, **I want** the system to suggest currently promising content ideas for my channel/concept, **so that** I don't have to manually track trends.
- AC1: Given a selected workspace, channel, and concept, the system returns a ranked list of opportunities, each with a score and the factors from §7's Opportunity Score breakdown.
- AC2: The user can dismiss, save, or request similar ideas; dismissed ideas do not reappear in the same session.
- AC3: If Opportunity Radar is deferred per open decision (§30), this story is descoped to a manual "start project" entry point with no ranked suggestions — flagged in `01-mvp-prd.md`.

## US-2 Project creation (Stage 2)
**As** a creator, **I want** to create a project scoped to a channel, concept, and source title, **so that** all downstream work is organized and reusable.
- AC1: A project cannot be created without a selected channel and concept.
- AC2: All fields listed in §Stage 2 are captured (season, episode, languages, format, duration, content mode, spoiler/theory level, rights declaration).
- AC3: Rights declaration is a required checkbox/field; project creation is blocked without it, and the declaration text does not claim legal clearance (§3.2).

## US-3 Source upload (Stage 3)
**As** a creator, **I want** to upload a large video directly to cloud storage with resumability, **so that** uploads survive network interruptions.
- AC1: Upload goes directly browser-to-object-storage (no full-file proxy through the app server) except where technically unavoidable (documented exception list maintained in `07-api-boundary-spec.md`).
- AC2: Upload can resume after a dropped connection without restarting from zero.
- AC3: Duplicate uploads (same content hash) are detected and the user is offered the existing analysis instead of re-uploading.
- AC4: Corrupted uploads are detected and rejected with a clear reason before entering the processing queue.

## US-4 Source processing (Stage 4)
**As** the system, **I need** to extract proxy, transcript, scenes, and features once per source, **so that** they can be reused across the project's lifetime (§3.4).
- AC1: All outputs listed in §Stage 4 are produced and persisted as structured artifacts (not only logs).
- AC2: Re-running Prompt Studio / Rough Cut / Script generation on the same project does not re-trigger any Stage 4 job.
- AC3: Each job is idempotent, retryable, checkpointed, cancellable, and reports progress (per `08-job-queue-spec.md`).

## US-5 Hot Moment extraction and exploration (Stage 5)
**As** a creator, **I want** to see 20–40 ranked candidate moments with reasons and score, **so that** I can pick strong clips quickly.
- AC1: Moment count is within 20–40 for a "typical" long source (definition of typical tracked as an open item pending real test data).
- AC2: Each moment card includes preview range, recommended core range, reasons, Hot Moment Score, spoiler level, characters, tags (per §Stage 5 example).
- AC3: All Stage 5 moment actions (select/favorite/reject/find similar/extend/filter by character/emotion/action/dialogue/theory/spoiler) are available and filter correctly.

## US-6 Moment selection (Stage 6)
**As** a creator, **I want** to select, reorder, lock, and annotate moments, **so that** I control what goes into the video.
- AC1: Locked moments are never altered by any AI regeneration action.
- AC2: "Find bridging moment" returns a moment from the same source only, using the Narrative Graph when available (Phase 1.1) or a heuristic fallback in MVP.

## US-7 Prompt Studio (Stage 7)
**As** a creator, **I want** to describe the intended video in natural language, **so that** the system generates a relevant rough cut and script.
- AC1: Logical Montage and Theory content modes are supported in MVP; other modes from §Stage 7 list are out of MVP scope unless promoted.
- AC2: The prompt and its resolved content mode are stored against the project for audit and regeneration.

## US-8 Rough cut generation and editing (Stage 8)
**As** a creator, **I want** an editable rough cut, not a flattened video, **so that** I can adjust it before scripting/voice.
- AC1: Rough cut is persisted as structured timeline JSON matching the shape in §Stage 8.
- AC2: Reorder, trim, lock, replace, crop-mode change, and audio-level change operations update the timeline JSON without a full regenerate.
- AC3: "Regenerate selected sections only" does not re-render or re-analyze untouched sections.

## US-9 Script generation and editing (Stage 9)
**As** a creator, **I want** three script variants with evidence links, **so that** I can trust and pick a defensible narration.
- AC1: Exactly 3 initial variants are produced: viral/fast, analytical, cinematic.
- AC2: Every sentence carries: timeline range, supporting source moment, confidence, statement type (confirmed/inference/interpretation/speculative per §3.6), authorship flag.
- AC3: Theory Guard v0 flags at minimum: theory-presented-as-fact, wrong chronology, and contradiction-with-transcript (subset of §11 checks feasible for v0; full list is a post-MVP target).
- AC4: Locked sentences are never silently overwritten by a Theory Guard warning or regeneration (§11).

## US-10 Voice generation (Stage 10)
**As** a creator, **I want** to generate and tweak voice-over per segment, **so that** I don't have to re-render the whole narration for one change.
- AC1: Voice, speed, stability, expressiveness, pauses, and pronunciation controls are exposed.
- AC2: Editing one script sentence and regenerating voice only regenerates the corresponding segment(s), not the full voice-over.

## US-11 Cloud timeline editing (Stage 11)
**As** a creator, **I want** to do final trims, captions, and audio mixing in-browser, **so that** CapCut/external tools stay optional.
- AC1: All Stage 11 "Initial features" are usable end-to-end for at least one project without leaving the platform.
- AC2: No feature in this story depends on an undocumented/unstable CapCut integration (§3, guardrail).

## US-12 Variant Arena (Stage 12)
**As** a creator, **I want** to compare cheap low-res hook variants before committing, **so that** I don't pay for full renders of options I won't use.
- AC1: At least 3 hook variants (action-first, dialogue-first, text-first) are generated at low resolution.
- AC2: Only the user-selected variant is submitted to full-quality rendering.

## US-13 Viral potential and view forecast (Stage 13–14)
**As** a creator, **I want** a rule-based viral score and a probabilistic view range, **so that** I can judge whether to publish without false certainty.
- AC1: Viral score output includes sub-scores (hook, topic timing, visual pacing, originality, competition) and at least one actionable recommendation, per the §Stage 13 example.
- AC2: View forecast always renders as a P10/P50/P90 range plus confidence label — never a single guaranteed number (§14, §31 non-goal).

## US-14 Publication (Stage 15)
**As** a channel owner, **I want** the video uploaded privately first and public release to require a distinct approval, **so that** nothing goes public by accident.
- AC1: The only path to `PUBLIC` state passes through `PUBLISH_APPROVAL_PENDING` → `PUBLISH_APPROVED`, both requiring `publication.approve_public`/`publication.publish` permission (§5).
- AC2: An audit record is created for the private upload and, separately, for the public approval action (§19).
- AC3: An unauthorized user's attempt to trigger public publish fails server-side even if attempted directly against the API (covered again in `16-testing-plan.md` E2E tests).

## US-15 Analytics and learning (Stage 16)
**As** a creator, **I want** post-publication analytics tied back to the decisions I made, **so that** future recommendations improve.
- AC1: At minimum the "Basic analytics" subset from §23 MVP scope is collected and displayed per project/channel.
- AC2: Collected signals are attributable to workspace, channel, concept per §12 "Learning must remain isolated by channel and concept unless a deliberate cross-channel rule is applied."

## Approval needed from
Product (story completeness), Eng (AC testability).
