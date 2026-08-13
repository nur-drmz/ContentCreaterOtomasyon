# 14 — Repository Structure (Draft)

**Formalizes:** `the plan.txt` §26 (Provisional Monorepo Direction), §25 (Architecture Evolution).
**Status:** Draft — runtime platform approved; scaffolding still waits for the
remaining stack decisions (§26 closing line)
**Approval needed from:** Eng

---

## 1. Status note

`the plan.txt` §26 ends with: "Do not create this structure until the stack and roadmap are explicitly approved." This document therefore describes the **intended** structure for when that approval happens; it is not an instruction to scaffold the repo now, and no directories from this doc should be created as part of this planning-documents deliverable.

## 2. Proposed structure (verbatim from §26, annotated)

```text
/apps
  /web        -- React/Next.js (framework choice open, §30) — full project setup, uploads, Moment Explorer,
                 Narrative Graph, prompting, script/timeline editors, channel/workspace settings, analytics (§15)
  /mobile      -- React Native or Flutter (open, §30) — notifications, Opportunity Radar, approvals, monitoring (§15)
  /admin       -- web-based, Platform/Support Admin surfaces (§5 platform roles)
  /api         -- the modular monolith described in §16 "Backend" — one deployable exposing the module
                 boundaries from `07-api-boundary-spec.md`, not one service per module (§25)

/workers
  /media       -- upload validation, proxy generation, audio/subtitle extraction, media deletion (`08-job-queue-spec.md`)
  /analysis    -- transcription, scene detection, keyframe extraction, Scene Genome, Narrative Graph, Hot Moment ranking
  /render      -- rough-cut, preview, and final rendering jobs, Variant Arena
  /integrations -- YouTube upload/analytics polling, TTS, trend-data collection, prediction recalibration

/packages
  /domain         -- entities and business rules from `05-domain-model.md`, provider-interface contracts (`07-api-boundary-spec.md` §4)
  /contracts      -- shared API request/response types between /apps and /api
  /auth           -- session/permission helpers implementing `03-role-permission-matrix.md`
  /ui             -- shared design system components (web + admin; mobile likely separate given RN/Flutter split)
  /config         -- shared config/env schema
  /observability  -- correlation-id propagation, logging redaction rules (§27, `13-security-threat-model.md` T7)
  /testing        -- shared test fixtures/harness (`16-testing-plan.md`)

/infrastructure   -- IaC, deploy config — empty/placeholder until hosting decisions (§30) are made

/scripts
  /windows        -- PowerShell setup, validation, worker and maintenance scripts

/docs
  /planning       -- this document set
  /adr            -- accepted architecture decisions, including the Windows runtime
```

## 3. Rationale for module boundary mirroring

`/workers` subfolders mirror the job categories in `08-job-queue-spec.md`, and `/packages/domain` mirrors the module list in `07-api-boundary-spec.md` §2 — the intent is that moving a module from in-process (modular monolith) to an extracted service later (§25) is a matter of moving a `/packages/domain/<module>` + a `/workers/<category>` into their own deployable, not a rewrite. This directly supports §25's "Keep clear module boundaries so the following may later become independent services: Video Analysis, Rendering, Trend Collection, Prediction, YouTube Integration."

## 4. What this document does not decide

- Web/mobile language and framework choices remain open. Backend and worker
  services use Python on the native Windows reference runtime
  (`adr/ADR-001-native-windows-runtime.md`).
- Monorepo tooling (Turborepo/Nx/Bazel/etc.) — not mentioned in source doc, left fully open.
- CI/CD pipeline shape — see `15-development-workflow.md`.

## 5. Open decisions

All of §30 "Backend" and "Web and mobile" subsections gate when this structure can actually be created. This document is approved as a *target shape*, with actual scaffolding blocked until those decisions land.

## Approval needed from
Eng.
