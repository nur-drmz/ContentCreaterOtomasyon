# AnalysisService

> Introduced in Milestone 4, the third application service. It owns Phase 0:
> assessing the attached source and producing/editing the processing plan —
> the decision inputs every later phase is gated on.

## Responsibilities

| Method | Responsibility |
|---|---|
| `assess_source(*, job_id=None, actor="console")` | probe → score both modes → write `source_assessment` → `assessment.created` |
| `get_assessment(*, required=False)` | the assessment artifact record, trustworthiness reported |
| `create_processing_plan(...)` | build the plan from an explicit human mode choice → write → `plan.created` |
| `update_processing_plan(...)` | rebuild from edited fields, reset approval, record invalidation → `plan.updated` |
| `get_processing_plan(*, required=False)` | the plan artifact record, trustworthiness reported |
| `describe_plan_review()` | review descriptor + staleness errors + the plan body |

### Deliberately outside

| Concern | Stays in | Why |
|---|---|---|
| Mode scoring, blockers, plan construction, `validate_processing_plan` | `local_pipeline.source_strategy` | deterministic domain logic; the service orchestrates, never re-derives a score |
| Probing + fingerprinting | `local_pipeline` (`probe_source`, `source_fingerprint_sha256`) | deterministic media logic |
| Full-source analysis, scene detection, transcription, hot-moment ranking, moment selection, script, TTS, render, publish, trend discovery | `PipelineService` | not Phase 0; assessment only READS an existing `hot_moments` artifact if one exists |
| Approval **lifecycle** (granting, state, chain, invalidation records, `APPROVAL_ARTIFACTS`, the rights/checklist preconditions) | `ApprovalService` — reached via `ports.ApprovalInvalidator` | approval semantics; this service only *triggers* the record an edit implies |
| Phase gates (`plan_gate`, `require_phase`, `plan_decision`) | `WorkflowGate` (facade delegates since Milestone 9) | consumed by render/script/TTS and the workflow map, not by analysis |
| Job type, idempotency, leases, retries, cancel requests | `JobStore` / `ConsoleApp` | this service only reports progress and lets `OperationCancelled` propagate |
| Provider router / SDK clients | `local_pipeline` gateways | assessment makes **no** provider call at all |

## Source-assessment workflow

```
AnalysisService.assess_source(job_id, actor)
  │  progress: start("Kaynak degerlendirme basladi")
  ├─ stage "probe"    → SourceQuery.get_source_path()        # raises if unattached/missing
  │                   → MediaBinaries.resolve()              # (ffmpeg, ffprobe)
  │                   → ArtifactRepository.load("hot_moments")   # optional
  ├─ stage "scoring"  → source_strategy.assess_source(...)   # deterministic
  │                      google_credential_configured = secrets.reference("google_api").configured
  ├─ stage "write"    → ArtifactRepository.write("source_assessment")
  │                   → AuditSink.record("assessment.created")
  └─ progress: finish("Oneri: <recommended_mode>")
```

On any exception the tracker records `fail("<Type>: <message>")` and the
exception is **re-raised unchanged** — the façade's error mapping and the job's
recoverable/terminal classification are untouched.

**Advisory-only invariant.** The artifact always carries
`recommendation_is_advisory=True`, `selected_mode=None`, `auto_selected=False`.
Phase 0 can never select or approve a mode; that is a human decision (Phase 0a).

## Processing-plan workflow

```
create_processing_plan(mode, selected_by, ...)
  ├─ require a trustworthy source_assessment (ArtifactProblem when corrupt)
  ├─ render_root = NamespaceStore.resolve(results/render, create_parent=True)
  ├─ source_strategy.build_processing_plan(...)        # deterministic
  ├─ relativise outputs: output_dir / render_manifest_path / final_video_path
  ├─ write processing_plan  →  audit plan.created
  └─ plan starts UNAPPROVED (human_approval.approved = False)

update_processing_plan(updated_by, ...)
  ├─ require trustworthy assessment AND existing plan
  ├─ merge: unspecified fields inherit the old decision
  ├─ REBUILD via build_processing_plan (not a JSON patch) → approval resets
  ├─ revision += 1, updated_by/updated_at set
  ├─ write processing_plan (ONE write, durable first)
  ├─ ApprovalInvalidator.record_invalidation(...)   # emits approval.invalidated
  │   only when the old plan was approved — the approval OWNER records it;
  │   no sibling import, no second artifact write
  └─ audit plan.updated {previous_mode, mode, decision_changed, revision}
```

A rebuild rather than a patch is the invariant that matters: every canonical
validation re-runs, output slots regenerate, and a previously granted approval
cannot carry over to an edited decision. The old approval stays visible in the
append-only audit trail but can never authorize the new plan.

## Artifact authority

| Artifact | Written by | Read by |
|---|---|---|
| `results/source_assessment.json` | **AnalysisService** (only writer) | `SourceService.get_duration`, plan creation/update/staleness, video analysis, workspace view |
| `results/processing_plan.json` | **AnalysisService** (only writer) | phase gates, render, script, TTS, batch slots, approval |
| `results/hot_moments.json` | analysis pipeline (façade) | assessment reads it when present |

Unchanged: logical names, relative paths, `schema_version`, `status`
(`review_required`), `status_reason`, the `source_assessment_sha256` binding in
the plan, the source fingerprint inside the assessment, and all downstream
staleness/invalidation rules.

## Progress and provider boundaries

- **Progress** is `ports.ProgressFactory`, implemented by
  `infrastructure/progress/local/LocalProgressFactory` and constructed from
  `(storage, ownership)` alone. The job runtime binds the cancel signal into
  the factory; the service never holds it. Stages, labels, states and the
  `phase_progress` record format are unchanged.
- **Providers**: none. The only provider-adjacent input is the boolean
  `secrets.reference("google_api").configured`, which decides whether the
  narrated mode is *feasible*. No credential value is read, logged or
  persisted — asserted by
  `test_no_credential_value_reaches_the_artifact`. Credential *alias names*
  (e.g. `google_api_key`) legitimately appear in human-readable blocker text.
- Therefore rate limits, daily quotas, billing blocks, provider fallback and
  safety refusals do not apply to assessment and were not touched.

## Error semantics

| Situation | Behaviour |
|---|---|
| no project artifact | `ArtifactProblem` ("bulunamadi") |
| project without a source | `PipelineServiceError` ("kayitli bir kaynak video yok") |
| source file deleted | `ArtifactProblem` ("manifestte var ama diskte yok") |
| corrupt assessment/plan | `ArtifactProblem` ("gecerli JSON degil") |
| **probe failure** | NOT fatal: `probe_ok=False` + `probe_error`, duration `None` — never a guess |
| plan edit with blank editor | `PipelineServiceError` |
| non-dict `render_config` | `PipelineServiceError` |
| unknown mode | `ValueError` from `source_strategy` |
| cancellation | `OperationCancelled` propagates; progress record stopped |

## Resolved defect: the source-duration fallback (Milestone 8)

`SourceService.get_duration()` reads
`source_assessment.source.duration_sec` as its authority. Its fallback for
projects whose assessment cannot answer was **dead code** in Milestones 3-7:

```python
render_utils.probe_media(str(self.get_source_path()), ffprobe)
# real signature: probe_media(ffmpeg_bin, ffprobe_bin, path)
```

The call was missing an argument, raised `TypeError`, and the surrounding
`except Exception` swallowed it — so the fallback always returned `None`.

**Repaired in Milestone 8.** The fallback now calls

```python
ffmpeg_bin, ffprobe_bin = self._binaries.resolve()
info, _backend = render_utils.probe_media(ffmpeg_bin, ffprobe_bin,
                                          self.get_source_path())
```

and the exception boundary was narrowed to the probe's real failure modes
(`PipelineServiceError`/`ArtifactProblem` for missing source state,
`RuntimeError` for `probe_media`'s documented failure, `OSError` for
unspawnable binaries/unreadable files) — each reporting `None`, while
unexpected programming errors propagate instead of being disguised as missing
duration. See `SOURCE_SERVICE.md` → "The duration fallback" for the full table.

The defect-pinning tests were replaced by regression pins:
- `test_without_an_assessment_the_probe_fallback_runs` — the fallback runs.
- `test_the_fallback_probe_call_uses_the_canonical_signature` — the call site
  passes `(ffmpeg_bin, ffprobe_bin, path)` in the canonical order.
- `tests/test_source_facade.py::TestSourceDuration` — fallback, failure and
  authority behaviour end to end.

### Consumers affected now that `None` became a real duration

| Consumer | Before (always `None`) | After the fix |
|---|---|---|
| `EditorService.update_clip` bounds validation | `set_clip_source_bounds(..., None)` skipped the upper bound | out-of-range clip ends are rejected |
| `clip_bounds_view` / single-clip timeline | scrubber could not scale | timeline renders for assessment-less projects |
| `save_clip_bounds` / `clip_bounds_override` | unbounded | bounded by the probe |
| `build_edl` default duration (callers passing it through) | may persist `0`/absent | real duration |
| any UI showing "source duration" | blank | populated |

`None` is still reachable — a genuine probe failure means *unknown*, and the
pre-fix unbounded behaviour is preserved for exactly that case (pinned by
`test_failed_probe_keeps_duration_unbounded`).

## Dependencies

- `ports.ArtifactRepository` — assessment, plan, `hot_moments`
- `ports.AuditSink` — `assessment.created`, `plan.created`, `plan.updated`
- `ports.ApprovalInvalidator` — `ApprovalService` records the
  `approval.invalidated` event a plan edit implies (Milestone 5; the event
  name and detail shape are approval semantics, so the approval owner emits
  them — the artifact itself is still written once, by this service)
- `ports.ProjectNamespaceStore` — render root + output relativisation
- `ports.SourceQuery` — where the source to assess lives
- `ports.ProgressFactory` — stage reporting and cancellation
- `application.media_binaries.MediaBinaries` — shared lazy ffmpeg/ffprobe resolver
- `secrets` resolver — `reference(kind).configured` only
- `probe_fn` — injected probe so tests score without media (`None` → `render_utils.probe_media`)
- `local_pipeline.source_strategy`, `local_pipeline.approval`

No façade-owned callable, no sibling-service import. `AnalysisService`
**produces** the assessment that `SourceService` later reads, but never calls
`SourceService.get_duration()` — asserted by
`test_duration_authority_does_not_cycle_back_into_analysis`.

## Composition root

```python
class ConsoleApp:
    source_service_cls    = SourceService
    analysis_service_cls  = AnalysisService
    approval_service_cls  = ApprovalService   # wired as `approvals=` (M5)
    review_gate_cls       = LocalReviewGate   # the gate ApprovalService consults
    progress_factory_cls  = LocalProgressFactory
    ...
    progress = self.progress_factory_cls(self.storage, ownership)   # per project
```

`service(ownership)` passes `progress=`, `source_cls=`, `editor_cls=` and
`analysis_cls=` into `PipelineService`, which builds the source service, hands
it to both the editor (as `SourceQuery`) and the analysis service, and forwards
`bind_cancel_check` to the progress factory. A bare
`PipelineService(storage, ownership, secrets)` still constructs every default
itself, so tests and notebooks are unaffected.

## Fault-injection note

`AnalysisService` binds no write function: assessment and plan writes go
through `LocalArtifactRepository` (an already-registered
`atomic_write_json` holder), and `creator_console/progress.py` imports the
writer lazily inside `_write`, so patching resolves at call time. **No new
holder registration was required.** Proven by
`tests/test_analysis_facade.py::TestAnalysisWriteFaultCoverage` (ENOSPC,
permission denied, partial write → next read reports `problem`) and by
`verify_faults.py` 70/70.
