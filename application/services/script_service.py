"""ScriptService -- the narrated-mode boundary for the spoken text.

Responsibilities extracted from the console façade (Milestone 13):

* ``run_script_generation`` -- the ONLY generation writer of
  ``selected_script``: the phase gate, the hot_moments + moment_selection
  trust check (``validate_moment_selection(require_preview=True)`` -- a
  script is written ABOUT a moment a human actually watched), the canonical
  ``local_pipeline.script_generation.run_script_generation`` engine call with
  the plan's decision inputs, the plan-derived angle/language stamping, the
  artifact write, the ``provider.call`` audit event and the progress
  lifecycle.
* ``update_script_text`` -- the ONLY human-edit writer: validation, the
  unchanged-text no-op, the `voice`-stage revision snapshot that invalidates
  every downstream artifact, the approval reset, the `manual_edits` history
  and the ``script.updated`` audit event.

What deliberately STAYS outside:

* **Canonical generation** -- prompt building, provider candidate
  construction/routing, response parsing, artifact construction and the
  one-paid-call cost contract all live in
  ``local_pipeline.script_generation`` (and, for Google, in the
  ``local_pipeline.google_ai`` router). This service orchestrates them and
  rewrites nothing.
* **``moment_selection`` / ``hot_moments`` ownership** -- MomentService and
  VideoAnalysisService; this service only READS them.
* **Approval** -- ApprovalService owns grants. Generated script is always
  ``review_required``; an edit drops a grant by resetting the block the
  content hash binds to, never by writing approval state.
* **TTS and render** -- a script mutation archives their artifacts (so they
  are re-derived deliberately) but never starts them.
* **The review-screen projection** -- ``_script_review_view`` stays in
  ``creator_console.app``: it formats already-owned state for one screen and
  carries no script-domain rule.
* **Transcript and glossary** -- NOT inputs to script generation in this
  system, and this service does not read or write either.
"""
from __future__ import annotations

from application import artifact_catalog
from application.errors import ArtifactProblem, PipelineServiceError
from application.time_utils import now_iso
from local_pipeline import approval, google_ai, script_generation, video_analysis

ARTIFACTS = artifact_catalog.ARTIFACTS
REWIND_PATHS = artifact_catalog.REWIND_PATHS

#: Longest script text a human edit may submit. Preserved verbatim from the
#: façade: a caption-length guard, not a model limit.
MAX_SCRIPT_CHARS = 20000

#: How many manual edits stay in the artifact's own history.
MAX_MANUAL_EDITS = 50


class ScriptService:
    """Script generation + human-edit ownership for `selected_script`."""

    def __init__(self, *, artifacts, workflow, archiver, progress, audit,
                 secrets, google_client_factory=None,
                 openai_client_factory=None, anthropic_client_factory=None):
        #: ports.ArtifactRepository -- `hot_moments`/`moment_selection`/
        #: `processing_plan` reads and the `selected_script` read/write.
        self._artifacts = artifacts
        #: ports.WorkflowGateQuery -- `require_phase("script_generation")`;
        #: direct/original-audio modes are denied here, not by a local check.
        self._workflow = workflow
        #: ports.RevisionArchiver -- the `voice`-stage snapshot a human edit
        #: takes. The MECHANICS are the adapter's; WHICH stage is invalidated
        #: is this service's decision.
        self._archiver = archiver
        #: ports.ProgressFactory -- the `script_generation` activity tracker,
        #: with cancellation bound by the job runtime.
        self._progress = progress
        #: ports.AuditSink -- `provider.call` and `script.updated`.
        self._audit = audit
        #: The secret resolver. Only `credential("google_api")` is read, only
        #: to build the client; the value never enters an artifact or event.
        self._secrets = secrets
        #: Injected client factories -- the established provider seam (same
        #: shape AnalysisService/PublishService use). Nothing here constructs
        #: a real SDK client unless a credential exists, so a test can
        #: guarantee zero network access.
        self._google_client_factory = google_client_factory
        self._openai_client_factory = openai_client_factory
        self._anthropic_client_factory = anthropic_client_factory

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _call(factory):
        return factory() if callable(factory) else None

    def _build_google_client(self):
        if callable(self._google_client_factory):
            return self._google_client_factory()
        credential = self._secrets.credential("google_api")
        if not credential.configured:
            return None
        return google_ai.build_google_client(credential)

    # ------------------------------------------------------------ generation
    def run_script_generation(self, *, job_id=None, actor="console",
                              enable_google_model_fallback: bool = True,
                              max_google_model_attempts: int = 4,
                              max_google_transient_retries: int = 2) -> dict:
        """Defaults keep the normal Route A behaviour. The three knobs exist so a
        deliberate single-call validation can pin the request to exactly one
        model, one attempt, zero retries."""
        self._workflow.require_phase("script_generation")
        tracker = self._progress.create("script_generation", job_id=job_id)
        tracker.start("Script uretimi basladi")
        try:
            return self._run_script_generation(
                tracker, job_id=job_id, actor=actor,
                enable_google_model_fallback=enable_google_model_fallback,
                max_google_model_attempts=max_google_model_attempts,
                max_google_transient_retries=max_google_transient_retries)
        except Exception as exc:  # noqa: BLE001
            tracker.fail(f"{type(exc).__name__}: {exc}")
            raise

    def _run_script_generation(self, tracker, *, job_id, actor,
                               enable_google_model_fallback,
                               max_google_model_attempts,
                               max_google_transient_retries) -> dict:
        tracker.stage("preparation", "Moment secimi dogrulaniyor")
        hot = self._artifacts.load_artifact("hot_moments", required=True)
        selection = self._artifacts.load_artifact("moment_selection",
                                                  required=True)
        for loaded in (hot, selection):
            if loaded["problem"]:
                raise ArtifactProblem(loaded["problem"])
        errors = video_analysis.validate_moment_selection(
            selection["data"], hot["data"], require_preview=True)
        if errors:
            raise PipelineServiceError(f"moment secimi gecersiz/bayat: {errors}")

        plan = self._artifacts.load_artifact("processing_plan")["data"] or {}
        decision = plan.get("decision") or {}
        google_client = self._build_google_client()
        tracker.stage("request", "Gemini yaniti bekleniyor")
        result = script_generation.run_script_generation(
            hot["data"],
            # `artifact_relative` is the declared port surface and is
            # byte-identical to the façade's `relative(path("hot_moments"))`
            # for this project-wide artifact (asserted in the parity tests).
            self._artifacts.artifact_relative("hot_moments"),
            selected_moment_rank=selection["data"]["selected_moment_rank"],
            google_client=google_client,
            openai_client=self._call(self._openai_client_factory),
            anthropic_client=self._call(self._anthropic_client_factory),
            target_duration_sec=decision.get("target_duration_sec", 60),
            moment_approved=True,
            moment_approved_by=selection["data"]["reviewed_by"],
            script_provider="google", allow_provider_fallback=False,
            content_language=decision.get("content_language"),
            theory_angle=decision.get("theory_angle") or "",
            derive_theory_angle=bool(decision.get("theory_angle_auto")),
            enable_google_model_fallback=enable_google_model_fallback,
            max_google_model_attempts=max_google_model_attempts,
            max_google_transient_retries=max_google_transient_retries,
        )
        tracker.stage("parse", "Yanit ayristirildi")
        # A derived angle is the one the script was actually written to, so it
        # wins over the plan's empty field. Which of the two it is stays visible
        # in `theory_angle_source`.
        result["theory_angle"] = (result.get("theory_angle")
                                  or decision.get("theory_angle"))
        result["theory_angle_auto"] = bool(decision.get("theory_angle_auto"))
        result["content_language"] = decision.get("content_language")
        tracker.stage("write", "Script artefakti yaziliyor")
        self._artifacts.write_artifact("selected_script", result,
                                       job_id=job_id, actor=actor)
        self._audit.record("provider.call", actor=actor, job_id=job_id,
                           detail={"phase": "script_generation",
                                   "provider": result.get("provider"),
                                   "model": result.get("model")})
        if result.get("status") == "failed":
            tracker.fail(result.get("status_reason") or "script uretilemedi")
        else:
            tracker.finish(message=f"{result.get('model') or 'model'} · "
                                   f"{len(result.get('script_text') or '')} karakter")
        return result

    # ----------------------------------------------------------- human edit
    def update_script_text(self, *, script_text: str, updated_by: str,
                           notes: str = "") -> dict:
        """Human-edit a generated script and archive audio/render descendants."""
        clean = str(script_text or "").strip()
        who = str(updated_by or "").strip()
        if not who:
            raise PipelineServiceError("scripti duzenleyen kisi zorunlu")
        if not clean:
            raise PipelineServiceError("script metni bos olamaz")
        if len(clean) > MAX_SCRIPT_CHARS:
            raise PipelineServiceError(
                f"script metni en fazla {MAX_SCRIPT_CHARS} karakter olabilir")
        loaded = self._artifacts.load_artifact("selected_script", required=True)
        if loaded["problem"]:
            raise ArtifactProblem(loaded["problem"])
        artifact = loaded["data"]
        old_hash = approval.content_hash_for("script", artifact)
        if clean == artifact.get("script_text"):
            return artifact
        downstream = tuple(path for path in REWIND_PATHS["voice"])
        revision = self._archiver.archive_revision(
            downstream, action="script_edit", actor=who, target="script")
        artifact["script_text"] = clean
        artifact["status"] = "review_required"
        artifact["status_reason"] = (
            "script insan tarafindan duzenlendi; yeniden inceleme ve onay bekleniyor")
        artifact["human_approval"] = {
            "approved": False, "approved_by": None, "approved_at": None,
            "notes": None, "approved_content_sha256": None,
        }
        artifact.setdefault("manual_edits", []).insert(0, {
            "updated_by": who[:120], "updated_at": now_iso(),
            "notes": str(notes or "").strip()[:1000] or None,
            "previous_script_sha256": old_hash,
            "revision_id": revision["revision_id"],
        })
        artifact["manual_edits"] = artifact["manual_edits"][:MAX_MANUAL_EDITS]
        self._artifacts.write_artifact("selected_script", artifact, actor=who)
        self._audit.record("script.updated", actor=who,
                           artifact=ARTIFACTS["selected_script"], detail={
                               "previous_script_sha256": old_hash,
                               "revision_id": revision["revision_id"],
                               "downstream_archived": revision["moved_paths"],
                           })
        return artifact
