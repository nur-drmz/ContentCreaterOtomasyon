# WorkflowGate

> Introduced in Milestone 9 — the narrowest extraction so far. It moves the
> plan-derived workflow-eligibility surface out of `PipelineService` so future
> production services can ask "may this phase run, and which slot am I?"
> without importing the façade.

## Why this boundary exists

Every `run_*` method in the façade begins with the same question — does the
approved `processing_plan` permit this phase? Today that answer lives on
`PipelineService.plan_gate` / `require_phase`, and the slot scope lives on
`require_slot` / `slot_*` / `slot_moment_rank`. A `VideoAnalysisService`,
`MomentService` or `RenderService` that asked the façade would import
providers, jobs and secrets it never uses — exactly the coupling the previous
extractions removed.

`WorkflowGate` is the same answers behind `ports.WorkflowGateQuery`, needing
nothing but the artifact repository.

## Responsibilities

| Method | Responsibility |
|---|---|
| `plan_gate(phase)` | `(allowed, reason)` — plan present/valid/approved + mode permits the phase |
| `require_phase(phase)` | raise `PipelineServiceError` when `plan_gate` denies |
| `plan_decision()` | the plan's `decision` block (`{}` when no plan) |
| `slot_count()` | how many Shorts the plan defines (1 when no plan) |
| `slot_indexes()` | `1..slot_count` |
| `require_slot(slot)` | validated slot index, or `PipelineServiceError` |
| `slot_moment_rank(slot)` | the plan entry's pre-assigned moment rank, or `None` |

All seven are read-only plan queries. `WorkflowGate` writes nothing, audits
nothing, and holds no provider, tracker, binary resolver or secret.

### Deliberately outside

| Concern | Owner | Why not here |
|---|---|---|
| phase-eligibility policy (`plan_allows_phase`, `MODE_PHASE_POLICY`, `GATED_PHASES`) | `local_pipeline.source_strategy` | canonical deterministic policy; the gate binds it to the stored artifact |
| plan-approval binding (`check_approval`) | `local_pipeline.approval` | canonical digest engine; consulted inside `plan_allows_phase` |
| slot arithmetic (`slot_count`, `require_slot`) | `application.slot_policy` | already a neutral module; `ApprovalService` shares it directly |
| content-approval lifecycle (`state`, `chain`, `invalidated`) | `ApprovalService` | approval ownership (Milestone 5) |
| rights/copyright checklists | `ports.ReviewGate` via `ApprovalService` | consulted at grant time, not re-checked per phase |
| per-phase artifact prerequisites (hot_moments, selection, script approval, render manifest) | each `run_*` runner | execution concerns — see the matrix below |
| render execution, providers, jobs, progress, secrets | their existing owners | the gate has no way to call them — by construction |

## Dependency graph

```
ConsoleApi
   │
   ▼
PipelineService ─────────────────────────────────────────┐
   │            │           │          │        │        │
   ▼            ▼           ▼          ▼        ▼        ▼
WorkflowGate  Source   EditorSvc  Analysis  Approval  Publish
   │            │           │          │        │        │
   ▼            ▼           ▼          ▼        ▼        ▼
ArtifactRepository   SourceQuery   ports.ApprovalInvalidator
                                     ports.ReviewGate
                                     ports.ApprovalQuery
```

`WorkflowGate → ports.ArtifactRepository` is its only edge. The façade exposes
it as `service.workflow_gate` and keeps all seven methods as thin delegates.

## Workflow eligibility matrix

Two layers decide whether a phase runs. The **gate** (this service) is the
first hurdle — plan shape, approval binding and mode. **Phase prerequisites**
are enforced inside each `run_*` method and are documented here so future
extractions know what else must move.

### Gate layer (`plan_gate` / `require_phase`)

| Condition | Result |
|---|---|
| no `processing_plan` artifact | deny: "Faz 0 isleme plani yok…" |
| corrupt/invalid plan artifact | deny: artifact `problem` string |
| plan fails `validate_processing_plan` | deny: "isleme plani gecersiz: …" |
| `status != "approved"` | deny: "isleme plani onaylanmamis" |
| `status == "approved"` but decision changed out-of-band | deny: "onayi artik gecerli degil" (binding re-derived, never trusted) |
| phase not in `GATED_PHASES` | **`ValueError`** — a programming error, not a denial |
| mode permits phase | allow `(True, "")` |
| mode denies phase | deny with the mode reason |

### Mode policy (`MODE_PHASE_POLICY` — canonical, unchanged)

| Phase | `direct_clip_series` | `narrated_theory` |
|---|---|---|
| `video_analysis` | allow | allow |
| `moment_selection` | allow | allow |
| `transcript` | allow — captions come from the source's own words | deny — captions come from the approved script |
| `script_generation` | deny — no generation-API call exists in this mode | allow |
| `tts` | deny — original audio is kept | allow |
| `render` | allow | allow |

### Phase prerequisites (enforced inside `run_*`, unchanged by this milestone)

| Entry point | Additional prerequisites beyond the gate |
|---|---|
| `run_video_analysis` | source attached (probe/analysis read it) |
| `run_moment_selection` | `hot_moments` artifact; rank must exist; no other slot may hold the same rank |
| `run_transcript` (require `transcript`) | selected moment; whisper runs locally |
| `run_script_generation` | `moment_selection` validates against `hot_moments` (`require_preview=True`); plan mode narrated |
| `run_tts` | `selected_script` artifact `status="approved"` AND `check_approval("script")` still binds |
| `run_render` (per slot) | `require_slot`; `hot_moments`; `moment_selection`; transcript (direct) or voiceover (narrated); `check_config_drift` vs approved plan |
| publish chain | **separate**: content approval (`ApprovalQuery`), metadata approval, publish-intent approval, private upload, visibility approval — `PublishService`, never this gate |

## Approval and rights integration

The gate consumes exactly one approval fact: whether the `processing_plan`'s
recorded `human_approval` still binds its current decision — computed by
`local_pipeline.approval.check_approval` inside `plan_allows_phase`. It computes
no approval of its own, grants none, and invalidates none.

- `ApprovalService.state/chain/invalidated` remain the only live approval
  truth for every produced artifact (script, voiceover, transcript, render).
- Rights checklists remain behind `ports.ReviewGate`, consulted by
  `ApprovalService._assert_preconditions` at grant time. A plan only reaches
  `status="approved"` through that gate, so the workflow gate does not
  re-check rights.
- `PublishService` keeps its existing `ApprovalQuery` path — publication
  authorization is NOT routed through WorkflowGate. There is no path from
  "render allowed" to anything public.

## Per-slot rules

`slot_count`/`require_slot`/`slot_moment_rank` read `plan.outputs` — the same
list the render directories are built from — so slot scope can never disagree
with the plan on disk. `slot_count` is 1 when no plan exists (historical
single-output default). `require_slot` rejects non-integer and out-of-range
slots with `PipelineServiceError`; `slot_moment_rank` returns `None` for a
slot the plan does not define and for entries with no pre-assigned rank.

## Error semantics

| Call | Denied | Unknown input |
|---|---|---|
| `plan_gate(phase)` | `(False, reason)` — never raises for an ordinary no | `ValueError` for unknown phase |
| `require_phase(phase)` | `PipelineServiceError(reason)` | `ValueError` propagates |
| `require_slot(slot)` | `PipelineServiceError` | `PipelineServiceError` ("gecersiz cikti yuvasi") |
| `plan_decision`, `slot_*`, `slot_moment_rank` | n/a — `{}`, `1`, `None` for absent plans | — |

The denial reasons are the operator-facing strings Phase 0/0a/0b tell the
user to run — unchanged byte-for-byte by this extraction.

## Compatibility guarantees

- All seven façade methods keep their signatures and observable behaviour;
  characterization tests pass unchanged before and after the move.
- Gate calls write no artifact and emit no audit event — a read-only check
  stays read-only.
- Direct-mode projects are never asked for script/TTS approval; narrated-mode
  projects still are (via their own runners and the approval chain).
- No API payload, artifact shape, slot layout or audit vocabulary changed.

## Readiness assessment for future production services

| Future service | Will own | Gate queries needed | Other ports it reuses | Still missing / order |
|---|---|---|---|---|
| ~~`VideoAnalysisService`~~ **extracted — Milestone 10** | `run_analysis`, `hot_moments` write | `require_phase` (consumed) | `ArtifactRepository`, `SourceQuery`, `MediaBinaries`, `ProgressFactory` | done — see `VIDEO_ANALYSIS_SERVICE.md` |
| ~~`MomentService`~~ **extracted — Milestone 11** | `record_moment_selection`, `moment_rank_owner`, `selected_moment`, `clip_bounds*`, `moment_selection` + `clip_bounds/output_NN.json` writes | `require_phase`, `require_slot`, `slot_indexes` (rank-owner scan), `slot_moment_rank` | `ArtifactRepository`, `SourceQuery`, `AuditSink`, `RevisionArchiver` | done — see `MOMENT_SERVICE.md` |
| ~~`TranscriptService`~~ **extracted — Milestone 12** | `run_transcript`, `transcript_view`, `transcript_segments`, `save_transcript_edits`, `set_transcript_exclusions`, `restore_transcript_from_asr`, `set_transcript_corrections` | `require_phase`, `require_slot`, `plan_gate` | `ArtifactRepository`, `SourceQuery`, `ApprovalQuery` (state), `RevisionArchiver`, `ProgressFactory`, `ProjectNamespaceStore`, `AuditSink` | done — see `TRANSCRIPT_SERVICE.md`; ASR engine + transcript_edit stay canonical |
| `ScriptService` | `run_script_generation`, script review | `require_phase`, `plan_decision` | `ArtifactRepository`, secrets | provider gateway port |
| `VoiceService` | `run_tts`, voiceover review | `require_phase`, `plan_decision` | `ArtifactRepository`, `MediaBinaries`, `ProgressFactory`, secrets, `ApprovalQuery` (script validity) | provider gateway port |
| `RenderService` | `run_render`, render manifests | `require_phase`, `require_slot`, `plan_decision`, `plan_gate` | `ArtifactRepository`, `MediaBinaries`, `ProgressFactory`, `ApprovalQuery` (chain), `ReviewGate` | largest: EDL, transcript drift, batch board interlock — last |

Each consumes `ports.WorkflowGateQuery` by injection — none needs
`PipelineService`. No new dependency cycle exists: `WorkflowGate` depends on
`ArtifactRepository` alone, and services that would also consume approval
queries already have `ApprovalQuery`/`ApprovalInvalidator`.
