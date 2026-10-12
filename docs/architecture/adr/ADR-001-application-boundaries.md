# ADR-001 — Application Boundary Model

## Status
Accepted

## Context
PROMPT 0 confirmed the real runtime path:

```
apps/web/src/*.jsx
       ↓
services/api/main.py (FastAPI transport)
       ↓
creator_console/api.py (request dispatch)
       ↓
creator_console/pipeline_service.py (console ↔ media façade)
       ↓
local_pipeline/* (deterministic media engine)
```

The target architecture is a layered SaaS boundary. The repository already has the bones of this model, but `turbo_studio.py` and `long_form_studio.py` bypass `PipelineService`, and `PipelineService` itself is 4300+ lines.

## Decision
Adopt a strict layered dependency direction.

| Layer | Permitted imports | Forbidden |
|---|---|---|
| `apps/web/` | none except React/Vite; HTTP calls only | backend, `local_pipeline` |
| `services/api/` | `creator_console.api`, `creator_console.app`, `creator_console.errors` | `local_pipeline`, `pipeline_service` direct calls |
| `creator_console/api.py` | `creator_console.app` / `ConsoleApp` | `local_pipeline` |
| `application/` (new) | `domain/`, `local_pipeline` through `domain` | `creator_console/api.py`, `services/api` |
| `creator_console/pipeline_service.py` (transitional façade) | `local_pipeline`, `creator_console` infrastructure | `apps/web`, `services/api` |
| `domain/` (new) | `local_pipeline` for media policies | HTTP, frontend |
| `local_pipeline/` | standard library only | `creator_console`, `application`, `services/api` |
| `infrastructure/` (new) | none; is called by `application` and `creator_console` | `application` direct import except through adapters |

During migration, existing tests and the Creator Console keep importing `creator_console.pipeline_service.PipelineService`. New application services live under `application/` and are gradually exposed through `creator_console/pipeline_service.py` delegation methods.

**Update (Milestone 2):** the first service now exists — `application/services/editor_service.py` (`EditorService`) owns all EDL/editor orchestration; the façade's `*_edl*` methods are thin delegates. The artifact catalog and the `PipelineServiceError`/`ArtifactProblem` types moved to `application/` so neither the service nor the local artifact adapter needs to import the façade. See `docs/architecture/APPLICATION_SERVICES.md`.

**Update (Milestone 3):** `application/services/source_service.py` (`SourceService`) owns source media; `source_path`, `source_duration_sec`, `source_fingerprint` and `register_source_video*` are now façade delegates. One boundary rule was learned and is now explicit: **a service may not receive a façade-owned callable.** Milestone 2's editor took `source_path`/`source_duration` as bound methods of `PipelineService`, which satisfied every import guard while still being a dependency on the façade. Application services must depend on ports (here `ports.SourceQuery`) or on other application services — never on a function the façade supplies to reach back into itself.

**Update (Milestone 4):** `application/services/analysis_service.py` (`AnalysisService`) owns Phase 0 — `run_source_assessment`, `create_processing_plan`, `update_processing_plan` and `describe_plan_review` are façade delegates.

The façade-owned-callable rule above is now **fully enforced rather than partially excused.** Milestone 3 had itself shipped one violation (`SourceService(tracker_factory=PipelineService._upload_tracker)`), justified as transitional because progress' cancel signal lived on the façade. Milestone 4 removed it by introducing `ports.ProgressFactory` with `LocalProgressFactory`, which owns the cancel signal itself. Two guards enforce the rule, because import analysis cannot see a callable passed as an argument:

- `test_no_application_service_takes_a_facade_owned_callable` — asserts no service constructor accepts `tracker_factory`/`facade`/`pipeline_service`/`source_path`/`source_duration`.
- `test_application_services_do_not_depend_on_each_other_concretely` — services share ports, never import a sibling.

Injected *infrastructure* values remain acceptable and are listed per service: pure helpers (`safe_filename`, `new_id`), test seams (`probe_fn`), and independently-constructed collaborators (`MediaBinaries`, the secret resolver, the progress factory).

**Update (Milestone 5):** `application/services/approval_service.py` (`ApprovalService`) owns the content-approval lifecycle — granting, the rights/checklist preconditions, state, chain validity, the stale sweep and invalidation records. `local_pipeline.approval` stays the canonical digest engine. Two consequences of this extraction sharpen the boundary model:

- **Inter-service collaboration is expressed as a port, never a sibling import.** `AnalysisService` needs the approval owner to *record* an invalidation when a plan edit drops a grant — that dependency is `ports.ApprovalInvalidator` (one method), satisfied structurally by `ApprovalService`. The no-sibling-import guard now covers `approval_service` too.
- **Preconditions an application service must consult are ports, not façade lambdas.** The rights/checklist gate is `ports.ReviewGate`, implemented by `infrastructure/review/local/LocalReviewGate` — an infrastructure adapter wrapping the console checklist modules (same documented transitional direction as `LocalArtifactRepository`), not a `PipelineService` method.

**Update (Milestone 6):** `application/services/publish_service.py` (`PublishService`) owns publication orchestration — metadata, intent, the one private upload, manifest, visibility, credential/OAuth surface. Publication policy stays canonical in `local_pipeline.publish`; the YouTube wire protocol stays in `local_pipeline.publish_youtube`, reached through the injected `publish.PublishProvider` protocol. The single edge back into content approval is `ports.ApprovalQuery` (read-only `chain()`), implemented by `ApprovalService` — not a bound `approval_chain`, not a sibling import. No new gateway port was created: the canonical `PublishProvider` protocol is already the right seam.

**Update (Milestone 9):** `application/services/workflow_gate.py` (`WorkflowGate`) owns plan-derived workflow eligibility — `plan_gate`, `require_phase`, `plan_decision`, and the slot-scope queries — as the boundary future production services consume through `ports.WorkflowGateQuery`. It deliberately did NOT extract the phases themselves: eligibility policy stays canonical in `local_pipeline.source_strategy`, slot arithmetic in `application.slot_policy`, and approval/rights verdicts stay owned by `ApprovalService`/`local_pipeline.approval`. The gate is read-only and depends on `ArtifactRepository` alone.

**Update (Milestone 10):** `application/services/video_analysis_service.py` (`VideoAnalysisService`) owns Phase 1 — `run_analysis` (window + `full_source` scopes) and is the application-level producer of `hot_moments`; `PipelineService.run_video_analysis` is a delegate. It is the first production consumer of `ports.WorkflowGateQuery` (eligibility arrives through the port, never a façade callback) and reuses `ports.SourceQuery` for media access. All algorithms stay canonical in `local_pipeline.video_analysis`; the analysis path makes no provider call, so no secrets or provider port was introduced. `MomentService` (selection) was deliberately left out: `hot_moments` now has an owner, which is exactly the boundary that extraction needs.

**Update (Milestone 11):** `application/services/moment_service.py` (`MomentService`) owns Phase 2 — `record_moment_selection` is the only application writer of `moment_selection`, and the per-slot `clip_bounds` surface (`results/clip_bounds/output_NN.json`) moved with it. It consumes `ports.WorkflowGateQuery` (phase + slot gates), `ports.SourceQuery` (path/duration), `ports.ArtifactRepository` (extended with `write_artifact_path` for the non-catalog clip-bounds document), `ports.AuditSink` and the new **`ports.RevisionArchiver`**: the `.revisions/` snapshot mechanics were shared with rewind/render-profile flows, so they moved to `infrastructure.persistence.local.LocalRevisionArchive` — a real adapter the façade also delegates to — rather than becoming a façade-owned callable. Canonical selection builders and bounds math stay in `local_pipeline`; `VideoAnalysisService` remains the sole `hot_moments` producer; no cycle, no provider, no job.

**Update (Milestone 12):** `application/services/transcript_service.py` (`TranscriptService`) owns the `moment_transcript` lifecycle — `run_transcript` (the only generation path: gate, hot_moments/selection trust check, canonical `render_utils.run_moment_transcription`, correction carry-over, replaced-edit archive, progress lifecycle) and every read/edit surface (`transcript_view`, `transcript_segments`, `save_transcript_edits`, `set_transcript_exclusions`, `restore_transcript_from_asr`, `set_transcript_corrections`). It consumes `ports.WorkflowGateQuery`, `ports.SourceQuery`, `ports.ArtifactRepository`, `ports.RevisionArchiver` (the `action="retranscribe"` snapshot), `ports.ProgressFactory` (`ProgressTracker` extended with `note`), `ports.ProjectNamespaceStore` (the `.runtime/` ASR scratch dir), `ports.AuditSink` and **`ports.ApprovalQuery`, extended with the read-only `state`** for the view's live approval read — still no grant/invalidate capability. Canonical ASR and transcript-edit math stay in `local_pipeline.{render_utils,transcript_edit,glossary}`; `MomentService` remains the sole `moment_selection` owner, `VideoAnalysisService` the sole `hot_moments` producer; no cycle, no provider call, no new `atomic_write_json` binding (every write flows through `LocalArtifactRepository`).

## Consequences
- `local_pipeline` remains pure.
- `services/api` stays a thin transport.
- `creator_console` becomes a backward-compatible wrapper over `application/`.
- `turbo_studio.py` and `long_form_studio.py` are classified as studio drivers, not full application services; see ADR-002.

## Rejected alternatives
- A. Split `PipelineService` first. Rejected because 4300 lines have many callers; blind split breaks tests.
- B. Let `services/api` call `local_pipeline` directly. Rejected; transport must not know media logic.

## Migration path
1. Add `application/` package with interfaces. *(done — Milestone 1)*
2. Move one responsibility to `application/`; keep `PipelineService` method as a thin delegate. *(done — Milestone 2: `EditorService`, not `ProjectService`; the editor was chosen as the most self-contained slice. Milestone 3: `SourceService`, chosen over `ProjectService` because it was what the editor actually depended on. Milestone 4: `AnalysisService`, which also retired the last façade-owned callable)*
3. Move `Storage`, `Audit`, `JobStore` to `infrastructure/` with repository interfaces. *(partially — `LocalArtifactRepository` is a real adapter; storage/audit/jobs remain aliased from `creator_console`)*
4. After all delegations are wired, `PipelineService` shrinks to a compatibility wrapper.
