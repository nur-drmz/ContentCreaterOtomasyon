# ScriptService

> Introduced in Milestone 13 — the extraction that gives `selected_script` an
> application-level owner. Both audited mutation paths (AI generation and the
> human text edit) moved out of `PipelineService` unchanged; prompts, provider
> routing, parsing and artifact construction stayed canonical in
> `local_pipeline.script_generation`.

## Why this boundary exists

`selected_script` is the only artifact in the project that costs money to
produce and is the sole input to TTS. Its two writers sat ~800 lines apart on
the façade: a job-backed generator that spends provider quota, and a
synchronous human edit that silently archives every downstream artifact.
ScriptService is now the single application writer, so "one request is one
paid call", "generated text is never approved" and "an edit invalidates the
voice" each have exactly one implementation.

## Responsibilities

| Method | Responsibility |
|---|---|
| `run_script_generation(*, job_id=None, actor="console", enable_google_model_fallback=True, max_google_model_attempts=4, max_google_transient_retries=2)` | the ONLY generation writer: `require_phase("script_generation")` → tracker start → required `hot_moments` + `moment_selection` → `validate_moment_selection(require_preview=True)` → canonical engine call with the plan's decision inputs → plan-derived angle/language stamping → artifact write → `provider.call` audit → tracker finish/fail |
| `update_script_text(*, script_text, updated_by, notes="")` | the ONLY human-edit writer: actor/empty/20000-char validation → unchanged-text no-op → `voice`-stage revision snapshot → approval reset + `status="review_required"` → `manual_edits` entry (cap 50) → artifact write → `script.updated` audit |

The three `max_google_*`/`enable_google_model_fallback` knobs are preserved
verbatim: they exist so a deliberate single-call validation can pin the
request to exactly one model, one attempt, zero retries.

### What deliberately stayed outside

- **Canonical generation** — `build_user_prompt`, `build_script_candidates`,
  `call_google_script_routed`, `parse_script_json`,
  `build_selected_script_artifact`, `GOOGLE_SCRIPT_MODELS` and the one-paid-
  call cost contract all stay in `local_pipeline.script_generation` (Google
  routing/quota/retry/safety policy in `local_pipeline.google_ai`). A guard
  asserts the service does not re-declare any of them.
- **`moment_selection` / `hot_moments` ownership** — MomentService and
  VideoAnalysisService; ScriptService only READS them.
- **Approval** — ApprovalService owns grants. Generation output is always
  `review_required`; an edit drops a grant by resetting the block the content
  hash binds to, never by writing approval state.
- **TTS and render** — `run_tts`/`run_render` stay on the façade. A script
  mutation archives their artifacts; it never starts them.
- **The review-screen projection** — `_script_review_view` stays in
  `creator_console.app` (see below).
- **Transcript and glossary** — not inputs, not read, not written.

## Dependencies (all injected)

| Constructor arg | Port / type | Used for |
|---|---|---|
| `artifacts` | `ports.ArtifactRepository` | `hot_moments`/`moment_selection`/`processing_plan` reads and the `selected_script` read/write (`artifact_relative("hot_moments")` supplies the engine's source-path field) |
| `workflow` | `ports.WorkflowGateQuery` | `require_phase("script_generation")` — this is what denies direct/original-audio mode |
| `archiver` | `ports.RevisionArchiver` | the `voice`-stage snapshot a human edit takes; the service decides WHICH stage, the adapter performs the move |
| `progress` | `ports.ProgressFactory` | the `script_generation` tracker (stages `preparation`/`request`/`parse`/`write`), cancellation bound by the job runtime |
| `audit` | `ports.AuditSink` | `provider.call` and `script.updated` |
| `secrets` | the secret resolver | `credential("google_api")` to build a client; the value never enters an artifact or event |
| `google_client_factory`, `openai_client_factory`, `anthropic_client_factory` | injected factories | the ESTABLISHED provider seam (same shape AnalysisService/PublishService use) — never a façade-owned lambda |

No new port was introduced. Raw `ArtifactRepository` reads were sufficient:
generation needs the *raw* `hot_moments` manifest and the *raw*
`moment_selection` document because it hands both to the canonical validator
and the engine, so a `MomentQuery`/`TranscriptQuery` would have added an
indirection without supplying a semantic result the service needs.

## Script scope: project-wide

`selected_script` is **project-wide**, not per-slot:

- `ARTIFACTS["selected_script"] == "results/selected_script.json"` and the key
  is absent from `PER_SLOT_ARTIFACTS`, so `artifact_relative(..., slot=N)`
  returns the same path for every slot.
- Generation reads the unslotted `moment_selection` (slot 1).
- `run_tts`, render, the approval chain and `_script_review_view` all read the
  same single path.
- `narrated_theory` plans are validated with `output_count == 1`, so a
  multi-slot narrated batch cannot exist today — which is why one project-wide
  script is coherent. Preserved exactly; not converted to per-slot.

## Actual upstream input matrix

| Input | Source | Note |
|---|---|---|
| `hot_moments` | `load_artifact(required=True)` | the manifest, passed whole to the engine |
| `moment_selection` | `load_artifact(required=True)` | `selected_moment_rank` + `reviewed_by` |
| selection validity | `validate_moment_selection(..., require_preview=True)` | a script is written about a moment a human actually watched; a missing/changed preview file fails |
| `target_duration_sec` | `processing_plan.decision`, default `60` | |
| `content_language` | `processing_plan.decision` | stamped onto the result afterwards too |
| `theory_angle` | `processing_plan.decision`, `""` when absent | a derived angle wins over the plan's empty field |
| `theory_angle_auto` | `processing_plan.decision` | passed as `derive_theory_angle` |
| provider clients | injected factories / `credential("google_api")` | `script_provider="google"`, `allow_provider_fallback=False` |
| `moment_approved` / `moment_approved_by` | hardcoded `True` / `selection.reviewed_by` | the gate already proved the selection is human-reviewed |

**`moment_transcript` is NOT an input to script generation.** The narrated
script is written from the moment's analysis metadata, not from recognised
speech.

**The glossary is NOT an input to script generation.** ScriptService neither
reads nor writes `results/glossary.json`. Milestone 12's TranscriptService
glossary-suggestion side effect is unchanged, and the split glossary
ownership remains a documented P3 outside this milestone.

Both absences are guarded by an architecture test, not just by convention.

## Workflow modes and eligibility

`require_phase("script_generation")` is the only gate:

- `direct_clip_series` / `original_audio_subtitles` — **denied**. The Short
  uses the source's own words; TranscriptService existing does not entitle
  anything to manufacture a script.
- `narrated_theory` — allowed once the plan is approved. Plan creation itself
  additionally requires a theory angle (or `theory_angle_auto`), a Gemini
  credential and `output_count == 1`; those are `source_strategy` plan rules,
  upstream of this service.

## Provider integration and fallback behaviour

Unchanged: `script_provider="google"`, `allow_provider_fallback=False`, so
`build_script_candidates` yields exactly ONE candidate and one request is one
paid call (asserted with the fixture provider counter). Google always goes
through the canonical router (Route A: `gemini-3.6-flash` →
`gemini-3.5-flash` → `gemini-3.5-flash-lite`), which owns model fallback,
the quota ledger, transient-retry counts and error classification.

OpenAI/Anthropic clients are still constructed when their factories are
injected but are never selected under this configuration — preserved exactly,
and tested (`providers == {"google"}`).

A provider failure does **not** raise: the engine records the attempt and
returns a **prompt-only package** (`script_text=None`, `status`
`review_required`, `prompt_package` present). Safety refusals are terminal
across providers by engine policy, and cross-provider fallback is disabled on
this path. Because `approval.content_hash_for("script", …)` raises when there
is no `script_text`, a refusal or failure can never produce an approvable
script.

## Generation pipeline

1. `require_phase("script_generation")` — before any work or tracker.
2. `ProgressFactory.create("script_generation")` → `tracker.start`.
3. `stage("preparation")` → required `hot_moments` + `moment_selection`;
   problems → `ArtifactProblem`; validation errors →
   `PipelineServiceError("moment secimi gecersiz/bayat: …")`.
4. Read `processing_plan.decision`; build the Google client.
5. `stage("request")` → canonical engine call (one paid call).
6. `stage("parse")` → stamp `theory_angle`/`theory_angle_auto`/
   `content_language` from the plan.
7. `stage("write")` → `write_artifact("selected_script")` (emits
   `artifact.written`).
8. `provider.call` audit **after** the durable write, with phase/provider/
   model only — never the prompt, the script or a credential.
9. `tracker.finish` (or `tracker.fail` when the result status is `failed`).
   Any exception → `tracker.fail(f"{type}: {exc}")` → re-raise.

Ordering is load-bearing and preserved: `tracker.finish` never precedes the
write, and a provider success followed by a write failure reports failure —
with no `provider.call` event, since that event comes after the write.

## Human-edit pipeline

`update_script_text` strips the input, requires a non-empty actor and text,
caps at 20000 characters, and loads the script `required=True`. It computes
the old content hash, then:

- **unchanged text → no-op**: returns the artifact with no write, no
  revision, no audit event and no approval loss.
- **changed text** → `archive_revision(REWIND_PATHS["voice"], action="script_edit", target="script")`
  → set text, `status="review_required"`, the human-edit status reason, reset
  `human_approval`, prepend a `manual_edits` entry
  (`updated_by`/`updated_at`/`notes`/`previous_script_sha256`/`revision_id`,
  history capped at 50) → write → `script.updated` audit with
  `previous_script_sha256`, `revision_id` and `downstream_archived`.

`REWIND_PATHS` moved to the neutral `application.artifact_catalog` so the
façade's rewind paths, the subtitle edit and this service share ONE
definition; `pipeline_service.REWIND_PATHS` is a re-export (a compatibility
surface `verify_revision_flow.py` imports). The rewind *policy* did not move
into `RevisionArchiver` — the archiver performs mechanics, the script
lifecycle chooses the `voice` stage.

## Revision and downstream invalidation

`REWIND_PATHS["voice"]` archives: `results/selected_voiceover.json`,
`results/tts`, `results/voice_review.json`, `results/render`,
`results/render_review.json`, `results/automation.json`, `progress`,
`results/subtitle_track.json` and `results/publish_intent.json`. The
transcript is deliberately NOT in the `voice` stage (it is upstream of both
voice and render), and the glossary is in no stage at all.

Regeneration invalidates differently and unchanged: it overwrites the
artifact, so the content hash moves and the previous grant stops binding —
no archive is taken, because nothing downstream existed that the new script
was written for.

## Approval binding

`APPROVAL_ARTIFACTS["script"] == "selected_script"` and
`approval.content_hash_for("script", …)` hashes the **script text itself**
(raising when there is none). Consequences, all tested:

- a generated script is `review_required` with
  `human_approval.approved = False`;
- an edit resets the block and changes the hash, so a previously valid grant
  becomes `approved=False, valid=False`;
- a no-op edit leaves a valid grant valid;
- a regeneration drops the grant;
- a prompt-only package is not reviewable.

ScriptService never emits `approval.granted` and uses no approval port — it
needs no approval read at all.

## TTS and render relationship

Unchanged and still on the façade. `run_tts` requires
`selected_script.status == "approved"` **and**
`approval.check_approval(...)["valid"]`, reads the plan's narration voice and
`content_language`, and raises `PipelineServiceError` for an unapproved or
stale script. Render eligibility flows through `approval_chain`, where an
unapproved script leaves `render.effective_valid` false. A script mutation
initiates neither.

## `_script_review_view` ownership decision

**It stays in `creator_console/app.py`.** Its body loads the already-owned
artifact and formats twelve fields, adding one presentation convenience
(`character_count = len(script_text)`). There is no script-domain rule in it:
no validation, no digest, no eligibility, no mutation. Moving it would make
ScriptService a UI view-model service, which this milestone explicitly
forbids. **No domain logic was extracted from it**, and a regression test
pins its exact output and key set after extraction.

## Fault windows

Every `selected_script` write goes through `LocalArtifactRepository`
(already an `_ATOMIC_WRITE_HOLDERS` entry), so no new module-level
`atomic_write_json` binding exists. Tested under ENOSPC, permission denied
and a 48-byte partial write on the generation path, and ENOSPC on the edit
path: the artifact stays absent (or is reported corrupt on read), no
`provider.call`/`script.updated` event is emitted, no approval appears and
progress ends `failed`.

**Documented non-atomic window (P2):** in `update_script_text` the downstream
archive MOVES voice/render artifacts *before* the script write. A crash in
between leaves the previous script live with its descendants already
archived — recoverable (the revision is restorable) and fail-safe in the
direction that matters (no approved-but-orphaned downstream state), but not
atomic. Transactionality was deliberately not redesigned in this milestone.

## Test strategy

- 39 characterization tests written against the pre-extraction façade.
- 7 parity tests: a standalone ScriptService on real adapters vs the façade.
- 5 provider tests using the canonical `creator_console.fixtures` doubles,
  including the one-paid-call counter and fallback-disabled proof.
- 14 negatives covering mode denial, missing/stale upstream, provider
  exception/malformed/empty/refusal, cancellation, write faults, approval and
  downstream non-initiation.
- 5 architecture guards: port wiring, canonical-engine use, no transcript or
  glossary dependency, single writer, no candidate artifact.

## Known limitations

- The engine never returns `status="failed"` today, so the façade's (now the
  service's) `tracker.fail(...)` branch for that case is unreachable in
  practice. It is preserved verbatim and covered by a monkeypatched test.
- A prompt-only package occupies `selected_script`, so "no script yet" and
  "generation could not produce one" share a path and are distinguished only
  by `script_text`/`status_reason` — preserved façade behaviour.
- `moment_approved=True` is hardcoded because the gate already proved the
  selection is human-reviewed; the engine's own "unapproved moment" guard is
  therefore unreachable from this path.
- The script edit has no job/progress record (it is synchronous, as before).
