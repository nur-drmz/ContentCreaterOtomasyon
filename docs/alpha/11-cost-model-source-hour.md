# 11 — Cost Model per Source Hour

**Formalizes:** `the plan.txt` §18 (Cost Optimization Strategy), §3.5 cascade, §16 (Data layer / jobs).
**Status:** Draft — structure only, unit costs pending provider selection
**Approval needed from:** Product, Eng, Finance (if applicable)

---

## 1. Purpose

Model the cost of taking one hour of source video through Stage 4 analysis (§Stage 4) once, so that per-project and per-workspace economics can be estimated before providers are chosen. This is a **cost structure**, not a priced estimate — every unit cost below is a placeholder pending the open provider decisions in §30.

## 2. Cost line items per source hour

| Line item | Driver | Unit | Placeholder unit cost | Notes |
|---|---|---|---|---|
| Object storage — original ingest | GB stored × retention days | GB-day | TODO | Depends on storage provider (§30) and retention policy (`09-media-lifecycle-policy.md`) |
| Object storage — proxy/derived assets | GB stored × retention days | GB-day | TODO | Proxy typically far smaller than original |
| Egress/transfer | GB moved (upload + internal worker reads) | GB | TODO | Direct-to-storage upload (§3.3) minimizes app-server egress but worker reads still cost |
| Compute — proxy generation | Source minutes | minute | TODO | T0, CPU-bound (`10-ai-model-routing-spec.md`) |
| Compute — audio extraction/analysis | Source minutes | minute | TODO | T0/T1 |
| Speech-to-text | Audio minutes | minute | TODO | T1, provider TBD |
| Compute — scene/shot detection | Source minutes | minute | TODO | T0 |
| Compute — keyframe extraction | Keyframe count | frame | TODO | Sparse sampling per §18 item 3 |
| Embeddings/classifiers (T2) | Scene count × feature count | call | TODO | Cheap, high volume |
| Medium multimodal/LLM (T3) | Filtered scene count | call/token | TODO | Scene Genome fields, Narrative Graph |
| Strong multimodal (T4) | Top-candidate scene count only | call | TODO | §3.5 step 6 — bounded by candidate cap (20–40 moments, §Stage 5) |
| Job orchestration overhead | Job count × avg duration | job-minute | TODO | Queue/worker runtime cost, provider TBD |

## 3. Formula shape

```
CostPerSourceHour =
    StorageCost(original, proxy; retentionDays)
  + TransferCost(uploadGB, workerReadGB)
  + Σ ComputeCost(stage) for stage in {proxy, audio, scene, keyframe}
  + SttCost(audioMinutes)
  + EmbeddingCost(sceneCount)
  + T3Cost(filteredSceneCount)
  + T4Cost(topCandidateCount)   // bounded, not proportional to source length beyond candidate cap
  + OrchestrationOverhead(jobCount)
```

`T4Cost` is intentionally decoupled from raw source length — because candidate count is capped at ~20–40 moments (§Stage 5) regardless of source duration, long sources do not linearly increase the most expensive cost line, which is the point of the cascade (§3.5, §18).

## 4. Cost recording requirement

Every term above must be observable in production via `ProviderUsage` rows (`05-domain-model.md` §7), scoped by workspace/channel/project/job/provider/model — per §18 item 11's explicit requirement — so this formula can be calibrated against real numbers starting in Phase 0.1, rather than staying a theoretical model.

## 5. Levers this model must make visible (from §18)

- Effect of proxy-first analysis vs. full-resolution analysis.
- Effect of sparse vs. dense keyframe sampling.
- Effect of caching by content hash + config version (repeat analysis of a previously-seen source should cost ~0).
- Effect of scale-to-zero workers on orchestration overhead during idle periods.

## 6. Open decisions blocking real numbers

Storage/queue/compute provider selection, STT/embedding/vision/LLM provider selection, local-vs-API model hosting (§30) — all unit costs above are `TODO` until these are resolved. This document's job is to make sure the *shape* is right so plugging in real numbers later is a data-entry exercise, not a redesign.

## Approval needed from
Product (which levers matter most for pricing decisions), Eng (formula shape accuracy).
