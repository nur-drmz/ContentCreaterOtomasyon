# 18 — Versioned Implementation Milestones

**Formalizes:** `the plan.txt` §24 (Release Phases), cross-referenced against §23 (MVP Scope) and §32 guardrail "Do not build advanced mobile editing before web validation" / "Do not introduce microservices prematurely."
**Status:** Draft
**Approval needed from:** Product, Eng

---

Each phase below restates §24's scope and adds explicit entry/exit criteria so "done" is checkable, not judged by feel. No phase should start coding until its entry criteria (approved docs from this set) are met, per §32.16.

## Phase 0 — Planning and validation
**Scope (§24):** Final MVP PRD, user flows, authorization matrix, upload PoC, scene/audio pipeline PoC, rough-cut render PoC, YouTube private upload PoC, provider cost validation.
- **Entry:** This document set (`docs/alpha/*`) reviewed.
- **Exit:** `01`, `02`, `03` approved; every owned PoC runs natively on
  Windows without WSL; at least one technical PoC per bullet above completed
  against a real (even if throwaway) provider choice, to de-risk before Phase
  0.1 commits to the remaining stack choices.

## Phase 0.1 — Technical prototype
**Scope (§24):** One user, one channel, one source video, transcript, scene detection, basic moment ranking, manual selection, simple rough cut.
- **Entry:** Stack decisions in §30 "Backend"/"AI" resolved enough to write code; `05`, `06`, `07`, `08` reviewed.
- **Exit:** A real video goes from upload → transcript → scenes → ranked moments → manually assembled rough cut, end to end, for one hardcoded user/channel. Corresponds to US-3, US-4, US-5 (partial), US-8 (manual only) in `02-user-stories-acceptance.md`.

## Phase 0.2 — Moment Explorer
**Scope (§24):** Preview cards, filters, similar moments, Scene Genome v0, music-rise scoring, narrative-importance v0.
- **Entry:** Phase 0.1 exit met.
- **Exit:** US-5 and US-6 fully met per their acceptance criteria; Scene Genome persisted per `05-domain-model.md` §3; Hot Moment score uses the §8 weighted formula, not a placeholder.

## Phase 0.3 — Prompt and script
**Scope (§24):** Prompt Studio, Logical Montage, Theory mode, script variants, evidence links, Theory Guard v0.
- **Entry:** Phase 0.2 exit met.
- **Exit:** US-7 and US-9 fully met; every script sentence has a populated `EvidenceLink` and statement classification (no exceptions, per `10-ai-model-routing-spec.md` §5); Theory Guard v0 flags at least the subset defined in US-9 AC3.

## Phase 0.4 — Voice, edit, and publication
**Scope (§24):** TTS provider, basic timeline, final render, YouTube private upload, public approval gate.
- **Entry:** Phase 0.3 exit met; `13-security-threat-model.md` publication checklist reviewed by Security.
- **Exit:** US-10, US-11, US-14 fully met; the two-step approve/publish separation (`04-state-transition-spec.md` critical rule) is implemented and covered by the E2E "Unauthorized public approval attempt" test (`16-testing-plan.md`).

## Phase 0.5 — Multi-channel
**Scope (§24):** Multiple channels, channel/concept switching, Channel Brain v0, channel-specific settings, Scene Reuse Guard.
- **Entry:** Phase 0.4 exit met.
- **Exit:** A single user can operate ≥2 channels with distinct concepts without cross-contamination of learning signals (`05-domain-model.md` §7 `user_feedback_signals` isolation by channel+concept); Scene Reuse Guard (§13) surfaces reuse counts/warnings.

## Phase 0.6 — Web and mobile beta
**Scope (§24):** Responsive web, mobile approval/monitoring, notifications, workspace roles, audit logs.
- **Entry:** Phase 0.5 exit met.
- **Exit:** Mobile app covers only the §15 "Mobile application" responsibility list (notifications, radar, switching, status, preview/selection, prompt input, approvals, forecast, publication approval, analytics overview) — explicitly **not** an advanced timeline editor (§15 closing line, §32.11); full `03-role-permission-matrix.md` enforced; audit log coverage matches `13-security-threat-model.md` §4 list.

## Phase 1.0 — SaaS foundation
**Scope (§24):** Multiple workspaces, invitations, fine-grained RBAC, usage tracking, subscription plans, admin console, tenant security review.
- **Entry:** Phase 0.6 exit met; commercial open decisions (§30 "Commercial") resolved.
- **Exit:** A full tenant security review (`13-security-threat-model.md`) passes; billing/usage tables (`06-database-schema.md` §8) populated from real usage.

## Phase 1.1 — Intelligence advantage
**Scope (§24):** Narrative Graph (full), Portfolio Optimizer, Variant Arena, retention learning, channel-calibrated prediction.
- **Entry:** Phase 1.0 exit met; sufficient production analytics volume exists to calibrate predictions (§14 V2/V3).
- **Exit:** Prediction outputs include channel-calibrated intervals per `05-domain-model.md` §6 `ViewPrediction`, not just the rule-based V0.

## Phase 2.0 — Scale
**Scope (§24):** Agency plan, white label, public API, provider marketplace, localization, multi-language channel variants, advanced mobile editing.
- **Entry:** Phase 1.1 exit met.
- **Exit:** Only phase where advanced mobile editing and a public API are in scope at all, consistent with the guardrail deferring both until web/core validation is complete.

## Cross-phase constraints (apply to every phase)

- Owned backend, AI/media worker and rendering code must remain operable on the
  native Windows reference runtime
  (`adr/ADR-001-native-windows-runtime.md`).
- No phase introduces automatic public publishing (§32.2) — this is permanent, not a Phase 0.4-only concern.
- No phase extracts a microservice without the justification list in §25 (independent scaling / failure isolation / different runtime / security isolation / cost optimization / team ownership) documented at extraction time.
- Every phase's exit criteria should be traceable to specific acceptance criteria in `02-user-stories-acceptance.md`, not just the prose scope bullets above.

## Open decisions

- Calendar/time estimates per phase are intentionally omitted — the source document does not specify them and estimating without a chosen stack would be fabricated precision.

## Approval needed from
Product, Eng.
