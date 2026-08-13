# Local Pre-Alpha Implementation Milestones

**Status:** Current progress tracker

## P0 — Documentation and repository

- [x] Preserve Claude documents as `docs/alpha/`.
- [x] Create the local Pre-Alpha planning set.
- [x] Review the Pre-Alpha set for internal link consistency.
- [x] Initialize a local Git repository with no remote.
- [x] Add `.gitignore` rules for media, models, database and secrets.

**Exit:** Current scope and decisions are discoverable from `00-index.md`.

## P1 — Environment and GPU PoC

- [x] Install native Windows Python 3.12 x64 and `uv`.
- [x] Create the project-local virtual environment and lockfile.
- [x] Install native Windows FFmpeg/FFprobe.
- [x] Validate native PyTorch and CTranslate2 CUDA compatibility.
- [x] Create G:/D: directory roots.
- [x] Place a test source in `G:\Youtube kaynak videolar`.
- [ ] Validate Whisper transcription on RTX 3080.
- [ ] Validate scene/audio/motion extraction.
- [ ] Validate Qwen VLM structured output.
- [ ] Record time, VRAM, RAM and artifact-size benchmark.
- [ ] Lock dependency and model revisions after success.

**Exit:** All PoC gates in `07-testing-evaluation.md` pass.

## P2 — Local application skeleton

- [ ] Django/HTMX project. (Django/server-rendered UI complete; HTMX deferred
  until an interaction requires it.)
- [x] SQLite WAL and migrations.
- [x] Project/source/job core models.
- [x] Single sequential worker. (Bounded `run_worker --once` source preflight;
  later pipeline stages intentionally pause.)
- [x] Health endpoint and native PowerShell one-click launcher.
- [ ] Local structured logging.

**Exit:** Launcher opens the app and a resumable test job completes.

## P3 — Media library

- [x] Source-library discovery.
- [x] Rights acknowledgement and project creation.
- [x] FFprobe validation and duration/size guards.
- [x] In-place source registration without file mutation.
- [ ] Hash-based duplicate detection.
- [ ] Disk warnings and generated-output trash/restore.
- [ ] Metadata backup and restore.

**Exit:** Source lifecycle tests pass without writing outside approved roots.

## P4 — Analysis and Moment Explorer

- [x] Subtitle/Whisper transcript cascade. (`studio/pipeline/transcript.py`;
  code-complete and unit-tested with mocked handlers, not yet exercised
  against the real GPU sample video — see the unchecked P1 Whisper PoC gate.)
- [x] Scene/audio/motion pipeline with checkpoints. (`studio/pipeline/scenes.py`,
  `audio.py`, `motion.py`, wired into `worker.run_analysis` with the same
  checkpoint/cancel/resume machinery as preflight; same real-hardware caveat
  as above.)
- [x] Pre-candidate generation. (`studio/pipeline/candidates.py`; deterministic
  scene/transcript/audio/motion merge and oversampled ranking, no VLM input yet.)
- [x] VLM enrichment and schema validation. (`studio/pipeline/vlm.py`; strict
  schema validation, retry-once repair prompt, `local_files_only=True` so no
  analysis job ever triggers a model download per `06-security-privacy.md`
  §2 — degrades to deterministic-only scoring, never invents values, when
  the model isn't already cached. Not yet exercised with real cached
  weights/hardware.)
- [x] Versioned Hot Moment score. (`studio/pipeline/scoring.py`,
  `FORMULA_VERSION`; real `HotMoment` rows now persisted by
  `worker.run_analysis`'s `SCORING` stage instead of only the demo seeder.)
- [x] Adaptive 20/30/40 results. (`pipeline/candidates.adaptive_target_count`,
  applied when capping scored candidates.)
- [ ] Preview cards, character/reason/spoiler filters. (Cards render real data;
  client-side filtering is still the static/demo-only filter rail.)
- [ ] Select/reject and context playback. (Decision persistence already works;
  "context playback" of the actual proxy segment is still not wired up.)
- [x] Optional focus prompt and no-reanalysis re-ranking.
  (`studio/pipeline/rerank.py`, `worker.run_rerank`, `RERANK_MOMENTS` job;
  reads only stored HotMoment data, never re-triggers stages 1-9. The
  "semantic relevance" is a local token-overlap heuristic, not embeddings —
  usable but a known quality gap.)

**Exit:** Three-video quality gate reaches five strong Top-20 moments each.

## P5 — Timeline and rough cut

- [x] 3–6 clip proposal. (`studio/pipeline/timeline.py`, `worker.run_propose_timeline`,
  `PROPOSE_TIMELINE` job; snaps unlocked clip edges to transcript segment
  boundaries and caps at 6 clips.)
- [x] Reorder, trim and lock — partial. Lock is fully wired (toggle on the
  Moment Explorer, respected exactly by timeline proposal). Trim uses the
  existing `MomentSelection.trim_in/trim_out` fields when set. Reorder has
  no drag-and-drop UI yet; only the existing `order_index` from selection
  order is used.
- [x] Versioned timeline JSON. (`TimelineVersion` model, unique per
  `(project, version_number)`, linked to its `parent_version`.)
- [x] Simple optional English captions. (`studio/pipeline/render.py` SRT
  generation + `subtitles=` burn-in when `TimelineVersion.captions_enabled`
  is set; no UI toggle for it yet — defaults to off.)
- [x] 720×1280 blurred-background H.264/AAC render. (`pipeline/render.py`;
  per-clip ffmpeg composite of a blurred/darkened background + centered
  foreground, concatenated, not yet run against real source media.)
- [x] Original-audio normalization. (`loudnorm` audio filter on the final
  concat pass.)
- [ ] Project close/reopen and render reuse. (Renders are versioned and kept,
  but there is no explicit archive/reopen flow or render-reuse cache check yet.)

**Exit:** Full E2E scenario passes and the 180-minute performance gate holds.

## P6 — Pluggable voice-over module (new scope, added by explicit user request)

Not part of the original P0-P5 plan; TTS was previously listed under
"Deferred until after Pre-Alpha" below. Scope is deliberately narrower than
the alpha docs' Script Studio: the user writes narration text directly, no
AI script generation.

- [x] `TextToSpeechProvider` interface + pluggable registry
  (`studio/voice/providers/`), matching the shape already specified in
  `docs/alpha/07-api-boundary-spec.md` so switching providers never touches
  call sites.
- [x] `ElevenLabsProvider` (real HTTP integration) and `LocalDisabledProvider`
  (default, zero-network fallback — see `06-security-privacy.md` §3 for the
  full approval/cost-limit/audit gate this satisfies).
- [x] Narration script + stable per-sentence identity
  (`NarrationScript`/`NarrationSentence`), `VoiceOver`/`VoiceSegment` models.
- [x] Segment-level regeneration — only sentences whose text or voice
  settings changed are re-synthesized (`worker.run_voiceover`, content-hash
  comparison), mirroring `docs/alpha/02-user-stories-acceptance.md` US-10 AC2.
- [x] Cost limit + audit trail (`VOICE_COST_LIMIT_USD`, `AuditEvent` model).
- [x] Voice Studio UI: script editor, provider dropdown, required approval
  checkbox, per-segment playback.
- [ ] Fine-grained voice controls (speed, stability, expressiveness, pauses,
  pronunciation) — `voice_settings` is wired end-to-end but the UI only
  exposes provider selection today.
- [ ] Narration mix-down into the rendered rough cut (`pipeline/render.py`
  does not yet accept a `VoiceOver` to duck/mix under the original audio).
- [ ] Real-provider validation — no test has made a real ElevenLabs API call;
  coverage is against a fake provider and a network-call-blocked check for
  the real one.

## Deferred until after Pre-Alpha

- Cloud/API provider implementation for the *analysis* pipeline (VLM stays
  local-only/optional as designed; no cloud analyzer is planned).
- AI script generation (narration remains user-authored; see P6).
- 1080p final export and advanced timeline.
- Remote/multi-user access.
- YouTube integration and publishing.
