"""AnalysisService: source assessment (Phase 0) and processing-plan use cases.

WHAT THIS OWNS
--------------
The Phase 0 decision inputs:

  - assessing an attached source into the `source_assessment` artifact,
  - creating the `processing_plan` from an explicit human mode choice,
  - rebuilding that plan when a human edits it (which resets its approval),
  - describing the plan for review, including staleness.

WHAT IT DELIBERATELY DOES NOT OWN
---------------------------------
- **Deterministic scoring and plan construction**: `assess_source`,
  `probe_source`, `build_processing_plan`, `validate_processing_plan` and the
  mode heuristics stay in `local_pipeline.source_strategy`. This service
  orchestrates them; it never re-implements a score, a blocker or a gate.
- **Video analysis**: full-source scanning, scene detection, transcription,
  hot-moment ranking and moment selection stay on the façade. Assessment only
  READS an existing `hot_moments` artifact when one happens to exist (Phase 0
  deliberately runs before Phase 1 is required), so the dependency is one
  artifact read, not the analysis pipeline.
- **Approval lifecycle**: granting, state and chain validity live in
  `ApprovalService`. This service depends only on the narrow
  `ports.ApprovalInvalidator` port, because a plan edit that drops a live
  approval must RECORD the invalidation -- but recording it is approval
  semantics (event name, detail shape, "never delete the original grant"),
  so the approval owner emits it, not this service.
- **Phase gates**: `plan_gate`/`require_phase`/`plan_decision` stay on the
  façade -- they are consumed by render, script, TTS and the workflow map, not
  by analysis.
- **Providers**: assessment makes NO provider call. It reads one boolean
  (`secrets.reference("google_api").configured`) to decide whether the narrated
  mode is feasible. No credential value is ever read, logged or persisted.
- **Job execution**: idempotency, leases, retries and cancellation requests
  remain with the job store; this service only reports progress and lets
  `OperationCancelled` propagate.
"""
from __future__ import annotations

from application.artifact_catalog import RESULTS_DIRNAME
from application.errors import ArtifactProblem, PipelineServiceError
from application.time_utils import now_iso
from local_pipeline import approval, source_strategy


class AnalysisService:
    """Phase 0 use cases for ONE project namespace."""

    def __init__(self, *, artifacts, audit, namespace, ownership, source,
                 binaries, progress, secrets, approvals, probe_fn=None):
        #: `ports.ArtifactRepository` -- assessment, plan and `hot_moments`.
        self._artifacts = artifacts
        #: `ports.AuditSink`.
        self._audit = audit
        #: `ports.ProjectNamespaceStore` -- resolves the render root.
        self._namespace = namespace
        self._ownership = ownership
        #: `ports.SourceQuery` -- where the source to assess lives.
        self._source = source
        #: `application.media_binaries.MediaBinaries`.
        self._binaries = binaries
        #: `ports.ProgressFactory`.
        self._progress = progress
        #: Secret RESOLVER, not a secret: only `.reference(kind).configured`
        #: is consulted, which is a boolean capability flag.
        self._secrets = secrets
        #: `ports.ApprovalInvalidator` -- implemented by `ApprovalService`. The
        #: port exists so this service can trigger the invalidation RECORD
        #: without importing a sibling service (ADR-001) and without owning
        #: approval semantics itself.
        self._approvals = approvals
        #: Injected probe so tests can score without a media file. `None` means
        #: `source_strategy` falls back to `render_utils.probe_media`.
        self._probe_fn = probe_fn

    # ------------------------------------------------------------- queries
    def get_assessment(self, *, required: bool = False) -> dict:
        """The assessment artifact record, with its trustworthiness reported."""
        return self._artifacts.load_artifact("source_assessment", required=required)

    def get_processing_plan(self, *, required: bool = False) -> dict:
        """The plan artifact record, with its trustworthiness reported."""
        return self._artifacts.load_artifact("processing_plan", required=required)

    # ---------------------------------------------------------- assessment
    def assess_source(self, *, job_id=None, actor: str = "console") -> dict:
        tracker = self._progress.create("source_assessment", job_id=job_id)
        tracker.start("Kaynak degerlendirme basladi")
        try:
            return self._assess(tracker, job_id=job_id, actor=actor)
        except Exception as exc:  # noqa: BLE001 - record, then re-raise unchanged
            tracker.fail(f"{type(exc).__name__}: {exc}")
            raise

    def _assess(self, tracker, *, job_id, actor) -> dict:
        tracker.stage("probe", "Medya akislari okunuyor")
        source = self._source.get_source_path()
        ffmpeg_bin, ffprobe_bin = self._binaries.resolve()
        hot = self._artifacts.load_artifact("hot_moments")
        tracker.stage("scoring", "Mod skorlari hesaplaniyor")
        assessment = source_strategy.assess_source(
            source, ffmpeg_bin, ffprobe_bin,
            hot_moments_data=hot["data"] if hot["data"] else None,
            google_credential_configured=self._secrets.reference("google_api").configured,
            probe_fn=self._probe_fn,
        )
        tracker.stage("write", "Degerlendirme yaziliyor")
        relative = self._artifacts.write_artifact(
            "source_assessment", assessment, job_id=job_id, actor=actor)
        self._audit.record("assessment.created", actor=actor, job_id=job_id,
                           artifact=relative,
                           detail={"recommended_mode": assessment["recommended_mode"],
                                   "auto_selected": assessment["auto_selected"]})
        tracker.finish(message=f"Oneri: {assessment['recommended_mode']}")
        return assessment

    # --------------------------------------------------------------- plan
    def create_processing_plan(self, *, mode: str, selected_by: str,
                               output_count=None, target_duration_sec=None,
                               moment_ranks=None, theory_angle: str = "",
                               theory_angle_auto: bool = False,
                               render_config=None, notes: str = "",
                               content_language=None,
                               narration_preset=None) -> dict:
        assessment = self._artifacts.load_artifact("source_assessment", required=True)
        if assessment["problem"]:
            raise ArtifactProblem(assessment["problem"])
        plan = source_strategy.build_processing_plan(
            assessment["data"], mode, selected_by, output_count=output_count,
            target_duration_sec=target_duration_sec, moment_ranks=moment_ranks,
            theory_angle=theory_angle, theory_angle_auto=theory_angle_auto,
            render_config=render_config,
            content_language=content_language,
            narration_preset=narration_preset,
            render_root=self._render_root(), notes=notes,
        )
        self._relativise_outputs(plan)
        relative = self._artifacts.write_artifact("processing_plan", plan,
                                                  actor=selected_by)
        self._audit.record("plan.created", actor=selected_by, artifact=relative,
                           detail={"mode": mode,
                                   "output_count": plan["decision"]["output_count"],
                                   "followed_recommendation":
                                       plan["followed_recommendation"]})
        return plan

    def update_processing_plan(self, *, updated_by: str, mode=None,
                               selected_by=None, output_count=None,
                               target_duration_sec=None, moment_ranks=None,
                               theory_angle=None, theory_angle_auto=None,
                               render_config=None, notes=None,
                               content_language=None,
                               narration_preset=None) -> dict:
        """Rebuild an existing plan from editable, validated Phase 0 fields.

        A plan edit is intentionally not an in-place JSON patch. Rebuilding via
        ``source_strategy.build_processing_plan`` re-applies every canonical
        validation, regenerates output slots, and resets human approval. The
        previous approval remains visible in the append-only audit trail, but
        can never authorize the edited decision.
        """
        assessment = self._artifacts.load_artifact("source_assessment", required=True)
        current = self._artifacts.load_artifact("processing_plan", required=True)
        if assessment["problem"]:
            raise ArtifactProblem(assessment["problem"])
        if current["problem"]:
            raise ArtifactProblem(current["problem"])
        if not current["data"]:
            raise PipelineServiceError("duzenlenecek isleme plani bulunamadi")

        old_plan = current["data"]
        old_decision = old_plan.get("decision") or {}
        actor = str(updated_by or "").strip()
        if not actor:
            raise PipelineServiceError("plani duzenleyen kisi bos olamaz")

        chosen_mode = mode or old_decision.get("mode")
        chosen_by = (str(selected_by).strip() if selected_by is not None
                     else str(old_plan.get("selected_by") or actor).strip())
        chosen_count = (output_count if output_count is not None
                        else old_decision.get("output_count"))
        chosen_duration = (target_duration_sec if target_duration_sec is not None
                           else old_decision.get("target_duration_sec"))
        chosen_ranks = (moment_ranks if moment_ranks is not None
                        else old_decision.get("moment_ranks") or [])
        chosen_angle = (theory_angle if theory_angle is not None
                        else old_decision.get("theory_angle") or "")
        chosen_auto_angle = (bool(theory_angle_auto) if theory_angle_auto is not None
                             else bool(old_decision.get("theory_angle_auto")))
        # Typing an angle is an explicit choice to stop deriving one; keeping
        # both would hit the server's "not both" refusal on every later edit.
        if chosen_angle and chosen_auto_angle and theory_angle:
            chosen_auto_angle = False
        if chosen_auto_angle:
            chosen_angle = ""
        chosen_notes = notes if notes is not None else old_plan.get("notes") or ""
        chosen_language = (content_language if content_language is not None
                           else old_decision.get("content_language"))
        chosen_preset = (narration_preset if narration_preset is not None
                         else (old_decision.get("narration") or {}).get("preset"))

        # Partial render edits preserve settings the current UI does not expose.
        chosen_render = dict(old_decision.get("render") or {})
        if render_config is not None:
            if not isinstance(render_config, dict):
                raise PipelineServiceError("render_config JSON nesnesi olmali")
            chosen_render.update(render_config)

        rebuilt = source_strategy.build_processing_plan(
            assessment["data"], chosen_mode, chosen_by,
            output_count=chosen_count,
            target_duration_sec=chosen_duration,
            moment_ranks=chosen_ranks,
            theory_angle=chosen_angle,
            content_language=chosen_language,
            narration_preset=chosen_preset,
            render_config=chosen_render,
            narration_config=None,
            render_root=self._render_root(),
            notes=chosen_notes,
            selected_at=old_plan.get("selected_at"),
        )
        self._relativise_outputs(rebuilt)

        old_hash = approval.content_hash_for("processing_plan", old_plan)
        new_hash = approval.content_hash_for("processing_plan", rebuilt)
        rebuilt["revision"] = int(old_plan.get("revision") or 1) + 1
        rebuilt["updated_by"] = actor[:120]
        rebuilt["updated_at"] = now_iso()
        relative = self._artifacts.write_artifact("processing_plan", rebuilt,
                                                  actor=actor)

        self._approvals.record_invalidation(
            "processing_plan", artifact=relative, actor=actor,
            reason="plan edited",
            previous_approval=old_plan.get("human_approval"))
        self._audit.record(
            "plan.updated", actor=actor, artifact=relative,
            detail={"previous_mode": old_decision.get("mode"),
                    "mode": chosen_mode,
                    "decision_changed": old_hash != new_hash,
                    "revision": rebuilt["revision"]},
        )
        return rebuilt

    def describe_plan_review(self) -> dict:
        plan = self._artifacts.load_artifact("processing_plan")
        if plan["problem"]:
            raise ArtifactProblem(plan["problem"])
        if not plan["data"]:
            return {"present": False}
        info = approval.describe_for_review("processing_plan", plan["data"])
        assessment = self._artifacts.load_artifact("source_assessment")
        errors = source_strategy.validate_processing_plan(
            plan["data"], assessment=assessment["data"] if assessment["data"] else None,
        )
        return {"present": True, "review": info, "staleness": errors,
                "plan": plan["data"]}

    # ---------------------------------------------------------- internals
    def _render_root(self):
        return self._namespace.resolve(self._ownership, RESULTS_DIRNAME, "render",
                                       create_parent=True)

    def _relativise_outputs(self, plan) -> None:
        """Store namespace-relative output dirs so the artifact never carries
        the operator's absolute filesystem layout."""
        for slot in plan["outputs"]:
            for field in ("output_dir", "render_manifest_path", "final_video_path"):
                slot[field] = self._namespace.relative_to_namespace(
                    self._ownership, slot[field])
