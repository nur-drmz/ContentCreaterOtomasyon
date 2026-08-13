# 17 — AI Evaluation Plan

**Formalizes:** `the plan.txt` §28 "AI evaluation" list, §29 (Product Metrics — Quality section), §11 (Theory Guard), §3.6 (Evidence over hallucination).
**Status:** Draft
**Approval needed from:** Eng, Product

---

## 1. Why this is separate from `16-testing-plan.md`

Standard tests check code correctness; AI evaluation checks *output quality*, which is probabilistic and drifts as models/prompts/weights change. §28 explicitly requires "versioned datasets and human review rubrics" — this is a distinct, recurring process, not a one-time test suite.

## 2. Evaluation dimensions (§28 list, each given a measurable definition)

| Dimension | Definition | Data source |
|---|---|---|
| Moment relevance | Fraction of Hot Moments a human reviewer rates as "would plausibly select" for the given channel concept | Sampled `HotMoment` sets + reviewer rubric |
| Narrative connection quality | Whether selected-moment sequences the Narrative Graph judges connected are rated coherent by a human reviewer | `NarrativeGraph` outputs + reviewer rubric |
| Script factuality | Fraction of `ScriptSentence` rows whose `EvidenceLink` actually supports the claim, per human spot-check | Sampled `ScriptSentence`/`EvidenceLink` pairs |
| Evidence coverage | Fraction of script sentences that carry a non-null evidence link and statement classification | Automatable from schema (`10-ai-model-routing-spec.md` §5) — no human review needed for this metric specifically |
| Theory certainty calibration | Whether sentences classified "speculative theory" are *worded* with appropriate hedging (not stated as fact) | Human rubric against `EvidenceLink.evidence_type` |
| Hallucination rate | Fraction of script sentences referencing an event/character/relationship not supported anywhere in the transcript or Narrative Graph | Human spot-check, cross-referenced against transcript |
| User acceptance rate | Fraction of AI-generated moments/scripts/voice takes the user keeps without edit or rejection | Automatable from `UserFeedbackSignal` (`05-domain-model.md` §7) |
| Regeneration rate | How often a user requests regeneration per stage | Automatable from job trigger logs (`08-job-queue-spec.md`) |

## 3. Dataset versioning

- Maintain a fixed, versioned "golden set" of source videos with human-reviewed expected moments/scripts, distinct per channel concept where behavior is expected to differ (e.g., Theory vs. Recap).
- Every model/prompt/weight change (`ChannelModelVersion`, `05-domain-model.md` §1) is evaluated against the golden set *before* being published, not just monitored after the fact in production — ties to §4.4's draft/published/rollback support.
- Golden set changes are themselves versioned, so a metric regression can be traced to "the model got worse" vs. "the golden set changed."

## 4. Human review rubric (minimum shape)

For each sampled item, a reviewer records: pass/fail per dimension above, a severity tag if fail (minor wording vs. factually wrong vs. spoiler/policy violation), and free-text notes. Rubric detail (scoring scale, reviewer count per item, inter-rater agreement target) is left open for Eng/Product to define once real output exists to calibrate against — premature to fix now.

## 5. Cadence

- Automatable metrics (evidence coverage, acceptance rate, regeneration rate): continuous, dashboarded per channel/concept.
- Human-reviewed metrics (relevance, connection quality, factuality, calibration, hallucination): run against the golden set on every `ChannelModelVersion` draft before publish, plus a periodic (cadence TBD) production sample audit to catch drift the golden set doesn't cover.

## 6. Relationship to Theory Guard

Theory Guard (§11) is the automated, real-time control; this evaluation plan is the offline, human-in-the-loop control that measures whether Theory Guard itself is working (e.g., a rising hallucination rate despite Theory Guard passing everything is a signal Theory Guard's checks need to be strengthened, not that the metric is wrong).

## 7. Open decisions

- Reviewer sourcing (internal team vs. contracted reviewers) — not specified in source doc.
- Specific pass/fail thresholds that would block a `ChannelModelVersion` publish — deferred until Phase 0.2/0.3 real data exists.

## Approval needed from
Eng, Product.
