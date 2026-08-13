# Alpha / Future SaaS Milestone Tracker

> This tracker belongs to the future cloud and multi-user Alpha roadmap.
> Current implementation progress is tracked in
> `../pre-alpha-local/08-implementation-milestones.md`.

Living checklist for the future AI Content Intelligence & Production
Platform. The authoritative Alpha scope/criteria live in `docs/alpha/`.

**How to use this file:** update checkboxes as work completes. Don't reorder or delete unchecked items without noting why (e.g., descoped, superseded) — this is meant to be a durable record you can scan anytime to see where the project stands.

**Last updated:** 2026-07-26

---

## Progress snapshot

| Phase | Status |
|---|---|
| Phase 0 — Planning and validation | 🟡 In progress |
| Phase 0.1 — Technical prototype | ⚪ Not started |
| Phase 0.2 — Moment Explorer | ⚪ Not started |
| Phase 0.3 — Prompt and script | ⚪ Not started |
| Phase 0.4 — Voice, edit, and publication | ⚪ Not started |
| Phase 0.5 — Multi-channel | ⚪ Not started |
| Phase 0.6 — Web and mobile beta | ⚪ Not started |
| Phase 1.0 — SaaS foundation | ⚪ Not started |
| Phase 1.1 — Intelligence advantage | ⚪ Not started |
| Phase 2.0 — Scale | ⚪ Not started |

Legend: ⚪ Not started · 🟡 In progress · 🟢 Complete · 🔴 Blocked

---

## Phase 0 — Planning and validation

Source: `../../the plan.txt` §24; docs: `docs/alpha/*`

### Required planning documents (§33) — review/approval status

- [x] `01-mvp-prd.md` drafted
- [x] `02-user-stories-acceptance.md` drafted
- [x] `03-role-permission-matrix.md` drafted
- [x] `04-state-transition-spec.md` drafted
- [x] `05-domain-model.md` drafted
- [x] `06-database-schema.md` drafted
- [x] `07-api-boundary-spec.md` drafted
- [x] `08-job-queue-spec.md` drafted
- [x] `09-media-lifecycle-policy.md` drafted
- [x] `10-ai-model-routing-spec.md` drafted
- [x] `11-cost-model-source-hour.md` drafted (structure only)
- [x] `12-cost-model-finished-video.md` drafted (structure only)
- [x] `13-security-threat-model.md` drafted
- [x] `14-repository-structure.md` drafted
- [x] `15-development-workflow.md` drafted
- [x] `16-testing-plan.md` drafted
- [x] `17-ai-evaluation-plan.md` drafted
- [x] `18-implementation-milestones.md` drafted
- [ ] `01` and `02` reviewed and approved by Product
- [ ] `03`–`18` reviewed and approved by their listed approvers (see `docs/alpha/00-index.md` §1)

### Open decisions resolved (see `docs/alpha/00-index.md` §3 for the full list)

- [x] Runtime platform: native Windows; WSL/Linux is not required
  (`adr/ADR-001-native-windows-runtime.md`)
- [ ] Product: final name, initial language/market, alpha vs. beta
- [ ] Product: Opportunity Radar in/out of first prototype
- [ ] Product: max source duration / file size, default retention values
- [ ] Web/mobile: React Native vs. Flutter, mobile upload in v1, offline prompt drafts
- [ ] Backend: framework, auth provider, DB hosting, object storage,
  Windows-compatible queue provider and vector storage approach (Python and
  native Windows runtime selected)
- [ ] AI: STT / embedding / vision / narrative / script model selection, local vs. API hosting, character clustering approach
- [ ] Rendering: FFmpeg-only vs. FFmpeg+Remotion, browser timeline approach, preview strategy, segment render cache
- [ ] Integrations: metadata provider, YouTube quota strategy, TTS commercial plan, CapCut feasibility
- [ ] Commercial: free plan limits, credit model, pricing (subscription/overage/team/storage)
- [ ] Security: four-eyes public-publish approval decision, MFA rollout timing, audit retention policy

### Proofs of concept (§24 Phase 0)

- [ ] Upload PoC (direct-to-storage, resumable, large file)
- [ ] Scene and audio pipeline PoC
- [ ] Rough-cut render PoC
- [ ] YouTube private upload PoC
- [ ] Provider cost validation (at least rough real numbers into `11`/`12` cost models)

**Phase 0 exit:** all boxes above checked.

---

## Phase 0.1 — Technical prototype

Source: `the plan.txt` §24; user stories: `02-user-stories-acceptance.md` US-3, US-4, US-5 (partial), US-8 (manual only)

- [ ] One user, one channel, one project hardcoded end-to-end path works
- [ ] Source video upload (US-3 AC1–AC4)
- [ ] Transcript extraction
- [ ] Scene detection
- [ ] Basic moment ranking (formula from §8, even if weights are placeholder)
- [ ] Manual moment selection (no filters/explorer UI needed yet)
- [ ] Simple rough cut assembled from manually selected moments, stored as structured timeline JSON (not flattened video)

**Phase 0.1 exit:** a real uploaded video produces a manually-assembled rough cut end to end.

---

## Phase 0.2 — Moment Explorer

Source: `02-user-stories-acceptance.md` US-5, US-6

- [ ] Preview cards (full Stage 5 card shape: preview range, core range, reasons, score, spoiler level, characters, tags)
- [ ] Filters: character, emotion, action, dialogue, theory potential, spoiler level
- [ ] "Find similar moments" action
- [ ] Scene Genome v0 persisted (`05-domain-model.md` §3 shape)
- [ ] Music-rise scoring feeding into Hot Moment score
- [ ] Narrative-importance v0 feeding into Hot Moment score
- [ ] Select / favorite / reject / lock / reorder / note / replace moment actions
- [ ] "Find a bridging moment" (heuristic fallback acceptable pre-Narrative-Graph)

**Phase 0.2 exit:** US-5 and US-6 acceptance criteria fully met.

---

## Phase 0.3 — Prompt and script

Source: `02-user-stories-acceptance.md` US-7, US-9

- [ ] Prompt Studio UI (natural-language prompt input)
- [ ] Logical Montage content mode
- [ ] Theory content mode
- [ ] 3 script variants generated (viral/fast, analytical, cinematic)
- [ ] Every script sentence has: timeline range, source moment, confidence, statement type, authorship flag
- [ ] Evidence links populated for every sentence (no exceptions)
- [ ] Theory Guard v0: theory-as-fact detection
- [ ] Theory Guard v0: wrong chronology detection
- [ ] Theory Guard v0: contradiction-with-transcript detection
- [ ] Locked sentences verified never silently overwritten

**Phase 0.3 exit:** US-7 and US-9 acceptance criteria fully met.

---

## Phase 0.4 — Voice, edit, and publication

Source: `02-user-stories-acceptance.md` US-10, US-11, US-14; `13-security-threat-model.md` publication checklist

- [ ] ElevenLabs TTS integration behind the `TextToSpeechProvider` interface
- [ ] Voice controls: voice, speed, stability, expressiveness, pauses, pronunciation
- [ ] Segment-level voice regeneration (only changed segments re-synthesized)
- [ ] Basic cloud timeline editor: trim, reorder, 9:16 crop, face-tracking crop, keyframe zoom
- [ ] Audio levels: original audio, voice-over, music, ducking
- [ ] Captions with timing and style
- [ ] Basic transitions and SFX
- [ ] Preview rendering
- [ ] Final rendering
- [ ] YouTube private/unlisted upload
- [ ] `PUBLISH_APPROVAL_PENDING → PUBLISH_APPROVED` and `PUBLISH_APPROVED → PUBLIC` implemented as two distinct, separately-audited actions
- [ ] E2E test: unauthorized public-approval attempt fails server-side
- [ ] Security review of publication gate signed off

**Phase 0.4 exit:** US-10, US-11, US-14 acceptance criteria fully met; publication safety checklist passes.

---

## Phase 0.5 — Multi-channel

Source: `the plan.txt` §24; `05-domain-model.md` §1, §7

- [ ] Support ≥2 channels per workspace
- [ ] Channel switching UI
- [ ] Concept switching UI
- [ ] Channel Brain v0 (learned profile fields populated from feedback signals)
- [ ] Channel-specific settings (tone, spoiler/theory policy, caption style, etc.)
- [ ] Scene Reuse Guard (reuse count + warning surfaced)
- [ ] Verified: feedback signals stay isolated per channel + concept (no cross-contamination)

**Phase 0.5 exit:** one user operates 2+ channels with distinct concepts and no learning leakage between them.

---

## Phase 0.6 — Web and mobile beta

Source: `the plan.txt` §15, §24

- [ ] Responsive web app covering full desktop workflow
- [ ] Mobile: notifications
- [ ] Mobile: Opportunity Radar view
- [ ] Mobile: channel/concept switching
- [ ] Mobile: project status
- [ ] Mobile: moment preview and selection
- [ ] Mobile: prompt input
- [ ] Mobile: script approval
- [ ] Mobile: voice selection
- [ ] Mobile: viral report view
- [ ] Mobile: publication approval
- [ ] Mobile: analytics overview
- [ ] Confirmed: no advanced mobile timeline editor built (out of scope this phase)
- [ ] Workspace roles enforced per `03-role-permission-matrix.md`
- [ ] Audit logging covers full `13-security-threat-model.md` §4 event list

**Phase 0.6 exit:** mobile covers exactly the §15 responsibility list, nothing more; audit + roles fully enforced.

---

## Phase 1.0 — SaaS foundation

Source: `the plan.txt` §24

- [ ] Multiple workspaces supported
- [ ] Workspace invitations
- [ ] Fine-grained RBAC fully enforced (all matrix cells from `03-role-permission-matrix.md`)
- [ ] Usage tracking (`UsageRecord`, `ProviderUsage`)
- [ ] Subscription plans
- [ ] Admin console (Platform Admin / Support Admin surfaces)
- [ ] Full tenant security review passed (`13-security-threat-model.md`)
- [ ] Commercial open decisions resolved (pricing, plan limits)

**Phase 1.0 exit:** tenant security review passes; billing/usage populated from real usage.

---

## Phase 1.1 — Intelligence advantage

Source: `the plan.txt` §24, §10, §13, §14

- [ ] Full Narrative Graph (beyond v0 heuristics)
- [ ] Portfolio Optimizer (cross-channel recommendation)
- [ ] Variant Arena (low-res hook variants → selected full render)
- [ ] Retention learning loop feeding back into Channel Brain
- [ ] Channel-calibrated prediction (§14 V2/V3, `ViewPrediction` with real intervals)

**Phase 1.1 exit:** predictions are channel-calibrated, not just rule-based V0.

---

## Phase 2.0 — Scale

Source: `the plan.txt` §24

- [ ] Agency plan
- [ ] White-label support
- [ ] Public API
- [ ] Provider marketplace
- [ ] Localization
- [ ] Multi-language channel variants
- [ ] Advanced mobile editing (only now in scope)

**Phase 2.0 exit:** all above shipped; this is the first phase where advanced mobile editing and a public API are appropriate per the guardrails.

---

## Cross-phase guardrails (recheck at every phase boundary)

- [ ] No automatic public publishing exists anywhere (permanent check, not phase-specific)
- [ ] No microservice extracted without documented justification (§25 criteria)
- [ ] No provider hard-coded into domain logic (spot-check `packages/domain` imports)
- [ ] No long media job running inside a synchronous API handler
- [ ] No OAuth tokens or provider secrets appearing in logs or client responses
