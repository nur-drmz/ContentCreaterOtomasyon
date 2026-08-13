# 12 — Cost Model per Finished Video

**Formalizes:** `the plan.txt` §18 (Cost Optimization Strategy), §Stage 8–15 (rough cut through publication).
**Status:** Draft — structure only, unit costs pending provider selection
**Approval needed from:** Product, Eng

---

## 1. Relationship to `11-cost-model-source-hour.md`

Source-hour cost (`11`) is paid **once per source video** and amortized across every video produced from it (§3.4 "analyze once, reuse many times"). This document covers the **marginal cost of one additional finished Short**, assuming the source has already been analyzed.

```
CostPerFinishedVideo = AmortizedSourceAnalysisShare + MarginalProductionCost
```

`AmortizedSourceAnalysisShare` should trend toward zero as more videos are produced from the same source — a key argument for the platform's cost model and worth surfacing on a per-workspace dashboard once real data exists (ties to `29` product metrics "Analysis reuse rate" in `the plan.txt`).

## 2. Marginal production cost line items

| Line item | Driver | Unit | Placeholder unit cost | Notes |
|---|---|---|---|---|
| Rough-cut generation | 1 per accepted prompt (+ partial regens) | call | TODO | Mostly deterministic timeline assembly, cheap |
| Script generation (3 variants) | 1 per project (+ per-sentence regens) | call/token | TODO | T3 draft + T5 review, per `10-ai-model-routing-spec.md` |
| Theory Guard review | Per flagged sentence escalated to T5 | call | TODO | Most sentences resolved at T2/T3 |
| Voice generation | Per script sentence/segment (+ regenerated segments only) | audio-minute | TODO | ElevenLabs or alternative TTS, §Stage 10 |
| Preview rendering | Per edit iteration | render-minute | TODO | Low-res, cheap, frequent |
| Variant Arena low-res renders | 3 hook variants | render-minute | TODO | Only selected variant proceeds to full render (§Stage 12) |
| Final rendering | 1 per approved edit (+ re-renders of affected sections only, §18 item 8) | render-minute | TODO | Full quality |
| YouTube upload | 1 per project | call | TODO | Usually free API-wise but counted for quota/ops cost |
| Viral score + view forecast compute | 1 per final video | call | TODO | Mostly deterministic scoring (T0), cheap |
| Analytics collection | Recurring per publication | call | TODO | Scheduled job cost |
| Storage — clips/preview/final render | GB × retention days | GB-day | TODO | Per `09-media-lifecycle-policy.md` retention table |

## 3. Formula shape

```
MarginalProductionCost =
    RoughCutCost
  + ScriptGenerationCost (incl. Theory Guard escalations)
  + VoiceGenerationCost (only-regenerated-segments basis)
  + PreviewRenderCost × iterationCount
  + VariantArenaCost (3 low-res variants)
  + FinalRenderCost (full or partial re-render)
  + PublicationCost
  + PredictionComputeCost
  + StorageCost(clips, preview, final; retentionDays)
```

## 4. Regeneration cost sensitivity

Because §Stage 8–11 explicitly require partial regeneration (reorder/trim without full recompute, single-sentence script regen, single-segment voice regen, affected-section-only re-render), `iterationCount` and regeneration granularity are the dominant cost-control levers here, more so than model choice. This should be the first place to look if actual costs run high in Phase 0.2+.

## 5. Cost recording requirement

Same as `11-cost-model-source-hour.md` §4 — every line item observable via `ProviderUsage`, additionally aggregable to a "cost per finished Short" figure per §29 Production efficiency metric.

## 6. Open decisions

Same provider/hosting decisions as `11-cost-model-source-hour.md` §6, plus: whether Variant Arena's low-res renders and preview renders share a render-cache mechanism (segment render cache, §30 open decision) — would materially reduce `PreviewRenderCost × iterationCount`.

## Approval needed from
Product, Eng.
