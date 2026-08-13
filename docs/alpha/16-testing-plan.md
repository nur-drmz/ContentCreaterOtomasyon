# 16 — Testing Plan

**Formalizes:** `the plan.txt` §28 (Testing Strategy).
**Status:** Draft
**Approval needed from:** Eng

---

## 1. Unit tests (§28, list preserved + owner mapping)

| Area | Ties to |
|---|---|
| Score calculations (Hot Moment, viral score, opportunity score, view forecast) | `10-ai-model-routing-spec.md`, §7/§8/§13/§14 formulas |
| State transitions | `04-state-transition-spec.md` — every table row should have a corresponding unit test, including rejected/illegal transitions |
| Permission checks | `03-role-permission-matrix.md` — every matrix cell with `X`/`-`/`own` should have at least one positive and one negative test |
| Retention policies | `09-media-lifecycle-policy.md` — including the "do not delete before final upload confirmed" exception |
| Provider adapters | `07-api-boundary-spec.md` §4 — each interface implementation tested against the interface contract, independent of the real vendor (mockable) |
| Prompt validation | Content-mode resolution, rejection of unsupported modes in MVP (US-7) |
| Theory classification | Theory Guard checks (§11), statement-type schema enforcement (`10-ai-model-routing-spec.md` §5) |

## 2. Integration tests (§28, list preserved)

| Area | Ties to |
|---|---|
| Multipart upload | US-3, `09-media-lifecycle-policy.md` §5 |
| Object lifecycle | Media asset creation → retention expiry → deletion job, including the exception path |
| Queue and worker flow | `08-job-queue-spec.md` — idempotency, retry, checkpoint, cancellation, timeout per job type |
| YouTube OAuth | Token storage/refresh/revocation, scope minimization (`13-security-threat-model.md` T3) |
| Private upload | US-14 AC1/AC2 |
| TTS generation | Segment-level regeneration only touching changed segments (US-10 AC2) |
| Tenant isolation | Cross-workspace access attempts rejected at every module boundary (`13-security-threat-model.md` T2) |

## 3. End-to-end tests (§28, list preserved)

| Scenario | Ties to |
|---|---|
| Registration to private upload | Full MVP happy path, `01-mvp-prd.md` §6 exit criteria |
| Multiple channel switching | Channel/concept scoping doesn't leak state across channels |
| Unauthorized public approval attempt | Must fail server-side even via direct API call, not just hidden in UI (`13-security-threat-model.md` §5 checklist) |
| Failed analysis recovery | A single failed sub-job (e.g., transcription) is retryable without re-running the whole Stage 4 pipeline (`08-job-queue-spec.md` §4) |
| Failed render retry | Partial re-render on retry, not full re-render, where the job supports checkpoints |
| Media deletion workflow | Retention sweep + exception-hold behavior end to end |

## 4. AI evaluation

Covered in depth in `17-ai-evaluation-plan.md`; summarized here as a required test category per §28: moment relevance, narrative connection quality, script factuality, evidence coverage, theory certainty calibration, hallucination rate, user acceptance rate, regeneration rate — "must use versioned datasets and human review rubrics" (§28 closing line).

## 5. CI gating recommendation

- Unit + integration tests block merge (per `15-development-workflow.md` PR review process).
- Windows is a required CI platform. Path handling, subprocess management,
  FFmpeg invocation and CUDA-capable worker smoke tests must be exercised using
  native Windows behavior; a Linux-only green build is insufficient.
- E2E tests run on a merge-queue/staging cadence given their cost/duration; not necessarily on every PR.
- AI evaluation runs on a scheduled cadence against versioned datasets (not per-PR, given cost) with alerting on regression beyond a defined threshold (threshold TBD, see `17-ai-evaluation-plan.md`).

## 6. Open decisions

- Python test framework/tooling details remain open; selected tooling must run
  natively on Windows.
- Coverage thresholds — not specified in source doc; recommend setting once Phase 0.1 codebase exists rather than guessing now.

## Approval needed from
Eng.
