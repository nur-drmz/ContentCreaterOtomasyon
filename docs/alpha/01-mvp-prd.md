# 01 — MVP Product Requirements Document

**Formalizes:** `the plan.txt` §1 (Product Vision), §2 (Positioning), §3 (Non-Negotiable Principles), §6 (Main User Workflow), §23 (MVP Scope).
**Status:** Draft
**Approval needed from:** Product

---

## 1. Problem statement

Entertainment YouTube creators (theory channels, recap channels, character-analysis channels) spend disproportionate time finding which scene of a long source video is worth clipping, writing a defensible script around it, and producing a Short. The MVP targets that specific workflow — not general-purpose video editing or clipping.

## 2. MVP goal

Let a single user, in one workspace, with one connected YouTube channel and one channel concept, go from **an uploaded long-form video** to **a privately-uploaded, human-approved Short on YouTube**, using AI to identify moments, draft a script, generate voice-over, and assemble a rough cut — with **no automatic public publication** (non-negotiable, §3.1).

## 3. In scope (MVP)

Directly adopts `the plan.txt` §23 "Included in MVP" list (35 items), grouped here by workflow stage:

| Area | MVP behavior |
|---|---|
| Auth & tenancy | Single-user login; schema supports future multi-workspace/multi-channel even though only one of each is exercised in MVP UX |
| Channels | Connect one YouTube channel via OAuth; one channel concept |
| Opportunity Radar | Present recommended movies/episodes/topics (§7); may ship as a simplified v0 if it risks delaying the core pipeline — see open decision in §30 "Opportunity Radar inclusion in the first prototype" |
| Upload | Direct-to-object-storage large video upload, resumable, hashed, deduped (§Stage 3) |
| Source processing | Proxy, audio track, transcript, shot/scene boundaries, keyframes, audio intensity/music-rise curves, basic narrative features (§Stage 4) |
| Hot Moments | 20–40 ranked candidate moments with reasons, score, spoiler level (§Stage 5) |
| Moment Explorer | Select/favorite/reject/filter moments (§Stage 5–6) |
| Prompt Studio | Natural-language prompt; Logical Montage and Theory content modes only for MVP (§Stage 7) |
| Rough cut | Structured timeline JSON, editable (reorder/trim/lock/replace/crop/audio) (§Stage 8) |
| Script Studio | 3 script variants (viral/analytical/cinematic), evidence-linked sentences, Theory Guard v0 (§Stage 9, §11) |
| Voice Studio | ElevenLabs integration, segment-level regeneration (§Stage 10) |
| Timeline editor | Basic cloud editor: trim, reorder, 9:16 crop, face-track crop, audio levels/ducking, captions, basic transitions/SFX, preview render (§Stage 11) |
| Rendering | Final render; CapCut remains optional/out of critical path (§3, §Stage 11) |
| Scoring | Rule-based viral score, basic competitor comparison, low-confidence probabilistic forecast (§Stage 13–14) |
| Publication | Upload private/unlisted; explicit separate public-approval action (§Stage 15, §3.1) |
| Analytics | Basic collection (§Stage 16, subset) |
| Media lifecycle | Temporary cleanup per §17 retention table |
| Audit | Audit logging of the actions listed in §19 |

## 4. Out of scope (MVP)

Directly adopts `the plan.txt` §23 "Excluded from MVP" and §31 "Explicit Non-Goals": no advanced mobile timeline editor, no custom foundation model, no full CapCut automation, no automatic public publishing, no general social suite, no agency billing/white-label, no large-scale fine-tuning, no guaranteed view prediction, no unconfirmed automatic character recognition, no DRM bypass or protected-stream downloading, no streaming credential storage.

## 5. Non-negotiable constraints carried into every MVP feature

1. Human approval gate before any public publish (§3.1) — enforced at the state-machine level, not just UI (see `04-state-transition-spec.md`).
2. User-provided media only; rights declaration requested but not presented as legal clearance (§3.2).
3. Cloud-first: no assumption of a permanent local project folder; browser-to-storage uploads (§3.3).
4. One analysis reused across multiple prompts/cuts/scripts (§3.4) — the MVP must not re-run full source analysis per script variant.
5. Cost-aware model routing cascade (§3.5, formalized in `10-ai-model-routing-spec.md`).
6. Every factual script statement evidence-linked and classified (§3.6, formalized in `11`... see `02` and Theory Guard notes).
7. Provider interfaces, not direct provider calls, in domain logic (§3.7).
8. Native Windows reference runtime for owned backend, AI/media and rendering
   processes; no WSL/Linux dependency
   (`adr/ADR-001-native-windows-runtime.md`).

## 6. Success criteria for MVP (exit criteria)

- A test user can complete the full pipeline (upload → private YouTube upload) for at least one real long-form video without manual database/file intervention.
- No path exists in the UI or API that publishes publicly without a distinct, audited approval action.
- Source video is analyzed once; a second prompt/script/rough-cut cycle on the same project does not re-trigger full source analysis.
- At least one non-ElevenLabs TTS provider can be added by implementing the provider interface only (no domain-logic changes) — proves §3.7 in practice, even if not shipped.
- Media lifecycle deletion jobs run automatically per the retention table without manual cleanup.

## 7. Open decisions this PRD depends on

See `00-index.md` "Open decisions" and `the plan.txt` §30. Notably unresolved and *not* assumed here: product name, initial language/market, alpha vs. beta, max source duration/file size, default retention values (table in §17 is a *proposal*), whether Opportunity Radar ships in the first prototype.
