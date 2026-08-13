# 10 — AI Model Routing Specification

**Formalizes:** `the plan.txt` §3.5 (Cost-aware AI model routing), §3.6 (Evidence over hallucination), §3.7 (Provider independence), §8 (Hot Moment Engine), §18 (Cost Optimization / inference cascade).
**Status:** Draft
**Approval needed from:** Eng, Product

---

## 1. Principle

"The strongest and most expensive model must not be used for every task" (§3.5). Every AI-assisted stage in the pipeline must declare which tier it uses and why, and must not skip the cheaper tiers that could filter its input first.

## 2. Model tiers (generic, provider-agnostic — §3.7)

| Tier | Description | Example use |
|---|---|---|
| T0 — Deterministic | FFmpeg/CPU signal processing, no ML | Shot boundaries, audio amplitude, basic scene cuts |
| T1 — Speech/audio ML | STT, audio classifiers | Transcription, laughter/shout/explosion detection |
| T2 — Lightweight embeddings/classifiers | Small models, cheap per-call | Candidate filtering, similarity search, coarse emotion/action tags |
| T3 — Medium multimodal/LLM | Mid-cost models | Narrative/semantic analysis on filtered candidates, Scene Genome fields, Narrative Graph edges |
| T4 — Strong multimodal | Vision-capable, expensive | Only top-N candidate scenes per §3.5 step 6 |
| T5 — Strong LLM | Most expensive | Final script reasoning, quality review, Theory Guard adjudication on flagged sentences only |

Concrete model/provider names for each tier are **not selected here** — open decisions ("Speech-to-text model", "Embedding model", "Vision model", "Narrative model", "Final script model", §30) — this document fixes the *routing shape*, providers plug in via the interfaces in `07-api-boundary-spec.md` §4.

## 3. Stage-to-tier routing table

| Pipeline stage | Tier(s) used | Rationale |
|---|---|---|
| Shot/scene boundary detection | T0 | Deterministic, no ML needed |
| Transcription | T1 | Purpose-built STT |
| Audio feature curves (intensity, music rise) | T0/T1 | Signal processing + audio classifiers |
| Initial scene/candidate filtering | T2 | Cheap embeddings prune the ~full scene list down before expensive analysis |
| Scene Genome field extraction (characters, actions, emotions, narrative_role) | T2 for coarse tags, T3 for narrative_role/theory_potential on filtered scenes | Matches §3.5 steps 3–5 |
| Narrative Graph construction | T3 | Cross-scene reasoning over an already-filtered scene set |
| Hot Moment scoring | T2/T3 combination per §8 weighted formula | Weighted formula itself is deterministic math over T2/T3-derived features |
| Top-candidate deep review (only for scenes advancing to script) | T4 | §3.5 step 6: "Strong multimodal models only for top candidate scenes" |
| Script generation (3 variants) | T3 for drafting, T5 for final quality/evidence review pass | §3.5 step 7: "Strong LLM only for final script reasoning and quality review" |
| Theory Guard | T2/T3 rule + classifier pass first; escalate only flagged sentences to T5 for adjudication | Avoids running the expensive tier over every sentence |
| Viral score / view forecast | T0 math (weighted formula, §13/§14) + T2 features; not an LLM task | Deterministic scoring, not generative |

## 4. Cascade enforcement rule

No stage may call a T4/T5 provider on an input that has not first passed through the corresponding T0–T3 filtering stage for that pipeline, except where the source document explicitly allows a direct high-tier call (none identified — script quality review and top-candidate review are the only T4/T5 entry points, and both require upstream filtering). This is enforced at the module boundary (`07-api-boundary-spec.md`): the Video Analysis and Script Generation modules' internal orchestration, not left to individual call sites, so a future contributor can't accidentally wire a cheap task straight to a T5 provider.

## 5. Evidence and confidence requirement (§3.6)

Every script sentence output by T3/T5 generation must carry, at generation time:
- a source timestamp / selected moment / transcript segment reference, or a verified external source reference,
- a statement classification: confirmed by source / reasonable inference / user interpretation / speculative theory.

This is a hard output-schema requirement on the Script Generation provider interface (`ScriptSentence` + `EvidenceLink`, `05-domain-model.md` §5), not an optional field — a script sentence without a classification is rejected before it reaches `SCRIPT_DRAFT`.

## 6. Channel-specific routing variation

`ChannelModelVersion.feature_weights` / `scene_ranking_weights` / `prompt_templates` (§4.4) may change *weights and prompts* per channel concept but do not change which tier is used for which stage — tier routing is a platform-level cost/architecture decision, not a per-channel one.

## 7. Open decisions

- Concrete model/provider selection per tier (§30 AI section, in full).
- Whether Theory Guard's T5 adjudication step is synchronous-blocking on `SCRIPT_DRAFT → SCRIPT_APPROVED` or advisory-only in MVP (recommend advisory-with-visible-warning for MVP per US-9 AC4's "locked sentences never silently overwritten," full blocking gate deferred).
- Local vs. API-hosted models ("Local versus API models", §30) — affects cost model in `11-cost-model-source-hour.md` but not the tier routing shape itself.

## Approval needed from
Eng (routing enforcement), Product (which stages are worth T4/T5 spend at MVP scale).
