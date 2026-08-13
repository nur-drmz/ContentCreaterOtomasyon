# 08 — Job and Queue Specification

**Formalizes:** `the plan.txt` §16 (Long-running background jobs list and per-job requirements).
**Status:** Draft
**Approval needed from:** Eng

---

## 1. Universal job contract

Every job type below must implement (verbatim requirement from §16):

- **Idempotency** — safe to run twice for the same input (idempotency key = deterministic hash of job type + input refs + config version).
- **Retries** — bounded, with backoff; count and thresholds are per-job-type config (open, tune during Phase 0.1).
- **Checkpoints** — resumable sub-steps where the job is naturally decomposable (e.g., per-chunk upload validation, per-segment TTS).
- **Progress reporting** — emits a progress percentage/stage the client can poll or subscribe to.
- **Cancellation** — a `CANCEL` signal is honored between checkpoints.
- **Timeouts** — per-job-type max runtime; on timeout, transitions to `FAILED` with a reason, not left hanging.
- **Failure reasons** — structured, not just a stack trace — surfaced to the state machine (`04-state-transition-spec.md`) and to the user.
- **Cost recording** — every provider call inside the job writes a `ProviderUsage` record (`05-domain-model.md` §7) linked to `job_id`.

## 2. Job type table

| Job | Input | Output | Idempotency key | Checkpointable | Typical trigger |
|---|---|---|---|---|---|
| Upload validation | `source_video_id` | pass/fail + reason | content_hash | per-chunk | `SOURCE_UPLOADED` |
| Proxy generation | `source_video_id` | proxy `media_asset` | source content_hash + proxy config version | yes (segment-wise) | `SOURCE_VALIDATED` |
| Audio extraction | `source_video_id` | audio `media_asset` | content_hash | no | `SOURCE_VALIDATED` |
| Subtitle extraction | `source_video_id` | subtitle asset (if embedded) | content_hash | no | `SOURCE_VALIDATED` |
| Transcription | audio asset | `TranscriptSegment[]` | audio asset hash + STT provider/model version | yes (per audio chunk) | after audio extraction |
| Scene detection | proxy asset | `Scene[]` | proxy hash + detector version | yes (per reel/segment) | after proxy generation |
| Keyframe extraction | `Scene[]` | keyframe assets | scene set hash | yes (per scene) | after scene detection |
| Audio analysis | audio asset | intensity/music-rise curves | audio hash + analyzer version | yes (per segment) | after audio extraction |
| Scene Genome generation | `Scene[]` + transcript + audio features | `SceneGenome[]` | inputs hash + model/weights version | yes (per scene) | after scene detection + transcription + audio analysis |
| Narrative Graph generation | `SceneGenome[]` + transcript | `NarrativeNode/Edge[]` | inputs hash + model version | partial (per act/segment if source is long) | after Scene Genome generation |
| Hot Moment ranking | `SceneGenome[]` + `NarrativeGraph` + concept weights | `HotMoment[]` (20–40) | inputs hash + `channel_model_version_id` | no (single pass, cheap relative to above) | `ANALYZED` |
| Rough-cut generation | `MomentSelection[]` + `UserPrompt` | `TimelineVersion` | selection set hash + prompt hash + weights version | yes ("regenerate selected sections only" per US-8 AC3) | `PROMPT_APPROVED`, or user-triggered partial regen |
| Script generation | `TimelineVersion` + concept + content mode | `ScriptOption[]` (3 variants) | timeline version hash + concept version | yes (per sentence, for single-sentence regen) | `ROUGH_CUT_APPROVED`, or user-triggered sentence regen |
| Voice generation | `ScriptSentence[]` | `VoiceSegment[]` | sentence text hash + voice settings | yes (per segment — "only changed segments regenerate", §Stage 10) | `SCRIPT_APPROVED`, or segment-level regen |
| Preview rendering | `TimelineVersion`/`VideoEdit` (draft) | low-res preview asset | edit state hash | yes (per affected timeline range) | any edit change in `EDITING` |
| Final rendering | `VideoEdit` (approved) | `FinalVideo` | edit state hash + render config version | yes (per timeline range where practical, §18) | `EDIT_APPROVED` |
| YouTube upload | `FinalVideo` | `Publication` (private/unlisted) | final video hash + channel id | yes (resumable upload) | `FINAL_APPROVED` |
| Analytics collection | `Publication` | `AnalyticsSnapshot` | publication id + time bucket | no | scheduled, post `UPLOADED_PRIVATE`/`PUBLIC` |
| Prediction recalibration | `AnalyticsSnapshot[]` for channel | updated `ChannelModelVersion.prediction_settings` (draft) | channel id + snapshot batch id | no | scheduled, per `10-ai-model-routing-spec.md`/prediction cadence |
| Media deletion | `MediaAsset` past `retention_expires_at` | deletion confirmation | asset id | no | scheduled sweep, per `09-media-lifecycle-policy.md` |

## 3. Queue/worker model (logical)

- Distinct queues per cost/latency class, so a slow Narrative Graph job doesn't starve a fast upload-validation job: `fast-cpu`, `transcription`, `vision-heavy`, `llm-heavy`, `render`, `publish`, `scheduled`.
- All owned workers use the native Windows reference runtime defined in
  `adr/ADR-001-native-windows-runtime.md`. Worker launch, shutdown and
  cancellation must not depend on Unix signals, Bash or `systemd`.
- Concrete queue technology is not decided here, but it must support native
  Windows workers and preserve the logical separation above.
- Every job carries a `correlation_id` linking to workspace/channel/project/media asset/user action (§27 observability requirement) — set at enqueue time and propagated through all checkpoints and retries.

## 4. Failure handling

- On exhausted retries, the job's owning project transitions to `FAILED` per `04-state-transition-spec.md`, with the structured failure reason surfaced in the UI and in `AuditLog`.
- Analysis-stage failures (proxy/transcription/scene/audio/genome/graph) should identify which specific sub-job failed so the user/support can retry only that sub-job rather than the whole analysis pipeline — required to keep "analyze once" (§3.4) economical even when one sub-job flakes.

## 5. Open decisions

- Concrete retry counts/backoff curves and timeout durations per job type — to be set from real timing data in Phase 0.1/0.2, not guessed here.
- Whether preview/final rendering use segment-level render caching (§18 item 8, "Segment render cache" open decision, §30) — modeled as desired but not committed.

## Approval needed from
Eng.
