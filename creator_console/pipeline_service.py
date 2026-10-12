"""The ONLY place the console calls `local_pipeline`.

WHY THIS EXISTS
---------------
The API layer must not know how a hot moment is scored, and the browser must not
know that `local_pipeline` exists at all. Every pipeline entry point is wrapped
here exactly once, so:

  - notebook logic is never duplicated into the frontend or the router,
  - provider clients are INJECTED (a factory), so tests use doubles and no test
    can accidentally reach a real endpoint,
  - artifacts are read through `load_artifact()`, which reports corruption
    instead of returning a plausible-looking default,
  - every write is atomic and namespace-scoped,
  - approvals go through the existing hash-bound `local_pipeline.approval` gates,
    never through a boolean in a request body.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from local_pipeline import (
    approval,
    glossary as glossary_module,
    google_ai,
    publish as publish_module,
    render_utils,
    scene_editor,
    script_generation,
    source_strategy,
    subtitle_edit,
    trend_ingest,
    tts as tts_module,
    video_analysis,
)

from application import artifact_catalog, project_record
from application.approval_catalog import APPROVAL_ARTIFACTS as _APPROVAL_ARTIFACTS
from application.errors import ArtifactProblem, PipelineServiceError
from application.media_binaries import MediaBinaries
from application.services import (
    AnalysisService, ApprovalService, EditorService, MomentService,
    PublishService, ScriptService, SourceService, TranscriptService,
    VideoAnalysisService, WorkflowGate)
from application.services.video_analysis_service import (
    FULL_SOURCE_ANALYSIS_STAGES)
from infrastructure.persistence.local import (
    LocalArtifactRepository, LocalRevisionArchive)
from infrastructure.progress.local import LocalProgressFactory
from infrastructure.review.local import LocalReviewGate

from .audit import AuditLog
from . import copyright_review as copyright_review_module
from . import render_profile as render_profile_module
from . import render_review as render_review_module
from . import voice_review as voice_review_module
from .ids import Ownership, new_id, safe_filename
from .storage import StorageViolation, atomic_write_json

# The artifact catalog (names, paths, required fields, schema versions and the
# per-slot layout policy) lives in `application.artifact_catalog` so the local
# artifact repository can share it without depending on this facade. These
# re-exports keep `pipeline_service.ARTIFACTS` and friends working for every
# existing caller and verify script.
ARTIFACTS = artifact_catalog.ARTIFACTS
PER_SLOT_ARTIFACTS = artifact_catalog.PER_SLOT_ARTIFACTS
REQUIRED_FIELDS = artifact_catalog.REQUIRED_FIELDS
SUPPORTED_SCHEMA_VERSIONS = artifact_catalog.SUPPORTED_SCHEMA_VERSIONS
PRODUCTION_READY_ALLOWED = artifact_catalog.PRODUCTION_READY_ALLOWED
RESULTS_DIRNAME = artifact_catalog.RESULTS_DIRNAME

# `FULL_SOURCE_ANALYSIS_STAGES` moved into
# `application.services.video_analysis_service` with the analysis ownership;
# the import above keeps `pipeline_service.FULL_SOURCE_ANALYSIS_STAGES`
# resolving for every existing caller.

SOURCE_DIRNAME = artifact_catalog.SOURCE_DIRNAME
RUNTIME_DIRNAME = artifact_catalog.RUNTIME_DIRNAME

# Keep upload validation on the exact same allow-list used by the analysis
# resolver.  MKV is a source container: browsers do not reliably play it, but
# FFmpeg reads it and the pipeline emits browser-compatible MP4 previews.
SUPPORTED_SOURCE_VIDEO_EXTENSIONS = frozenset(
    video_analysis.SUPPORTED_SOURCE_VIDEO_EXTENSIONS
)


def is_supported_source_video_filename(filename: str) -> bool:
    """Return whether an untrusted upload name has a supported video suffix."""
    display = safe_filename(filename, default="source")
    return Path(display).suffix.lower() in SUPPORTED_SOURCE_VIDEO_EXTENSIONS

#: Rewind path policy lives in the neutral `application.artifact_catalog`
#: so the façade, `ScriptService` (whose human edit archives the `voice`
#: stage) and the subtitle path share ONE definition without an import
#: edge between them. Re-exported here: `pipeline_service.REWIND_PATHS`
#: is a compatibility surface (`verify_revision_flow.py` imports it).
REWIND_PATHS = artifact_catalog.REWIND_PATHS

REWIND_LABELS = {
    "assessment": "Faz 0 degerlendirme",
    "plan": "isleme plani",
    "analysis": "video analizi",
    "moment": "moment secimi",
    "script": "script",
    "voice": "seslendirme",
    "render": "render",
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class PipelineService:
    """Ownership-scoped facade over the canonical pipeline modules.

    The EDL/editor use cases live in `application.services.EditorService`; the
    matching public methods below are thin delegates so every existing caller
    keeps working unchanged.
    """

    def __init__(self, storage, ownership: Ownership, secret_resolver, *,
                 google_client_factory=None, openai_client_factory=None,
                 anthropic_client_factory=None, elevenlabs_client_factory=None,
                 probe_fn=None, ffmpeg_bin=None, ffprobe_bin=None, audit: AuditLog | None = None,
                 youtube_provider=None, music_root=None,
                 artifacts=None, editor=None, editor_cls=None,
                 source=None, source_cls=None, binaries=None,
                 analysis=None, analysis_cls=None, progress=None,
                 approval=None, approval_cls=None,
                 review_gate=None, review_gate_cls=None,
                 publish=None, publish_cls=None,
                 workflow=None, workflow_cls=None,
                 video_analysis=None, video_analysis_cls=None,
                 moment=None, moment_cls=None, revision_archive=None,
                 transcript=None, transcript_cls=None,
                 script=None, script_cls=None):
        self.storage = storage
        self.ownership = ownership
        self.secrets = secret_resolver
        self.audit = audit or AuditLog(storage, ownership)
        self._cancel_check = None
        # Progress reporting. Independently constructed (it needs only storage +
        # ownership), so application services depend on the port rather than on
        # a facade-owned tracker callable. The job runtime binds the cancel
        # signal into it via `bind_cancel_check`.
        self._progress = progress or LocalProgressFactory(storage, ownership)
        # Persistence/application adapters. The composition root (`ConsoleApp`)
        # normally injects these; the defaults keep
        # `PipelineService(storage, ownership, secrets)` constructible on its
        # own for tests and notebooks. The local artifact repository is the
        # real implementation -- the artifact methods below delegate to it --
        # so the dependency runs facade -> service -> adapter, never back.
        if artifacts is None:
            artifacts = LocalArtifactRepository(storage, ownership, self.audit)
        self._artifacts = artifacts
        # One shared binary resolver: the facade's `binaries()` and the source
        # service must agree, and resolution must happen at most once.
        self._binaries = binaries or MediaBinaries(ffmpeg_bin, ffprobe_bin)
        # Workflow eligibility + slot scope -- the read-only interpreter of
        # `processing_plan`. Built early because it depends on nothing but the
        # artifact repository; future production services receive it through
        # `ports.WorkflowGateQuery`, never through this facade.
        self.workflow_gate = workflow or (workflow_cls or WorkflowGate)(
            artifacts=self._artifacts)
        if source is None:
            source = (source_cls or SourceService)(
                artifacts=self._artifacts,
                audit=self.audit,
                namespace=storage,
                ownership=ownership,
                binaries=self._binaries,
                safe_filename=safe_filename,
                new_id=new_id,
                progress=self._progress,
            )
        self.source = source
        # Human-review preconditions (rights + checklists) are read through the
        # review gate -- an infrastructure adapter, because it wraps the console
        # review modules. ApprovalService consults it; the checklist-mutation
        # methods below reuse it too, so "bound to the current digest" has one
        # definition.
        self._review_gate = review_gate or (review_gate_cls or LocalReviewGate)(
            artifacts=self._artifacts,
            ownership=ownership,
            source=self.source,
        )
        if approval is None:
            approval = (approval_cls or ApprovalService)(
                artifacts=self._artifacts,
                audit=self.audit,
                ownership=ownership,
                review_gate=self._review_gate,
            )
        self.approval = approval
        if analysis is None:
            analysis = (analysis_cls or AnalysisService)(
                artifacts=self._artifacts,
                audit=self.audit,
                namespace=storage,
                ownership=ownership,
                source=self.source,
                binaries=self._binaries,
                progress=self._progress,
                secrets=secret_resolver,
                approvals=self.approval,
                probe_fn=probe_fn,
            )
        self.analysis = analysis
        if editor is None:
            # The editor receives the narrow read-only source contract, never
            # this facade: `ports.SourceQuery`, satisfied by `SourceService`.
            editor = (editor_cls or EditorService)(
                artifacts=self._artifacts,
                audit=self.audit,
                namespace=storage,
                ownership=ownership,
                source=self.source,
            )
        self.editor = editor
        if publish is None:
            # Publication orchestration. The provider is INJECTED (an already
            # built PublishProvider) or constructed lazily inside the service
            # from the namespace's OAuth token path -- never a facade-owned
            # callable. `approvals` is the read-only `ports.ApprovalQuery` view
            # the publish gate consults; ApprovalService satisfies it.
            publish = (publish_cls or PublishService)(
                artifacts=self._artifacts,
                audit=self.audit,
                namespace=storage,
                ownership=ownership,
                progress=self._progress,
                approvals=self.approval,
                secrets=secret_resolver,
                provider=youtube_provider,
                google_client_factory=google_client_factory,
            )
        self.publish = publish
        if video_analysis is None:
            # Phase 1 analysis orchestration. The gate arrives through
            # `ports.WorkflowGateQuery`, the source through `ports.SourceQuery`
            # -- never as callbacks bound to this facade.
            video_analysis = (video_analysis_cls or VideoAnalysisService)(
                artifacts=self._artifacts,
                namespace=storage,
                ownership=ownership,
                source=self.source,
                binaries=self._binaries,
                progress=self._progress,
                workflow=self.workflow_gate,
            )
        self.video_analysis = video_analysis
        # `.revisions/` snapshot mechanics -- the shared invalidation primitive
        # used by rewind, render-profile edits, review flows and clip-bounds
        # changes. One implementation behind `ports.RevisionArchiver`; the
        # facade's `_archive_revision`/`_render_slot_archive_paths` delegate to
        # it so MomentService never receives a facade-owned callable.
        self.revision_archive = revision_archive or LocalRevisionArchive(
            namespace=storage, ownership=ownership)
        if moment is None:
            # Phase 2 moment selection + clip-bounds ownership. The gate arrives
            # through `ports.WorkflowGateQuery`, the source through
            # `ports.SourceQuery`, the invalidation through
            # `ports.RevisionArchiver` -- never as facade callbacks.
            moment = (moment_cls or MomentService)(
                artifacts=self._artifacts,
                source=self.source,
                workflow=self.workflow_gate,
                archiver=self.revision_archive,
                audit=self.audit,
            )
        self.moment = moment
        if transcript is None:
            # Transcript lifecycle ownership. The gate arrives through
            # `ports.WorkflowGateQuery`, approval reads through the read-only
            # `ports.ApprovalQuery`, the replacement archive through
            # `ports.RevisionArchiver` and progress through
            # `ports.ProgressFactory` -- never as facade callbacks.
            transcript = (transcript_cls or TranscriptService)(
                artifacts=self._artifacts,
                source=self.source,
                workflow=self.workflow_gate,
                approvals=self.approval,
                archiver=self.revision_archive,
                progress=self._progress,
                namespace=storage,
                ownership=ownership,
                audit=self.audit,
            )
        self.transcript = transcript
        if script is None:
            # Narrated-mode script lifecycle. The gate arrives through
            # `ports.WorkflowGateQuery`, the downstream invalidation through
            # `ports.RevisionArchiver`, progress through
            # `ports.ProgressFactory`; the provider clients arrive as the
            # same injected factories this facade received -- never as a
            # facade-owned lambda.
            script = (script_cls or ScriptService)(
                artifacts=self._artifacts,
                workflow=self.workflow_gate,
                archiver=self.revision_archive,
                progress=self._progress,
                audit=self.audit,
                secrets=secret_resolver,
                google_client_factory=google_client_factory,
                openai_client_factory=openai_client_factory,
                anthropic_client_factory=anthropic_client_factory,
            )
        self.script = script
        # Injected factories: nothing in this class constructs a real SDK client
        # on its own, so a test can guarantee zero network access.
        self._google_client_factory = google_client_factory
        self._openai_client_factory = openai_client_factory
        self._anthropic_client_factory = anthropic_client_factory
        self._elevenlabs_client_factory = elevenlabs_client_factory
        self._probe_fn = probe_fn
        # Injected like every other environment fact, rather than read
        # from a global config: this class must stay constructible in a
        # test without an app around it.
        self.music_root = music_root

    def bind_cancel_check(self, callback) -> None:
        """Bind the current job's ownership-scoped cooperative stop signal.

        Forwarded to the progress factory so trackers built by application
        services observe the same signal: cancellation must not depend on which
        layer created the tracker.
        """
        self._cancel_check = callback
        binder = getattr(self._progress, "bind_cancel_check", None)
        if callable(binder):
            binder(callback)

    # ------------------------------------------------------------------ paths
    def tracker(self, activity: str, *, job_id=None, attempt: int = 1, stages=None):
        """A progress writer for one activity.

        Kept public for the many façade phases that still report progress
        directly; the real implementation is the injected `ports.ProgressFactory`.
        """
        return self._progress.create(activity, job_id=job_id, attempt=attempt,
                                     stages=stages)

    def artifact_relative(self, key: str, slot: int = 1) -> str:
        """Namespace-relative path of an artifact FOR ONE OUTPUT SLOT."""
        return self._artifacts.artifact_relative(key, slot)

    def path(self, key: str, slot: int = 1) -> Path:
        return self._artifacts.path(key, slot)

    def relative(self, path) -> str:
        return self._artifacts.relative(path)

    def binaries(self):
        """The ffmpeg/ffprobe pair, resolved lazily and cached once."""
        return self._binaries.resolve()

    # -------------------------------------------------------------- artifacts
    # The real implementation is `LocalArtifactRepository` (injected as
    # `self._artifacts`); these methods stay so every existing caller keeps
    # working. See `docs/architecture/APPLICATION_SERVICES.md`.
    def load_artifact(self, key: str, *, required: bool = False, slot: int = 1) -> dict:
        """Read one artifact and REPORT its trustworthiness.

        Returns {'present','data','problem','relative_path'}. A corrupt or
        incompatible artifact yields `data=None` and a human-actionable
        `problem` -- it is never repaired, guessed at, or replaced by a default.
        """
        return self._artifacts.load_artifact(key, required=required, slot=slot)

    def load_artifact_path(self, relative: str):
        """Read one JSON artifact by RELATIVE path (not a registered key)."""
        return self._artifacts.load_artifact_path(relative)

    def write_artifact(self, key: str, data, *, job_id=None, actor="console", slot: int = 1) -> str:
        return self._artifacts.write_artifact(key, data, job_id=job_id,
                                              actor=actor, slot=slot)

    def write_artifact_path(self, relative: str, data) -> str:
        """Atomically write a non-catalog JSON document by relative path.

        The clip_bounds files moved behind MomentService use this primitive;
        the delegate keeps `PipelineService` satisfying `ports.ArtifactRepository`.
        """
        return self._artifacts.write_artifact_path(relative, data)

    # ---------------------------------------------------------------- project
    def create_project(self, *, title: str, created_by: str,
                       operator_name: str | None = None) -> dict:
        # Shared with `SourceService`, which must bootstrap a project when an
        # upload arrives before one exists. One implementation, so the record
        # shape and the `project.created` event cannot drift apart.
        return project_record.create_project(
            artifacts=self._artifacts, audit=self.audit, namespace=self.storage,
            ownership=self.ownership, title=title, created_by=created_by,
            operator_name=operator_name)

    def project_actor(self) -> str:
        """Return the one persisted human name used throughout this project.

        Legacy projects predate ``operator_name``. Recover a name that the user
        already entered in an earlier decision instead of asking again.
        """
        project = self.load_artifact("project")
        record = project.get("data") or {}
        candidates = [record.get("operator_name"), record.get("created_by")]
        plan = self.load_artifact("processing_plan").get("data") or {}
        candidates.extend([plan.get("updated_by"), plan.get("selected_by")])
        selection = self.load_artifact("moment_selection").get("data") or {}
        candidates.append(selection.get("reviewed_by"))
        for value in candidates:
            clean = str(value or "").strip()
            if clean and clean.lower() not in {"console", "console:manual", "system"}:
                return clean[:120]
        return "console"

    def update_project(self, *, title: str, notes: str = "", updated_by: str = "console",
                       operator_name: str | None = None) -> dict:
        """Edit user-facing project metadata without touching pipeline inputs.

        Title/notes are deliberately outside approval hashes: renaming an
        archive entry must not invalidate a reviewed source, plan or render.
        Source identity and every pipeline artifact remain byte-for-byte intact.
        """
        loaded = self.load_artifact("project", required=True)
        if loaded["problem"]:
            raise ArtifactProblem(loaded["problem"])
        clean_title = str(title or "").strip()
        clean_notes = str(notes or "").strip()
        if not clean_title:
            raise PipelineServiceError("proje adi bos olamaz")
        if len(clean_title) > 200:
            raise PipelineServiceError("proje adi en fazla 200 karakter olabilir")
        if len(clean_notes) > 2000:
            raise PipelineServiceError("proje notu en fazla 2000 karakter olabilir")
        clean_operator = (str(operator_name).strip()[:120]
                          if operator_name is not None else None)
        if operator_name is not None and not clean_operator:
            raise PipelineServiceError("proje ureticisi bos olamaz")

        record = loaded["data"]
        previous_title = record.get("title")
        record.update({
            "title": clean_title,
            "notes": clean_notes,
            "updated_by": str(updated_by or "console")[:120],
            "updated_at": _now_iso(),
        })
        if clean_operator is not None:
            record["operator_name"] = clean_operator
        self.write_artifact("project", record, actor=updated_by)
        self.audit.record(
            "project.updated",
            actor=updated_by,
            detail={"previous_title": previous_title, "title": clean_title,
                    "notes_present": bool(clean_notes)},
        )
        return record

    def save_long_form_plan(self, *, topic: str, content_type: str,
                            target_duration_min: int, chapter_count: int,
                            language: str, tone: str, audience: str,
                            angle: str = "", narration_mode: str = "undecided",
                            include_subtitles: bool = True, notes: str = "",
                            updated_by: str = "console") -> dict:
        """Persist a planning-only long-form brief; never starts generation."""
        topic = str(topic or "").strip()
        angle = str(angle or "").strip()
        audience = str(audience or "").strip()
        notes = str(notes or "").strip()
        content_type = str(content_type or "").strip()
        language = str(language or "").strip()
        tone = str(tone or "").strip()
        narration_mode = str(narration_mode or "").strip()
        if not topic:
            raise PipelineServiceError("uzun video konusu bos olamaz")
        if len(topic) > 300:
            raise PipelineServiceError("uzun video konusu en fazla 300 karakter olabilir")
        if len(angle) > 1000:
            raise PipelineServiceError("anlatim acisi en fazla 1000 karakter olabilir")
        if len(audience) > 300:
            raise PipelineServiceError("hedef kitle en fazla 300 karakter olabilir")
        if len(notes) > 3000:
            raise PipelineServiceError("uzun video notlari en fazla 3000 karakter olabilir")
        if content_type not in {"theory", "explainer", "comparison"}:
            raise PipelineServiceError("uzun video turu gecersiz")
        if language not in {"tr-TR", "en-US"}:
            raise PipelineServiceError("uzun video dili gecersiz")
        if tone not in {"cinematic", "documentary", "conversational", "analytical"}:
            raise PipelineServiceError("uzun video tonu gecersiz")
        if narration_mode not in {"undecided", "tts", "own_voice"}:
            raise PipelineServiceError("uzun video ses tercihi gecersiz")
        try:
            duration_value = float(target_duration_min)
            chapters_value = float(chapter_count)
        except (TypeError, ValueError) as exc:
            raise PipelineServiceError("uzun video suresi ve bolum sayisi sayi olmali") from exc
        if not duration_value.is_integer() or not chapters_value.is_integer():
            raise PipelineServiceError("uzun video suresi ve bolum sayisi tam sayi olmali")
        duration = int(duration_value)
        chapters = int(chapters_value)
        if duration < 15 or duration > 20:
            raise PipelineServiceError("uzun video hedef suresi 15-20 dakika olmali")
        if chapters < 5 or chapters > 7:
            raise PipelineServiceError("uzun video bolum sayisi 5-7 olmali")

        current = self.load_artifact("long_form_plan").get("data") or {}
        now = _now_iso()
        record = {
            "schema_version": 1,
            "artifact_type": "long_form_plan",
            "status": "draft",
            "status_reason": "plan kaydedildi; outline ve uretim henuz baslatilmadi",
            "topic": topic,
            "content_type": content_type,
            "target_duration_min": duration,
            "estimated_word_range": {
                "minimum": duration * 130,
                "maximum": duration * 150,
            },
            "chapter_count": chapters,
            "language": language,
            "tone": tone,
            "audience": audience,
            "angle": angle,
            "narration_mode": narration_mode,
            "include_subtitles": bool(include_subtitles),
            "notes": notes,
            "planning_only": True,
            "generation_started": False,
            "created_at": current.get("created_at") or now,
            "updated_at": now,
            "updated_by": str(updated_by or "console")[:120],
        }
        self.write_artifact("long_form_plan", record, actor=updated_by)
        return record

    def long_form_studio(self):
        """The long-form workflow for this project.

        Built on demand rather than in __init__ so a Shorts-only project never
        pays for it and the Shorts pipeline keeps no reference to it.
        """
        from .long_form_studio import LongFormStudio
        return LongFormStudio(self)

    def turbo_studio(self):
        """The Turbo workflow for this project.

        Same on-demand construction as `long_form_studio`: a project that never
        opens the Turbo tab never builds one, and neither of the other two
        pipelines holds a reference to it.
        """
        from .turbo_studio import TurboStudio
        return TurboStudio(self)

    # ------------------------------------------------------- project lifecycle
    def _archive_revision(self, paths, *, action: str, actor: str,
                          target: str | None = None) -> dict:
        """Move exact namespace-relative paths into a recoverable snapshot.

        The mechanics live in `LocalRevisionArchive` (`ports.RevisionArchiver`)
        so `MomentService` shares one implementation without a facade callback.
        """
        return self.revision_archive.archive_revision(
            paths, action=action, actor=actor, target=target)

    def revisions(self, *, limit: int = 20) -> list:
        root = self.storage.resolve(self.ownership, ".revisions")
        if not root.is_dir():
            return []
        records = []
        for child in root.iterdir():
            manifest = child / "manifest.json"
            if not manifest.is_file():
                continue
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            records.append(data)
        records.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return records[:max(1, min(int(limit), 100))]

    def rewind_project(self, *, target: str, actor: str = "console") -> dict:
        if target not in REWIND_PATHS:
            raise PipelineServiceError(
                f"gecersiz geri donus asamasi: {target!r}")
        revision = self._archive_revision(
            REWIND_PATHS[target], action="rewind", actor=actor, target=target)
        project = self.load_artifact("project", required=True)["data"]
        project.update({
            "updated_at": _now_iso(),
            "updated_by": str(actor or "console")[:120],
            "status": "review_required",
            "status_reason": (
                f"{REWIND_LABELS[target]} asamasina geri donuldu; "
                "bu asama ve sonrasi yeniden calistirilacak"),
        })
        self.write_artifact("project", project, actor=actor)
        self.audit.record("project.rewound", actor=actor, detail={
            "target": target, "revision_id": revision["revision_id"],
            "moved_paths": revision["moved_paths"],
        })
        return {"project": project, "revision": revision}

    # ------------------------------------------------------- guided revision
    #: Which rewind stage each review item's fix really lives at. A SUGGESTION,
    #: never applied on its own: "crop is wrong" can mean the plan's framing was
    #: wrong (re-plan) or that this one moment is badly framed (re-select), and
    #: only the person who watched the video knows which.
    REVISION_STAGE_HINTS = {
        "clip_boundaries": "moment",
        "crop": "plan",
        "subtitle_sync": "script",
        "active_word_highlight": "script",
        "font_readability": "plan",
        "audio_continuity": "moment",
    }

    def render_adjustment_relative(self, slot: int = 1) -> str:
        index = self.require_slot(slot)
        return f"results/render_adjustments/output_{index:02d}.json"

    def render_adjustment(self, slot: int = 1) -> dict:
        loaded = self.load_artifact_path(self.render_adjustment_relative(slot))
        if not isinstance(loaded, dict):
            return {"frame_zoom": 1.0, "frame_focus_x": 0.5,
                    "frame_focus_y": 0.5, "source": "default"}
        return {
            "frame_zoom": min(2.0, max(1.0, float(loaded.get("frame_zoom", 1.0)))),
            "frame_focus_x": min(1.0, max(0.0, float(loaded.get("frame_focus_x", 0.5)))),
            "frame_focus_y": min(1.0, max(0.0, float(loaded.get("frame_focus_y", 0.5)))),
            "source": loaded.get("source") or "saved_revision",
            "reason": loaded.get("reason") or "",
            "updated_at": loaded.get("updated_at"),
        }

    @staticmethod
    def infer_render_adjustment(notes: str, current=None) -> dict | None:
        """Parse a deliberately small, auditable framing vocabulary.

        This is not an LLM pretending to understand arbitrary edit direction.
        It only acts when the note contains an unambiguous zoom/focus command.
        """
        text = str(notes or "").casefold().translate(str.maketrans({
            "ı": "i", "ş": "s", "ğ": "g", "ü": "u", "ö": "o", "ç": "c",
        }))
        base = current or {}
        zoom = float(base.get("frame_zoom", 1.0) or 1.0)
        focus_x = float(base.get("frame_focus_x", 0.5) or 0.5)
        focus_y = float(base.get("frame_focus_y", 0.5) or 0.5)
        matched = []
        if any(token in text for token in
               ("yakinlas", "yakinlastir", "zoom in", "zoomla", "daha yakin")):
            zoom = 1.12 if "biraz" in text else 1.20
            matched.append("zoom_in")
        elif any(token in text for token in ("uzaklas", "zoom out", "uzaklastir")):
            zoom = 1.0
            matched.append("zoom_out")
        if any(token in text for token in ("sola odak", "sol tarafa", "sola kaydir")):
            focus_x = 0.30
            matched.append("focus_left")
        elif any(token in text for token in ("saga odak", "sag tarafa", "saga kaydir")):
            focus_x = 0.70
            matched.append("focus_right")
        if any(token in text for token in
               ("yukariya odak", "yukari odak", "yukari kaydir")):
            focus_y = 0.30
            matched.append("focus_up")
        elif any(token in text for token in
                 ("asagiya odak", "asagi odak", "asagi kaydir")):
            focus_y = 0.70
            matched.append("focus_down")
        if not matched:
            return None
        return {"frame_zoom": zoom, "frame_focus_x": focus_x,
                "frame_focus_y": focus_y, "matched_commands": matched}

    def save_render_adjustment(self, adjustment: dict, *, notes: str, actor: str,
                               slot: int = 1) -> dict:
        record = {
            "artifact_type": "render_adjustment", "schema_version": 1,
            "status": "active", "not_uploaded": True,
            "frame_zoom": min(2.0, max(1.0, float(adjustment["frame_zoom"]))),
            "frame_focus_x": min(1.0, max(0.0, float(adjustment["frame_focus_x"]))),
            "frame_focus_y": min(1.0, max(0.0, float(adjustment["frame_focus_y"]))),
            "matched_commands": list(adjustment.get("matched_commands") or []),
            "source": "revision_note", "reason": str(notes or "")[:2000],
            "updated_at": _now_iso(), "updated_by": str(actor or "console")[:120],
        }
        relative = self.render_adjustment_relative(slot)
        atomic_write_json(self.storage.resolve(self.ownership, relative, create_parent=True),
                          record)
        self.audit.record("artifact.written", actor=actor, artifact=relative,
                          detail={"slot": int(slot), "frame_zoom": record["frame_zoom"],
                                  "matched_commands": record["matched_commands"]})
        return record

    # ---------------------------------------------------------- render profile
    def render_profile_relative(self, slot: int = 1) -> str:
        index = self.require_slot(slot)
        return f"results/render_profiles/output_{index:02d}.json"

    def plan_decision(self) -> dict:
        return self.workflow_gate.plan_decision()

    def render_profile(self, slot: int = 1) -> dict:
        """The saved profile, or Phase 0's decision expressed as one.

        A project that never opens Render Studio has NO profile file, and must
        keep rendering exactly as the approved plan said. So the fallback is not
        a hardcoded default -- it is the plan itself, plus whatever framing the
        old `render_adjustment` artifact recorded, migrated on read so the two
        never disagree.
        """
        decision = self.plan_decision()
        loaded = self.load_artifact_path(self.render_profile_relative(slot))
        if isinstance(loaded, dict) and isinstance(loaded.get("settings"), dict):
            try:
                settings = render_profile_module.validate(
                    loaded["settings"], slot=slot, plan_decision=decision)
            except render_profile_module.RenderProfileError:
                # A profile written by an older schema, or hand-edited into an
                # invalid state, must not brick the render screen. Fall back to
                # the plan and say so, rather than raising at read time.
                return {"settings": render_profile_module.default_profile(
                            decision, slot=slot),
                        "source": "plan_default_invalid_profile",
                        "content_sha256": None, "updated_at": None,
                        "updated_by": None, "reason": "", "base_plan_hash": None}
            return {"settings": settings,
                    "source": "saved_profile",
                    "content_sha256": loaded.get("content_sha256"),
                    "updated_at": loaded.get("updated_at"),
                    "updated_by": loaded.get("updated_by"),
                    "reason": loaded.get("reason") or "",
                    "base_plan_hash": loaded.get("base_plan_hash")}

        adjustment = self.load_artifact_path(self.render_adjustment_relative(slot))
        settings = render_profile_module.from_render_adjustment(
            adjustment if isinstance(adjustment, dict) else None, decision, slot=slot)
        return {"settings": settings,
                "source": ("migrated_render_adjustment"
                           if isinstance(adjustment, dict) else "plan_default"),
                "content_sha256": render_profile_module.content_sha256(settings),
                "updated_at": None, "updated_by": None, "reason": "",
                "base_plan_hash": None}

    def effective_render_config(self, slot: int = 1) -> dict:
        """What a render of this slot will actually use.

        Phase 0 supplies every key; the profile replaces only the presentation
        ones. `presentation_overridden` travels WITH the config so the drift
        check knows the difference is a recorded choice and not an edited
        notebook cell.
        """
        decision = self.plan_decision()
        profile = self.render_profile(slot)["settings"]
        config = render_profile_module.effective_render_config(decision, profile)
        return {
            "config": config,
            "quality": render_profile_module.resolve_quality(profile),
            "framing": {key: profile[key] for key in
                        ("frame_zoom", "frame_focus_x", "frame_focus_y")},
            "caption_overrides": {
                "font_size": profile["caption_font_size"],
                "margin_v": profile["caption_margin_v"],
                "active_color_ass": render_profile_module.ACTIVE_COLORS[
                    profile["active_word_color"]],
            },
            # Compared against the plan expressed AS a profile, not against the
            # plan dict's raw keys: a plan that simply omits `preview_mode`
            # would otherwise look overridden on every untouched project, and
            # silently disarm the drift check for projects nobody edited.
            "profile_settings": profile,
            "presentation_overridden": config != render_profile_module
                .effective_render_config(
                    decision,
                    render_profile_module.default_profile(decision, slot=slot)),
        }

    #: Artifacts that live in a slot's render directory but are INPUTS to the
    #: render, not products of it. Re-encoding must never take them.
    #:
    #: Slot 1 keeps these at the historical top-level paths, outside the render
    #: directory, so they were safe there by accident. Slots 2+ keep them INSIDE
    #: `results/render/output_NN/`, where archiving the directory wholesale
    #: silently destroyed the moment selection and the approved caption text --
    #: turning "change the caption theme" into "lose your moment".
    RENDER_ARCHIVE_PRESERVED = ("moment_selection.json", "moment_transcript.json")

    def _render_slot_archive_paths(self, slot: int = 1, *,
                                   preserve=None) -> list:
        """Exactly this slot's render OUTPUT -- nothing another Short owns, and
        nothing this Short needs in order to render again.

        Slot 1's render directory CONTAINS slot 2's (`results/render/output_02`),
        so archiving `results/render` wholesale would take every other Short with
        it. That is the slot leak `PER_SLOT_ARTIFACTS` exists to prevent, and it
        would be silent: the operator changes slot 1's caption theme and slot 3's
        approved video quietly moves into the archive.

        `preserve` names files to keep even though they sit in the slot's
        directory; pass an empty tuple to archive the transcript too, which is
        what a direct-mode clip-range edit has to do.

        The mechanics live in `LocalRevisionArchive`
        (`ports.RevisionArchiver`) so `MomentService` shares one implementation
        without a facade callback.
        """
        return self.revision_archive.render_slot_archive_paths(
            slot, preserve=preserve)

    def render_profile_impact(self, slot: int = 1) -> dict:
        """What saving a new profile WOULD archive. Pure read, changes nothing.

        Shown before the button is pressed for the same reason `revision_plan`
        is: a confirmation that cannot name what it moves is not consent.
        """
        slot = self.require_slot(slot)
        entries = []
        for relative in self._render_slot_archive_paths(slot):
            try:
                path = self.storage.resolve(self.ownership, relative)
            except StorageViolation:
                continue
            if not path.exists():
                continue
            if path.is_dir():
                files = [child for child in path.rglob("*") if child.is_file()]
                entries.append({"path": relative, "kind": "dir",
                                "file_count": len(files),
                                "size_bytes": sum(f.stat().st_size for f in files)})
            else:
                entries.append({"path": relative, "kind": "file", "file_count": 1,
                                "size_bytes": path.stat().st_size})
        return {
            "slot": slot,
            "moves": entries,
            "file_count": sum(entry["file_count"] for entry in entries),
            "size_bytes": sum(entry["size_bytes"] for entry in entries),
            "nothing_to_move": not entries,
            "destination": "revisions/<revizyon-id>/",
            "reversible": True,
            # Said plainly so the UI can promise it: changing how a Short LOOKS
            # never re-runs the script or the voice, and never costs a provider
            # call. Only the encode is redone.
            "preserved": ["results/selected_script.json",
                          "results/selected_voiceover.json",
                          "results/moment_selection.json",
                          "results/moment_transcript.json",
                          "results/subtitle_track.json"],
            "not_uploaded": True,
        }

    # --------------------------------------------------------- clip bounds
    # Moment selection + clip-bounds ownership lives in
    # `application.services.MomentService` (Milestone 11); these methods keep
    # the public facade contract and delegate.
    def clip_bounds_relative(self, slot: int = 1) -> str:
        return self.moment.clip_bounds_relative(slot)

    def selected_moment(self, slot: int = 1) -> dict:
        """The analysed moment this slot renders, or {} when none is chosen."""
        return self.moment.selected_moment(slot)

    def clip_bounds(self, slot: int = 1) -> dict:
        """This slot's edited clip range, falling back to the analysed moment.

        Per SLOT, not per project: `results/edl/edl.json` is a single
        project-wide artifact, so storing a slot's in/out there would let one
        Short's edit silently move another's -- the leak `PER_SLOT_ARTIFACTS`
        exists to stop.
        """
        return self.moment.clip_bounds(slot)

    def clip_bounds_override(self, slot: int = 1):
        """What `_run_render` passes down -- `None` when nothing was edited."""
        return self.moment.clip_bounds_override(slot)

    def source_duration_sec(self):
        """How long the source video is, in seconds, or None."""
        return self.source.get_duration()

    def clip_bounds_view(self, slot: int = 1) -> dict:
        """Everything the single-clip editor needs, decided on the server.

        Includes `source_duration_sec` because a timeline that does not know how
        long the source is cannot draw a scrubber, and `editable` because the
        answer depends on which file the render will actually seek into -- an
        analysis clip is already cropped and absolute source seconds mean
        something different in it.
        """
        return self.moment.clip_bounds_view(slot)

    def save_clip_bounds(self, *, start: float, end: float, actor: str = "console",
                         slot: int = 1) -> dict:
        """Move or resize this slot's clip inside the source.

        Direct mode captions come from THIS range, so changing it invalidates the
        approved transcript: the words a human read are no longer the words that
        will be spoken. Narrated mode keeps its script, voice and subtitles --
        the narration does not change because the picture underneath moved.
        """
        return self.moment.save_clip_bounds(
            start=start, end=end, actor=actor, slot=slot)

    def reset_clip_bounds(self, *, actor: str = "console", slot: int = 1) -> dict:
        """Snap back to the range the analysis chose."""
        return self.moment.reset_clip_bounds(actor=actor, slot=slot)

    def render_profile_view(self, slot: int = 1) -> dict:
        """Everything the Render Studio panel needs, decided on the server.

        Includes the CURRENT output's settings separately from the NEXT
        render's, because the screen must never show a pending choice as though
        it were what the existing video was made with. `stale` is the whole
        point: an operator who saved a new theme and did not re-render has to
        see that the video below is the old one.
        """
        try:
            slot = self.require_slot(slot)
        # Rendered on EVERY page load, including a project with no plan yet.
        # A panel that cannot open must say so, never break the page.
        except (PipelineServiceError, ArtifactProblem):
            return {"available": False, "slot": int(slot or 1),
                    "reason": "bu slot planda tanimli degil"}
        profile = self.render_profile(slot)
        effective = self.effective_render_config(slot)
        manifest = self.load_artifact("render_manifest", slot=slot)
        rendered = (manifest["data"] or {}).get("render_profile") or {}
        current_hash = rendered.get("content_sha256")
        return {
            "available": True,
            "slot": slot,
            "settings": profile["settings"],
            "source": profile["source"],
            "content_sha256": profile["content_sha256"],
            "updated_at": profile["updated_at"],
            "updated_by": profile["updated_by"],
            "reason": profile["reason"],
            "effective": effective,
            "impact": self.render_profile_impact(slot),
            # What the EXISTING video was actually rendered with. Provenance,
            # not a comparison: saving a profile ARCHIVES the old render, so a
            # video and a profile can never disagree here -- there is no "stale
            # output" state to warn about, only "no output yet".
            #
            # Older renders predate the stamp and report `null`, which the panel
            # says plainly instead of guessing that they match.
            "rendered_settings": rendered.get("settings"),
            "rendered_sha256": current_hash,
            "rendered_quality_preset": rendered.get("quality_preset"),
            "rendered_is_delivery_quality": rendered.get("is_delivery_quality"),
            "has_output": bool(manifest["data"]),
            "unknown_rendered_settings": bool(manifest["data"]) and current_hash is None,
            "options": {
                "quality_presets": sorted(render_profile_module.QUALITY_PRESETS),
                "resolution_presets": sorted(render_profile_module.RESOLUTION_PRESETS),
                "render_modes": list(render_profile_module.RENDER_MODES),
                "caption_themes": ["auto"] + list(render_profile_module.allowed_themes()),
                "active_word_colors": sorted(render_profile_module.ACTIVE_COLORS),
                "delivery_quality": render_profile_module.DELIVERY_QUALITY,
                "font_size_range": [render_profile_module.FONT_SIZE_MIN,
                                    render_profile_module.FONT_SIZE_MAX],
                "margin_v_range": [render_profile_module.MARGIN_V_MIN,
                                   render_profile_module.MARGIN_V_MAX],
            },
            "not_uploaded": True,
        }

    def save_render_profile(self, values, *, actor: str = "console",
                            expected_hash=None, reason: str = "",
                            slot: int = 1) -> dict:
        """Validate, archive this slot's render, write the new profile.

        Saving a profile and STARTING a render are deliberately separate: the
        operator may want to line up several changes before spending an encode,
        and an encode that begins on a keystroke is not a decision anyone made.
        """
        slot = self.require_slot(slot)
        decision = self.plan_decision()
        current = self.render_profile(slot)

        # Stale-write guard. The echoed hash is the operator's claim about WHICH
        # settings they were editing; if the slot moved underneath them, saving
        # would silently discard the other change.
        if expected_hash is not None:
            echoed = str(expected_hash or "").strip()
            if echoed != (current["content_sha256"] or ""):
                raise PipelineServiceError(
                    "Bu slotun render profili siz duzenlerken degisti. Sayfayi "
                    "yenileyip degisikliginizi tekrar girin.")

        # Validation FIRST: it is the step that can reject, and nothing may be
        # archived on a request that was never going to be accepted.
        try:
            settings = render_profile_module.validate(
                values, slot=slot, plan_decision=decision)
        except render_profile_module.RenderProfileError as exc:
            self.audit.record("artifact.rejected", actor=actor,
                              artifact=self.render_profile_relative(slot),
                              detail={"kind": "render_profile", "slot": slot,
                                      "reason": str(exc)})
            raise PipelineServiceError(str(exc)) from exc

        changed = render_profile_module.changed_fields(current["settings"], settings)
        if not changed and current["source"] == "saved_profile":
            # Saving identical settings must archive NOTHING. Otherwise every
            # stray click would throw away a render that is still exactly right.
            return {"slot": slot, "settings": settings, "changed_fields": [],
                    "unchanged": True, "archived": None,
                    "content_sha256": current["content_sha256"],
                    "render_required": False, "not_uploaded": True}

        impact = self.render_profile_impact(slot)
        archived = None
        if not impact["nothing_to_move"]:
            archived = self._archive_revision(
                self._render_slot_archive_paths(slot),
                action="render_profile_saved", actor=actor, target="render")

        plan_hash = ((self.load_artifact("processing_plan")["data"] or {})
                     .get("approval") or {}).get("content_sha256")
        artifact = render_profile_module.build_artifact(
            settings, slot=slot, base_plan_hash=plan_hash,
            actor=actor, reason=reason,
            previous_sha256=current["content_sha256"])
        atomic_write_json(
            self.storage.resolve(self.ownership,
                                 self.render_profile_relative(slot),
                                 create_parent=True),
            artifact)

        # Field NAMES only. `reason` is operator free text and never enters the
        # trail as content.
        self.audit.record("render_profile.saved", actor=actor,
                          artifact=self.render_profile_relative(slot),
                          detail={"slot": slot, "changed_fields": changed,
                                  "previous_sha256": current["content_sha256"],
                                  "content_sha256": artifact["content_sha256"],
                                  "has_reason": bool(str(reason or "").strip()),
                                  "archived_paths": (archived or {}).get("moved_paths", []),
                                  "revision_id": (archived or {}).get("revision_id")})
        return {"slot": slot, "settings": settings, "changed_fields": changed,
                "unchanged": False, "archived": archived,
                "content_sha256": artifact["content_sha256"],
                "render_required": True, "not_uploaded": True}

    def reset_render_profile(self, *, actor: str = "console",
                             slot: int = 1) -> dict:
        """Return this slot to what Phase 0 approved.

        Implemented as a save of the plan's own defaults rather than by deleting
        the file, so the reset archives the stale render exactly like any other
        change and leaves the same audit trail.
        """
        slot = self.require_slot(slot)
        defaults = render_profile_module.default_profile(self.plan_decision(),
                                                         slot=slot)
        result = self.save_render_profile(
            defaults, actor=actor, reason="Faz 0 varsayilanlarina donuldu",
            slot=slot)
        self.audit.record("render_profile.reset", actor=actor,
                          artifact=self.render_profile_relative(slot),
                          detail={"slot": slot,
                                  "changed_fields": result["changed_fields"]})
        return result

    def revision_plan(self, *, target: str, slot: int = 1) -> dict:
        """What starting this revision WOULD archive. Pure read, changes nothing.

        Shown before the button is pressed because the honest question is not
        "are you sure" but "are you sure about THESE files". A confirmation
        dialog that cannot name what it destroys is not consent.
        """
        if target not in REWIND_PATHS:
            raise PipelineServiceError(
                f"gecersiz revizyon asamasi: {target!r} (beklenen: {sorted(REWIND_PATHS)})")
        entries = []
        for relative in REWIND_PATHS[target]:
            try:
                path = self.storage.resolve(self.ownership, relative)
            except StorageViolation:
                continue
            if not path.exists():
                continue
            if path.is_dir():
                files = [child for child in path.rglob("*") if child.is_file()]
                entries.append({"path": relative, "kind": "dir",
                                "file_count": len(files),
                                "size_bytes": sum(f.stat().st_size for f in files)})
            else:
                entries.append({"path": relative, "kind": "file", "file_count": 1,
                                "size_bytes": path.stat().st_size})
        current_review = self.render_review_state(slot)
        open_note = ((current_review.get("open_revision") or {}).get("notes") or "")
        inferred = (self.infer_render_adjustment(
            open_note, self.render_adjustment(slot)) if target == "render" else None)
        return {
            "target": target,
            "label": REWIND_LABELS.get(target, target),
            "slot": int(slot or 1),
            "moves": entries,
            "file_count": sum(entry["file_count"] for entry in entries),
            "size_bytes": sum(entry["size_bytes"] for entry in entries),
            "nothing_to_move": not entries,
            # Said plainly: the previous attempt is not deleted, and that is why
            # there is no "are you sure you want to lose this" here.
            # Where they actually land: storage.resolve strips the leading dot,
            # so the archive is <proje>/revisions/<id>/, not '.revisions'.
            "destination": "revisions/<revizyon-id>/",
            "reversible": True,
            "not_uploaded": True,
            "suggested_render_adjustment": inferred,
        }

    def suggested_revision_stage(self, slot: int = 1) -> dict:
        """Stage suggested by the failing review items, with the reason.

        Derived from what the reviewer actually rejected rather than from free
        text: interpreting a sentence would mean asking a model which artifacts
        to destroy, and this mode is contractually provider-free anyway.
        """
        state = self.render_review_state(slot)
        open_revision = state.get("open_revision") or {}
        target_map = {
            "moments": "moment", "phase0": "plan", "script": "script",
            "voice": "voice", "render": "render",
        }
        explicit_stage = target_map.get(open_revision.get("target_tab"))
        if explicit_stage:
            return {
                "stage": explicit_stage,
                "reasons": [f"Kayitli duzeltme hedefi -> "
                            f"{REWIND_LABELS.get(explicit_stage, explicit_stage)}"],
                "note": "Revizyon kaydinda kullanicinin sectigi hedef esas alindi.",
            }
        order = ["assessment", "plan", "analysis", "moment", "script", "voice", "render"]
        unchecked = [item for item in (state.get("items") or [])
                     if not item.get("checked")]
        stages, reasons = [], []
        for item in unchecked:
            stage = self.REVISION_STAGE_HINTS.get(item.get("key"))
            if stage:
                stages.append(stage)
                reasons.append(f"{item.get('label') or item.get('key')} -> "
                               f"{REWIND_LABELS.get(stage, stage)}")
        if not stages:
            # Everything passed, or nothing was reviewed. Redoing only the render
            # is the smallest thing that can be meant by "do it again".
            return {"stage": "render", "reasons": [],
                    "note": "Isaretlenmemis inceleme maddesi yok; en dar kapsam "
                            "olarak yalnizca render onerildi."}
        # The EARLIEST stage wins: fixing the framing and then re-rendering with a
        # stale moment would fix one complaint and keep the other.
        earliest = min(stages, key=order.index)
        return {"stage": earliest, "reasons": reasons,
                "note": f"En erken asama secildi ({REWIND_LABELS.get(earliest, earliest)}); "
                        "daha gec bir asamadan baslamak onceki sikayeti cozmeden birakir."}

    def start_revision(self, *, target: str, notes: str = "", actor: str = "console",
                       slot: int = 1) -> dict:
        """Archive the invalidated stage and hand the project back for a rerun.

        This is the step that was missing. Requesting a revision used to be a
        NOTE -- it changed no artifact and moved nothing -- so the rejected clip,
        transcript and video stayed exactly where the next run would pick them
        up again. Nothing is deleted: `_archive_revision` MOVES the paths into
        `.revisions/<id>/` with a manifest, so a revision taken by mistake is
        recoverable.
        """
        slot = self.require_slot(slot)
        plan = self.revision_plan(target=target, slot=slot)
        if plan["nothing_to_move"]:
            raise PipelineServiceError(
                f"{plan['label']} asamasinda tasinacak bir cikti yok; "
                "bu asama zaten bos.")
        open_note = ((self.render_review_state(slot).get("open_revision") or {})
                     .get("notes") or "")
        note = str(notes or open_note or "").strip()[:2000]
        # Record the reason BEFORE moving anything: if the move fails halfway the
        # project still says why someone tried.
        if note:
            try:
                self.request_render_revision(requested_by=actor, notes=note,
                                             target_tab="render", slot=slot)
            except PipelineServiceError:
                # No render to attach the note to (rewinding from an earlier
                # stage). The audit entry below still carries it.
                pass
        adjustment = None
        if target == "render":
            inferred = self.infer_render_adjustment(note, self.render_adjustment(slot))
            if inferred:
                adjustment = self.save_render_adjustment(
                    inferred, notes=note, actor=actor, slot=slot)
        result = self.rewind_project(target=target, actor=actor)
        self.audit.record("project.rewound", actor=actor, detail={
            "revision_started": True, "target": target, "slot": slot,
            "revision_id": result["revision"]["revision_id"],
            "moved_paths": result["revision"]["moved_paths"],
            "has_notes": bool(note),
        })
        return {"revision": result["revision"], "project": result["project"],
                "plan": plan, "next_stage": target,
                "label": REWIND_LABELS.get(target, target),
                "render_adjustment": adjustment}

    def archive_view(self, *, limit: int = 20) -> dict:
        """The archive, so a moved-away attempt is visible instead of merely gone."""
        records = self.revisions(limit=limit)
        return {
            "revisions": [
                {"revision_id": item.get("revision_id"), "action": item.get("action"),
                 "target": item.get("target"), "created_at": item.get("created_at"),
                 "created_by": item.get("created_by"),
                 "moved_paths": item.get("moved_paths") or [],
                 "path_count": len(item.get("moved_paths") or [])}
                for item in records
            ],
            "count": len(records),
            "hint": ("Revizyon baslatildiginda eski ciktilar silinmez, buraya "
                     "tasinir. Kalici silme ayri ve acik bir islemdir."),
            "not_uploaded": True,
        }

    def purge_revision(self, *, revision_id: str, confirmation: str,
                       actor: str = "console") -> dict:
        """Permanently delete ONE archived revision.

        Separate from starting a revision, and gated by typing the id back:
        archiving exists so a wrong decision is recoverable, and a delete that
        rides along with the revision button would quietly remove that safety.
        """
        identifier = str(revision_id or "").strip()
        if not identifier or "/" in identifier or "\\" in identifier \
                or identifier.startswith("."):
            raise PipelineServiceError(f"gecersiz revizyon kimligi: {revision_id!r}")
        if str(confirmation or "").strip() != identifier:
            raise PipelineServiceError(
                "Kalici silme icin revizyon kimligini birebir yazmalisin.")
        root = self.storage.resolve(self.ownership, ".revisions", identifier)
        if not root.is_dir():
            raise LookupError(f"arsivlenmis revizyon bulunamadi: {identifier}")
        shutil.rmtree(root)
        self.audit.record("project.reset", actor=actor,
                          detail={"purged_revision": identifier})
        return self.archive_view()

    def reset_project(self, *, keep_source: bool = True,
                      actor: str = "console") -> dict:
        paths = ["results", "jobs", "progress", ".runtime", "runtime"]
        if not keep_source:
            paths.append(SOURCE_DIRNAME)
        revision = self._archive_revision(
            paths, action="reset", actor=actor,
            target="source" if keep_source else "empty")
        project = self.load_artifact("project", required=True)["data"]
        if not keep_source:
            project["source"] = None
        project.update({
            "updated_at": _now_iso(),
            "updated_by": str(actor or "console")[:120],
            "status": "review_required",
            "status_reason": (
                "proje kaynak video korunarak sifirlandi; Faz 0 bekleniyor"
                if keep_source else
                "proje tamamen sifirlandi; kaynak video bekleniyor"),
        })
        self.write_artifact("project", project, actor=actor)
        self.audit.record("project.reset", actor=actor, detail={
            "keep_source": bool(keep_source),
            "revision_id": revision["revision_id"],
            "moved_paths": revision["moved_paths"],
        })
        return {"project": project, "revision": revision}

    def update_script_text(self, *, script_text: str, updated_by: str,
                           notes: str = "") -> dict:
        """Human-edit a generated script and archive audio/render descendants."""
        return self.script.update_script_text(
            script_text=script_text, updated_by=updated_by, notes=notes)


    def subtitle_view(self, slot: int = 1) -> dict:
        """Editable narrated-caption rows, preferring saved human revisions."""
        slot = self.require_slot(slot)
        plan = self.load_artifact("processing_plan")["data"] or {}
        mode = (plan.get("decision") or {}).get("mode")
        loaded = self.load_artifact("subtitle_track")
        if loaded["problem"]:
            return {"slot": slot, "mode": mode, "available": False,
                    "problem": loaded["problem"], "cues": []}

        artifact = loaded["data"] or {}
        source = artifact.get("source") or "saved_revision"
        cues = artifact.get("cues") or []
        if not cues:
            srt_path = self.slot_render_dir(slot) / "subtitles.srt"
            if not srt_path.is_file():
                return {
                    "slot": slot, "mode": mode, "available": False,
                    "problem": None, "cues": [], "source": None,
                    "hint": "Ilk renderden sonra altyazi satirlari burada duzenlenebilir.",
                }
            try:
                cues = subtitle_edit.parse_srt(srt_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, subtitle_edit.SubtitleEditError) as exc:
                return {"slot": slot, "mode": mode, "available": False,
                        "problem": f"altyazi dosyasi okunamadi: {exc}", "cues": []}
            source = "rendered_srt"

        try:
            public = subtitle_edit.public_cues(cues)
            digest = subtitle_edit.content_sha256(cues)
        except subtitle_edit.SubtitleEditError as exc:
            return {"slot": slot, "mode": mode, "available": False,
                    "problem": f"altyazi artefakti gecersiz: {exc}", "cues": []}
        return {
            "slot": slot, "mode": mode, "available": True, "problem": None,
            "source": source, "source_sha256": digest,
            "revision": int(artifact.get("revision") or 0),
            "cues": public, "cue_count": len(public),
            "limits": {"max_cue_chars": subtitle_edit.MAX_CUE_CHARS,
                       "max_total_chars": subtitle_edit.MAX_TOTAL_CHARS},
        }

    def save_subtitle_edits(self, *, cues, edited_by: str, notes: str = "",
                            slot: int = 1) -> dict:
        """Save text-only cue edits and archive the now-stale rendered video."""
        slot = self.require_slot(slot)
        actor = str(edited_by or "").strip()
        if not actor:
            raise PipelineServiceError("altyaziyi duzenleyen kisi zorunlu")
        current = self.subtitle_view(slot)
        if current.get("problem"):
            raise PipelineServiceError(current["problem"])
        if not current.get("available"):
            raise PipelineServiceError(
                "duzenlenecek altyazi yok; once anlatimli videoyu bir kez render et")
        raw_cues = [
            {"start": cue["start"], "end": cue["end"], "text": cue["text"]}
            for cue in current["cues"]
        ]
        try:
            rebuilt, changed = subtitle_edit.apply_text_edits(raw_cues, cues)
        except subtitle_edit.SubtitleEditError as exc:
            raise PipelineServiceError(str(exc)) from None
        if not changed:
            return current

        previous = self.load_artifact("subtitle_track")["data"] or None
        revision = self._archive_revision(
            REWIND_PATHS["render"], action="subtitle_edit", actor=actor,
            target="render")
        artifact = subtitle_edit.build_artifact(
            rebuilt, source=current.get("source") or "rendered_srt",
            actor=actor, notes=notes, previous=previous,
            revision_id=revision["revision_id"])
        relative = self.write_artifact("subtitle_track", artifact, actor=actor)
        self.audit.record(
            "subtitle.updated", actor=actor, artifact=relative,
            detail={"slot": slot, "changed_cues": changed,
                    "changed_count": len(changed),
                    "revision_id": revision["revision_id"],
                    "render_archived": revision["moved_paths"]})
        return self.subtitle_view(slot)

    # Source media is owned by `application.services.SourceService`; these
    # methods keep the public facade contract and delegate.
    # See `docs/architecture/SOURCE_SERVICE.md`.
    def register_source_video(self, *, filename: str, payload: bytes,
                              registered_by: str, track: bool = False) -> dict:
        """Store an uploaded source video inside the project namespace."""
        return self.source.store_upload(filename=filename, payload=payload,
                                        registered_by=registered_by, track=track)

    def register_source_video_stream(self, *, filename: str, stream,
                                     content_length: int, registered_by: str,
                                     track: bool = True) -> dict:
        """Stream a large local upload to disk without buffering it in RAM."""
        return self.source.store_upload_stream(
            filename=filename, stream=stream, content_length=content_length,
            registered_by=registered_by, track=track)

    def source_path(self) -> Path:
        return self.source.get_source_path()

    # ------------------------------------------------- phase -1: trend ingest
    def discover_source_candidates(self, *, terms=None, manual_urls=None,
                                   max_search_calls: int = 4, lookback_days: int = 14,
                                   shorts_only: bool = False, actor="console",
                                   youtube_client=None) -> dict:
        """Build the source-candidate shortlist.

        Trend discovery runs only when a YouTube Data credential exists; a
        missing key is NOT an error, it simply means the list is limited to the
        URLs the user pasted in. Nothing here selects or downloads anything.
        """
        terms = [str(term).strip() for term in (terms or []) if str(term).strip()]
        discovery = None
        discovery_error = None
        if terms:
            credential = self.secrets.credential("youtube_data")
            if not credential.configured:
                discovery_error = (
                    "YouTube Data anahtari yok; trend kesfi atlandi. Yalnizca elle "
                    "girilen URL'ler listelendi."
                )
            else:
                from local_pipeline import youtube_data  # noqa: PLC0415

                cache_dir = self.storage.resolve(
                    self.ownership, RUNTIME_DIRNAME, "youtube_cache", create_parent=True)
                try:
                    client = youtube_client or youtube_data.build_client(credential.value)
                    discovery = youtube_data.discover_trending_content(
                        client, youtube_data.Cache(cache_dir), youtube_data.QuotaTracker(),
                        terms, lookback_days=lookback_days,
                        max_search_calls=max_search_calls, shorts_only=shorts_only,
                        api_key=credential.value,
                    )
                except Exception as exc:  # noqa: BLE001 - sanitized, never raw
                    discovery_error = youtube_data.sanitize_error_text(exc, credential.value)

        artifact = trend_ingest.build_source_candidates(discovery, manual_urls=manual_urls)
        artifact["discovery_terms"] = terms
        artifact["discovery_error"] = discovery_error
        relative = self.write_artifact("source_candidates", artifact, actor=actor)
        self.audit.record("artifact.written", actor=actor, artifact=relative,
                          detail={"candidate_count": artifact["candidate_count"],
                                  "terms": len(terms)})
        return artifact

    def probe_source_candidate(self, *, candidate_url: str, actor="console",
                               ytdlp_module=None) -> dict:
        """Read one candidate's metadata. Downloads nothing."""
        if not trend_ingest.is_supported_source_url(candidate_url):
            raise PipelineServiceError(f"gecersiz kaynak URL: {candidate_url!r}")
        record = trend_ingest.probe_candidate(candidate_url, ytdlp_module=ytdlp_module)
        self.audit.record("artifact.written", actor=actor,
                          artifact=ARTIFACTS["source_candidates"],
                          detail={"probe_status": record.get("status"),
                                  "duration_sec": record.get("duration_sec")})
        return record

    def record_rights_decision(self, *, candidate_url: str, license_type: str,
                               decided_by: str, attribution: str = "",
                               transformation_note: str = "",
                               risk_acknowledged: bool = False,
                               source_title: str = "", source_channel: str = "",
                               notes: str = "") -> dict:
        """Persist the human's licence decision. `build_rights_decision` raises
        on anything incomplete, so a half-filled record can never unlock a
        download."""
        decision = trend_ingest.build_rights_decision(
            candidate_url, license_type, decided_by,
            attribution=attribution, transformation_note=transformation_note,
            risk_acknowledged=risk_acknowledged, source_title=source_title,
            source_channel=source_channel, notes=notes,
        )
        relative = self.write_artifact("rights_decision", decision, actor=decided_by)
        self.audit.record("approval.granted", actor=decided_by, artifact=relative,
                          detail={"kind": "rights_decision",
                                  "license_type": license_type,
                                  "decision_sha256": decision["decision_sha256"]})
        return decision

    def run_source_ingest(self, *, candidate_url: str, job_id=None, actor="console",
                          max_duration_sec=trend_ingest.DEFAULT_MAX_DURATION_SEC,
                          max_filesize_bytes=trend_ingest.DEFAULT_MAX_FILESIZE_BYTES,
                          ytdlp_module=None) -> dict:
        tracker = self.tracker("source_ingest", job_id=job_id)
        tracker.start("Kaynak indirme basladi")
        try:
            return self._run_source_ingest(
                tracker, candidate_url=candidate_url, job_id=job_id, actor=actor,
                max_duration_sec=max_duration_sec, max_filesize_bytes=max_filesize_bytes,
                ytdlp_module=ytdlp_module)
        except Exception as exc:  # noqa: BLE001
            tracker.fail(f"{type(exc).__name__}: {exc}")
            raise

    def _run_source_ingest(self, tracker, *, candidate_url, job_id, actor,
                           max_duration_sec, max_filesize_bytes, ytdlp_module) -> dict:
        tracker.stage("rights", "Haklar karari dogrulaniyor")
        rights = self.load_artifact("rights_decision", required=True)
        if rights["problem"]:
            raise ArtifactProblem(rights["problem"])
        errors = trend_ingest.validate_rights_decision(rights["data"], candidate_url)
        if errors:
            # Refused before any byte is fetched. Reported as a service error so
            # the console shows the exact missing condition.
            raise PipelineServiceError("INDIRME REDDEDILDI: " + "; ".join(errors))

        tracker.stage("probe", "Kaynak meta verisi okunuyor")
        probe = trend_ingest.probe_candidate(candidate_url, ytdlp_module=ytdlp_module)
        if probe.get("status") == "failed":
            raise PipelineServiceError(
                f"kaynak okunamadi: {probe.get('status_reason')}")
        if probe.get("ingest_blockers"):
            raise PipelineServiceError(
                "kaynak indirmeye uygun degil: " + "; ".join(probe["ingest_blockers"]))

        tracker.stage("download", "Kaynak indiriliyor")
        target_dir = self.storage.resolve(self.ownership, SOURCE_DIRNAME, "ingest",
                                          create_parent=True)
        manifest = trend_ingest.download_source(
            candidate_url, target_dir, rights["data"], downloaded_by=actor,
            ytdlp_module=ytdlp_module, probe_record=probe,
            max_duration_sec=max_duration_sec, max_filesize_bytes=max_filesize_bytes,
        )
        if manifest.get("status") != "review_required":
            self.write_artifact("ingest_manifest", manifest, job_id=job_id, actor=actor)
            tracker.fail(manifest.get("error") or manifest.get("status_reason")
                         or "indirme basarisiz")
            return manifest

        tracker.stage("register", "Kaynak projeye baglaniyor")
        stored = Path(manifest["source_path"])
        manifest["source_path"] = self.relative(stored)
        self.write_artifact("ingest_manifest", manifest, job_id=job_id, actor=actor)
        self._register_ingested_source(stored, manifest, registered_by=actor)
        self.audit.record("source.registered", actor=actor, job_id=job_id,
                          detail={"origin": "trend_ingest",
                                  "license_type": manifest["rights"]["license_type"],
                                  "size_bytes": manifest["filesize_bytes"]})
        tracker.finish(message=f"{manifest['filesize_bytes']} bayt kaynak hazir")
        return manifest

    def _register_ingested_source(self, stored: Path, manifest: dict, *,
                                  registered_by: str) -> dict:
        """Point the project at a downloaded file.

        Reached only after the rights gate above has passed and the ingest
        manifest was written; the attachment itself is `SourceService`'s job.
        """
        return self.source.attach_downloaded_source(
            stored, manifest, registered_by=registered_by)

    def ingest_state(self) -> dict:
        """View model for the Trend Radar / rights screens."""
        candidates = self.load_artifact("source_candidates")
        rights = self.load_artifact("rights_decision")
        ingest = self.load_artifact("ingest_manifest")
        rights_errors = (trend_ingest.validate_rights_decision(rights["data"])
                         if rights["data"] else ["haklar karari verilmedi"])
        return {
            "candidates": candidates["data"],
            "candidates_problem": candidates["problem"],
            "rights": rights["data"],
            "rights_problem": rights["problem"],
            "rights_valid": not rights_errors,
            "rights_errors": rights_errors,
            "ingest": ingest["data"],
            "ingest_problem": ingest["problem"],
            "license_types": list(trend_ingest.LICENSE_TYPES),
            "third_party_license_types": list(trend_ingest.THIRD_PARTY_LICENSE_TYPES),
            "rights_notice": trend_ingest.CONTENT_ID_RISK_NOTICE,
        }

    # ----------------------------------------------------- publication
    # Publication orchestration is owned by
    # `application.services.PublishService`; these methods keep the public
    # facade contract and delegate. Publication policy (private-first,
    # one-use intents, visibility binding) stays canonical in
    # `local_pipeline.publish`; the YouTube wire protocol stays in
    # `local_pipeline.publish_youtube`. See docs/architecture/PUBLISH_SERVICE.md.
    def run_metadata_generation(self, *, job_id=None, actor="console") -> dict:
        return self.publish.generate_metadata(job_id=job_id, actor=actor)

    def write_manual_metadata(self, *, title: str, description: str = "", tags=None,
                              actor="console") -> dict:
        return self.publish.write_manual_metadata(
            title=title, description=description, tags=tags, actor=actor)

    def select_publication_metadata(self, *, title_index: int, description=None,
                                    tags=None, category_id=None,
                                    actor="console") -> dict:
        return self.publish.select_metadata(
            title_index=title_index, description=description, tags=tags,
            category_id=category_id, actor=actor)

    def approve_publication_metadata(self, *, reviewed_sha256: str,
                                     approved_by: str, notes: str = "") -> dict:
        return self.publish.approve_metadata(
            reviewed_sha256=reviewed_sha256, approved_by=approved_by, notes=notes)

    def youtube_provider(self):
        """The publication provider (lazy; reads no secret, makes no call)."""
        return self.publish.provider()

    def publish_credential_state(self) -> dict:
        return self.publish.credential_state()

    def youtube_authorization_url(self, *, redirect_uri: str, state: str = "") -> str:
        return self.publish.authorization_url(redirect_uri=redirect_uri, state=state)

    def complete_youtube_authorization(self, *, code: str, redirect_uri: str,
                                       actor="console") -> dict:
        return self.publish.complete_authorization(
            code=code, redirect_uri=redirect_uri, actor=actor)

    def create_publish_intent(self, *, platform=publish_module.PLATFORM_YOUTUBE,
                              visibility=publish_module.VISIBILITY_PRIVATE,
                              publish_at=None, made_for_kids=False,
                              account_ref="", actor="console") -> dict:
        return self.publish.create_intent(
            platform=platform, visibility=visibility, publish_at=publish_at,
            made_for_kids=made_for_kids, account_ref=account_ref, actor=actor)

    def approve_publish_intent(self, *, reviewed_sha256: str, approved_by: str,
                               notes: str = "") -> dict:
        return self.publish.approve_intent(
            reviewed_sha256=reviewed_sha256, approved_by=approved_by, notes=notes)

    def run_publish_upload(self, *, job_id=None, actor="console") -> dict:
        return self.publish.run_upload(job_id=job_id, actor=actor)

    def approve_publish_visibility(self, *, target_visibility: str,
                                   reviewed_remote_id: str, approved_by: str,
                                   publish_at=None, notes: str = "") -> dict:
        return self.publish.approve_visibility(
            target_visibility=target_visibility,
            reviewed_remote_id=reviewed_remote_id, approved_by=approved_by,
            publish_at=publish_at, notes=notes)

    def run_visibility_change(self, *, job_id=None, actor="console") -> dict:
        return self.publish.apply_visibility(job_id=job_id, actor=actor)

    def publish_state(self) -> dict:
        """View model for the publish screen: what is ready, what is blocking."""
        return self.publish.publish_state()

    # ------------------------------------------------------------- phase 0
    # Phase 0 (assessment + processing plan) is owned by
    # `application.services.AnalysisService`; these methods keep the public
    # facade contract and delegate. See `docs/architecture/ANALYSIS_SERVICE.md`.
    def run_source_assessment(self, *, job_id=None, actor="console") -> dict:
        return self.analysis.assess_source(job_id=job_id, actor=actor)

    def create_processing_plan(self, *, mode: str, selected_by: str, output_count=None,
                               target_duration_sec=None, moment_ranks=None, theory_angle="",
                               theory_angle_auto: bool = False,
                               render_config=None, notes="",
                               content_language=None,
                               narration_preset=None) -> dict:
        return self.analysis.create_processing_plan(
            mode=mode, selected_by=selected_by, output_count=output_count,
            target_duration_sec=target_duration_sec, moment_ranks=moment_ranks,
            theory_angle=theory_angle, theory_angle_auto=theory_angle_auto,
            render_config=render_config, notes=notes,
            content_language=content_language,
            narration_preset=narration_preset)

    def update_processing_plan(self, *, updated_by: str, mode=None, selected_by=None,
                               output_count=None, target_duration_sec=None,
                               moment_ranks=None, theory_angle=None,
                               theory_angle_auto=None,
                               render_config=None, notes=None,
                               content_language=None,
                               narration_preset=None) -> dict:
        """Rebuild an existing plan from editable, validated Phase 0 fields."""
        return self.analysis.update_processing_plan(
            updated_by=updated_by, mode=mode, selected_by=selected_by,
            output_count=output_count, target_duration_sec=target_duration_sec,
            moment_ranks=moment_ranks, theory_angle=theory_angle,
            theory_angle_auto=theory_angle_auto, render_config=render_config,
            notes=notes, content_language=content_language,
            narration_preset=narration_preset)

    def describe_plan_review(self) -> dict:
        return self.analysis.describe_plan_review()

    # ------------------------------------------------------------- approvals
    # The registry lives in `application.approval_catalog` so `ApprovalService`
    # and this facade agree on which artifacts the content-approval gate
    # covers. Publication approvals are deliberately NOT in it (Invariant F).
    APPROVAL_ARTIFACTS = _APPROVAL_ARTIFACTS

    # ------------------------------------------------------- render review
    # The READ side of every checklist (state, digests, paths) is owned by the
    # review gate so the approval precondition and the UI can never disagree.
    # The MUTATION methods below stay here for now; they reuse the gate so the
    # "bound to the current digest" rule has one implementation.
    def render_review_relative(self, slot: int = 1) -> str:
        """Where THIS slot's checklist lives.

        Each Short is watched separately, so each Short owns its own checklist.
        Sharing one file would let ticks earned on slot 1 unlock the approval
        gate of a slot 2 nobody has watched -- the exact failure the checklist
        exists to prevent.
        """
        return self._review_gate.render_review_relative(slot)

    def render_video_digest(self, slot: int = 1):
        """SHA-256 of the render currently on disk, or None.

        The SAME digest the approval gate binds to, so a checklist entry and an
        approval can never disagree about which video was reviewed.
        """
        return self._review_gate.render_video_digest(slot)

    def render_review_state(self, slot: int = 1) -> dict:
        return self._review_gate.render_review_state(slot)

    def _save_render_review(self, review: dict, slot: int = 1) -> dict:
        path = self.storage.resolve(self.ownership, self.render_review_relative(slot),
                                     create_parent=True)
        atomic_write_json(path, review)
        return review

    def _load_or_new_review(self, slot: int = 1) -> dict:
        return self._review_gate.load_or_new_render_review(slot)

    def set_render_review_item(self, key: str, *, checked: bool, reviewed_by: str,
                                note: str = "", slot: int = 1) -> dict:
        slot = self.require_slot(slot)
        digest = self.render_video_digest(slot)
        if not digest:
            raise PipelineServiceError(
                "Incelenecek bir render videosu yok; once render fazini calistir.")
        review = render_review_module.set_item(
            self._load_or_new_review(slot), key, checked=checked, video_sha256=digest,
            reviewed_by=reviewed_by, note=note)
        self._save_render_review(review, slot)
        self.audit.record("artifact.written", actor=reviewed_by,
                          artifact=self.render_review_relative(slot),
                          detail={"checklist_item": key, "checked": checked, "slot": slot})
        return self.render_review_state(slot)

    #: Which revision target the machine can actually act on, and with what job.
    #: `moments` and `phase0` are absent on purpose: choosing a different moment
    #: or changing the plan is a human decision, and a job cannot make it.
    REVISION_JOBS = {
        "render": "render",
        "voice": "tts",
        "script": "script_generation",
    }
    #: Targets whose job spends provider quota. The console asks before starting
    #: these, because a mis-click used to cost nothing and should not silently
    #: start costing a daily quota that allows 20 requests.
    REVISION_JOBS_WITH_PROVIDER_COST = ("voice", "script")

    def revision_action(self, target_tab: str) -> dict:
        """What clicking 'request revision' can DO for this target.

        Returns the plan, never performs it: the caller decides whether the
        operator has confirmed a quota-spending run.
        """
        job_type = self.REVISION_JOBS.get(str(target_tab or ""))
        if job_type is None:
            return {
                "target_tab": target_tab, "runnable": False, "job_type": None,
                "needs_confirmation": False,
                "reason": ("Bu revizyon senin karariniz gerektiriyor "
                           "(moment secimi / plan degisikligi); otomatik "
                           "calistirilamaz."),
            }
        costs = target_tab in self.REVISION_JOBS_WITH_PROVIDER_COST
        return {
            "target_tab": target_tab, "runnable": True, "job_type": job_type,
            "needs_confirmation": costs,
            "reason": ("Bu faz saglayici kotasi harcar; baslatmadan once onay "
                       "istenir." if costs else
                       "Yerel islem; saglayici kotasi harcamaz."),
        }

    def request_render_revision(self, *, requested_by: str, notes: str,
                                 target_tab: str = "moments", slot: int = 1) -> dict:
        slot = self.require_slot(slot)
        digest = self.render_video_digest(slot)
        if not digest:
            raise PipelineServiceError("Revizyon istenecek bir render videosu yok.")
        review = render_review_module.request_revision(
            self._load_or_new_review(slot), video_sha256=digest,
            requested_by=requested_by, notes=notes, target_tab=target_tab,
            # Snapshot taken BEFORE the fix runs, so the report afterwards
            # compares against what the reviewer actually objected to.
            before=render_review_module.technical_summary(
                self.load_artifact("render_manifest", slot=slot)["data"]))
        self._save_render_review(review, slot)
        # A revision request is a NOTE: it changes no artifact, no status, and
        # never touches the video.
        self.audit.record("approval.rejected", actor=requested_by,
                          artifact=self.render_review_relative(slot),
                          detail={"revision_requested": True, "target_tab": target_tab,
                                  "slot": slot})
        return self.render_review_state(slot)

    def clear_render_revision(self, *, resolved_by: str, slot: int = 1) -> dict:
        slot = self.require_slot(slot)
        review = render_review_module.clear_revision(
            self._load_or_new_review(slot), video_sha256=self.render_video_digest(slot),
            resolved_by=resolved_by)
        self._save_render_review(review, slot)
        return self.render_review_state(slot)

    def voice_audio_digest(self):
        """SHA-256 of the voiceover currently on disk, or None.

        The SAME digest the approval gate binds to, so a checklist entry and an
        approval can never disagree about which audio was listened to.
        """
        return self._review_gate.voice_audio_digest()

    def voice_review_state(self) -> dict:
        return self._review_gate.voice_review_state()

    def _save_voice_review(self, review: dict) -> dict:
        path = self.storage.resolve(self.ownership,
                                     voice_review_module.VOICE_REVIEW_ARTIFACT,
                                     create_parent=True)
        atomic_write_json(path, review)
        return review

    def _load_or_new_voice_review(self) -> dict:
        return self._review_gate.load_or_new_voice_review()

    # ------------------------------------------------- copyright review
    def source_fingerprint(self):
        """The digest the copyright answers are bound to."""
        return self._review_gate.source_fingerprint()

    def copyright_review_state(self) -> dict:
        return self._review_gate.copyright_review_state()

    def _save_copyright_review(self, review: dict) -> dict:
        path = self.storage.resolve(
            self.ownership, copyright_review_module.COPYRIGHT_REVIEW_ARTIFACT,
            create_parent=True)
        atomic_write_json(path, review)
        return review

    def _load_or_new_copyright_review(self) -> dict:
        return self._review_gate.load_or_new_copyright_review()

    def set_copyright_review_item(self, key: str, *, checked: bool,
                                  reviewed_by: str, note: str = "") -> dict:
        digest = self.source_fingerprint()
        if not digest:
            raise PipelineServiceError(
                "Once kaynak video yukle; telif kontrolu kaynaga baglanir.")
        review = copyright_review_module.set_item(
            self._load_or_new_copyright_review(), key, checked=checked,
            source_sha256=digest, reviewed_by=reviewed_by, note=note)
        self._save_copyright_review(review)
        self.audit.record(
            "artifact.written", actor=reviewed_by,
            artifact=copyright_review_module.COPYRIGHT_REVIEW_ARTIFACT,
            detail={"checklist_item": key, "checked": checked})
        return self.copyright_review_state()

    def request_copyright_revision(self, *, requested_by: str, notes: str,
                                   target_tab: str = "phase0") -> dict:
        digest = self.source_fingerprint()
        if not digest:
            raise PipelineServiceError(
                "Once kaynak video yukle; telif kontrolu kaynaga baglanir.")
        review = copyright_review_module.request_revision(
            self._load_or_new_copyright_review(), source_sha256=digest,
            notes=notes, requested_by=requested_by, target_tab=target_tab)
        self._save_copyright_review(review)
        return self.copyright_review_state()

    def clear_copyright_revision(self, *, cleared_by: str) -> dict:
        digest = self.source_fingerprint()
        review = copyright_review_module.clear_revision(
            self._load_or_new_copyright_review(), source_sha256=digest,
            resolved_by=cleared_by)
        self._save_copyright_review(review)
        return self.copyright_review_state()

    def set_voice_review_item(self, key: str, *, checked: bool, reviewed_by: str,
                              note: str = "") -> dict:
        digest = self.voice_audio_digest()
        if not digest:
            raise PipelineServiceError(
                "Incelenecek bir seslendirme yok; once TTS fazini calistir.")
        review = voice_review_module.set_item(
            self._load_or_new_voice_review(), key, checked=checked, audio_sha256=digest,
            reviewed_by=reviewed_by, note=note)
        self._save_voice_review(review)
        self.audit.record("artifact.written", actor=reviewed_by,
                          artifact=voice_review_module.VOICE_REVIEW_ARTIFACT,
                          detail={"checklist_item": key, "checked": checked})
        return self.voice_review_state()

    def request_voice_revision(self, *, requested_by: str, notes: str,
                               target_tab: str = "voice") -> dict:
        digest = self.voice_audio_digest()
        if not digest:
            raise PipelineServiceError("Revizyon istenecek bir seslendirme yok.")
        review = voice_review_module.request_revision(
            self._load_or_new_voice_review(), audio_sha256=digest,
            requested_by=requested_by, notes=notes, target_tab=target_tab,
            before=voice_review_module.technical_summary(
                self.load_artifact("selected_voiceover")["data"],
                self.load_artifact("selected_script")["data"]))
        self._save_voice_review(review)
        # A revision request is a NOTE: it changes no artifact, no status, and
        # never touches the audio.
        self.audit.record("approval.rejected", actor=requested_by,
                          artifact=voice_review_module.VOICE_REVIEW_ARTIFACT,
                          detail={"revision_requested": True, "target_tab": target_tab})
        return self.voice_review_state()

    def clear_voice_revision(self, *, resolved_by: str) -> dict:
        review = voice_review_module.clear_revision(
            self._load_or_new_voice_review(), audio_sha256=self.voice_audio_digest(),
            resolved_by=resolved_by)
        self._save_voice_review(review)
        return self.voice_review_state()

    def approve(self, kind: str, *, reviewed_sha256: str, approved_by: str, notes: str = "",
                slot: int = 1) -> dict:
        """Approve through the EXISTING hash-bound gate.

        The console cannot approve anything the gate would refuse: it passes the
        user's echoed hash straight to `approval.approve_artifact`, which
        re-derives the digest from current content.
        """
        return self.approval.approve(
            kind, reviewed_sha256=reviewed_sha256, approved_by=approved_by,
            notes=notes, slot=slot)

    def approval_state(self, kind: str, slot: int = 1) -> dict:
        """Live approval validity, re-derived from current content every time."""
        return self.approval.state(kind, slot)

    def approval_chain(self, slot: int = 1) -> dict:
        """Approval validity INCLUDING upstream dependencies.

        A per-artifact hash check is not enough. Approving a script, then editing
        the processing plan's decision, leaves the script's own digest untouched --
        so `check_approval('script')` still says "valid" even though the strategy
        it was written for no longer exists. The user-visible rule is that a
        change anywhere upstream invalidates everything downstream, so validity is
        computed along the chain:

            processing_plan -> [slot moment_selection] -> script -> voiceover -> slot render

        `script`/`voiceover` are only in the chain for `narrated_theory`; in
        `direct_clip_series` they are never produced and their absence must not
        invalidate the render.

        The chain is evaluated FOR ONE SLOT. Each Short has its own moment
        selection and its own render, so slot 2's render approval must not be
        invalidated by slot 3's stale selection -- they are independent Shorts
        that happen to share a source, a plan and an analysis.

        NOTHING is deleted here. The artifacts, their `human_approval` blocks and
        the audit trail all stay exactly as they were -- this only reports that an
        approval no longer binds.
        """
        return self.approval.chain(slot)

    def invalidated_approvals(self) -> list:
        """Approvals that were granted but no longer bind -- their own content or
        anything upstream of them changed.

        Every slot is checked: a batch can have one Short whose approval still
        binds and another whose source moment was re-analysed underneath it.

        Reported, never deleted: the audit trail of the original approval stays
        intact even after it stops being valid.
        """
        return self.approval.invalidated()

    # ------------------------------------------------------------- phase gate
    # Plan-derived eligibility + slot scope are owned by
    # `application.services.WorkflowGate` (Milestone 9); these methods keep the
    # public facade contract and delegate.
    def plan_gate(self, phase: str) -> tuple:
        return self.workflow_gate.plan_gate(phase)

    def require_phase(self, phase: str) -> None:
        self.workflow_gate.require_phase(phase)

    # ------------------------------------------------------------ output slots
    def slot_count(self) -> int:
        return self.workflow_gate.slot_count()

    def slot_indexes(self) -> list:
        return self.workflow_gate.slot_indexes()

    def require_slot(self, slot: int) -> int:
        return self.workflow_gate.require_slot(slot)

    def slot_moment_rank(self, slot: int):
        return self.workflow_gate.slot_moment_rank(slot)

    # ------------------------------------------------------------ pipeline
    def run_video_analysis(self, *, clip_start_sec=0, clip_duration_sec=240,
                           focus_region=None, clip_duration_min=None,
                           custom_start_min=None, analysis_scope="window",
                           whisper_model_size="small", scene_detector="content",
                           top_n=5, job_id=None, actor="console") -> dict:
        """Phase 1 video analysis -> `hot_moments`. Delegate.

        The orchestration (gate, progress, source resolution, engine call,
        artifact write) lives in `application.services.VideoAnalysisService`;
        the signature is unchanged so job dispatch, notebooks and verify
        scripts keep working.
        """
        return self.video_analysis.run_analysis(
            clip_start_sec=clip_start_sec, clip_duration_sec=clip_duration_sec,
            focus_region=focus_region, clip_duration_min=clip_duration_min,
            custom_start_min=custom_start_min, analysis_scope=analysis_scope,
            whisper_model_size=whisper_model_size, scene_detector=scene_detector,
            top_n=top_n, job_id=job_id, actor=actor)

    def record_moment_selection(self, *, selected_rank: int, reviewed_by: str,
                                rejected_ranks=None, notes: str = "",
                                require_preview: bool = True, slot: int = 1) -> dict:
        """Phase 2 moment selection -> `moment_selection`. Delegate.

        The gate, rank-ownership check and canonical selection build live in
        `application.services.MomentService` (Milestone 11); the signature is
        unchanged so API routes, notebooks and verify scripts keep working.
        """
        return self.moment.record_moment_selection(
            selected_rank=selected_rank, reviewed_by=reviewed_by,
            rejected_ranks=rejected_ranks, notes=notes,
            require_preview=require_preview, slot=slot)

    def moment_rank_owner(self, rank: int, slot: int):
        """Index of another slot that already selected `rank`, or None.

        Producing the same moment twice in one batch is almost always a mistake
        rather than an intent, and it is invisible until both Shorts are rendered
        and watched. It is rejected at selection time, where the fix is one click.
        """
        return self.moment.moment_rank_owner(rank, slot)

    def run_script_generation(self, *, job_id=None, actor="console",
                              enable_google_model_fallback: bool = True,
                              max_google_model_attempts: int = 4,
                              max_google_transient_retries: int = 2) -> dict:
        """Defaults keep the normal Route A behaviour. The three knobs exist so a
        deliberate single-call validation can pin the request to exactly one
        model, one attempt, zero retries."""
        return self.script.run_script_generation(
            job_id=job_id, actor=actor,
            enable_google_model_fallback=enable_google_model_fallback,
            max_google_model_attempts=max_google_model_attempts,
            max_google_transient_retries=max_google_transient_retries)


    def run_tts(self, *, voice="Kore", language_code="tr-TR", narration_direction="",
                job_id=None, actor="console",
                enable_google_model_fallback: bool = True,
                max_google_model_attempts: int = 4,
                max_google_transient_retries: int = 2) -> dict:
        """Defaults keep the normal Route D behaviour. The three knobs mirror
        `run_script_generation`, so a deliberate single-call validation can pin
        the request to exactly one model, one attempt, zero retries."""
        self.require_phase("tts")
        tracker = self.tracker("tts", job_id=job_id)
        tracker.start("Seslendirme basladi")
        try:
            return self._run_tts(
                tracker, voice=voice, language_code=language_code,
                narration_direction=narration_direction, job_id=job_id, actor=actor,
                enable_google_model_fallback=enable_google_model_fallback,
                max_google_model_attempts=max_google_model_attempts,
                max_google_transient_retries=max_google_transient_retries)
        except Exception as exc:  # noqa: BLE001
            tracker.fail(f"{type(exc).__name__}: {exc}")
            raise

    def _run_tts(self, tracker, *, voice, language_code, narration_direction,
                 job_id, actor, enable_google_model_fallback=True,
                 max_google_model_attempts=4, max_google_transient_retries=2) -> dict:
        tracker.stage("preparation", "Onayli script okunuyor")
        script = self.load_artifact("selected_script", required=True)
        # The plan decided the content language; narrating an English script in
        # Turkish because a caller passed a default would be a silent
        # mistranslation of approved content.
        plan_decision = ((self.load_artifact("processing_plan")["data"] or {})
                         .get("decision") or {})
        language_code = plan_decision.get("content_language") or language_code
        # Voice and delivery are part of the APPROVED plan. A caller default
        # would silently narrate in a different voice than the one reviewed.
        narration = plan_decision.get("narration") or {}
        if narration.get("voice"):
            voice = narration["voice"]
        if narration.get("narration_direction"):
            narration_direction = narration["narration_direction"]
        if script["problem"]:
            raise ArtifactProblem(script["problem"])
        if script["data"].get("status") != "approved":
            raise PipelineServiceError(
                "seslendirme icin script ONAYLI olmali "
                f"(su anki durum: {script['data'].get('status')!r})"
            )
        state = approval.check_approval(script["data"], "script")
        if not state["valid"]:
            raise PipelineServiceError(f"script onayi artik gecerli degil: {state['reason']}")

        client = self._build_google_client()
        if client is None:
            raise PipelineServiceError(
                "Gemini kimlik bilgisi yok; TTS calistirilamaz. " +
                google_ai.credential_help_text("google_api")
            )
        out_path = self.storage.resolve(self.ownership, RESULTS_DIRNAME, "tts", "gemini_tts.wav",
                                         create_parent=True)
        tracker.stage("synthesize", "Gemini TTS yaniti bekleniyor")
        result = google_ai.run_google_tts(
            script["data"]["script_text"], out_path, client=client,
            voice=voice, language_code=language_code, narration_direction=narration_direction,
            enable_model_fallback=enable_google_model_fallback,
            max_model_attempts=max_google_model_attempts,
            max_transient_retries=max_google_transient_retries,
        )
        tracker.stage("validate", "Ses dosyasi dogrulaniyor")
        artifact = google_ai.build_tts_artifact(
            result, script["data"]["script_text"],
            script_status=script["data"].get("status", "review_required"),
        )
        if artifact.get("audio_path"):
            artifact["audio_path_relative"] = self.relative(artifact["audio_path"])
        artifact["narration_preset"] = narration.get("preset")
        tracker.stage("write", "Seslendirme artefakti yaziliyor")
        self.write_artifact("selected_voiceover", artifact, job_id=job_id, actor=actor)
        self.audit.record("provider.call", actor=actor, job_id=job_id,
                          detail={"phase": "tts", "model": artifact.get("model")})
        if artifact.get("status") in ("failed", "insufficient_data"):
            tracker.fail(artifact.get("status_reason") or "seslendirme uretilemedi")
        else:
            tracker.finish(message=f"{artifact.get('duration_sec', 0)}s ses hazir")
        return artifact

    # ------------------------------------------------------------- glossary
    def glossary(self) -> dict:
        """Project glossary, or an empty one.

        Deliberately NOT per-slot: a proper name is a fact about the source, not
        about one Short cut from it, and every Short of a project should benefit
        from a name its sibling's reviewer already fixed.
        """
        record = self.load_artifact("glossary")["data"]
        return record if isinstance(record, dict) else glossary_module.new_glossary(
            self.ownership)

    def glossary_view(self) -> dict:
        return glossary_module.summarise(self.glossary())

    def set_glossary_terms(self, terms, *, actor: str) -> dict:
        record = glossary_module.set_terms(self.glossary(), terms, actor=actor)
        relative = self.write_artifact("glossary", record, actor=actor)
        # Count only. The terms are the user's own content; an append-only log
        # that outlives the artifact is the wrong place to copy them into.
        self.audit.record("artifact.written", actor=actor, artifact=relative,
                          detail={"glossary_terms": len(record.get("terms") or [])})
        return self.glossary_view()

    def accept_glossary_suggestion(self, term: str, *, actor: str) -> dict:
        """Promote a suggested term into the list the recogniser is given."""
        record = self.glossary()
        entries = list(glossary_module.terms_of(record))
        entries.append({"term": term, "source": "suggestion"})
        record = glossary_module.set_terms(record, entries, actor=actor)
        key = glossary_module.normalise(term)
        record["suggestions"] = [item for item in (record.get("suggestions") or [])
                                 if item.get("key") != key]
        relative = self.write_artifact("glossary", record, actor=actor)
        self.audit.record("artifact.written", actor=actor, artifact=relative,
                          detail={"glossary_terms": len(record.get("terms") or []),
                                  "accepted_suggestion": True})
        return self.glossary_view()

    # ----------------------------------------------------- transcript edits
    # ----------------------------------------------------- transcript edits
    # Transcript lifecycle lives in `application.services.TranscriptService`
    # (Milestone 12). The methods below are compatibility delegates; the
    # private helpers (_transcript_artifact, _apply_transcript_corrections,
    # _archive_replaced_transcript, _record_glossary_suggestions_from_edit)
    # moved with it.
    def transcript_segments(self, slot: int = 1) -> list:
        return self.transcript.transcript_segments(slot)

    def save_transcript_edits(self, *, segments=None, full_text=None,
                              edited_by: str, slot: int = 1) -> dict:
        return self.transcript.save_transcript_edits(
            segments=segments, full_text=full_text, edited_by=edited_by,
            slot=slot)

    def set_transcript_exclusions(self, *, segment_ids, excluded: bool,
                                  actor: str, slot: int = 1) -> dict:
        return self.transcript.set_transcript_exclusions(
            segment_ids=segment_ids, excluded=excluded, actor=actor, slot=slot)

    def restore_transcript_from_asr(self, *, actor: str, slot: int = 1) -> dict:
        return self.transcript.restore_transcript_from_asr(actor=actor, slot=slot)

    def transcript_view(self, slot: int = 1) -> dict:
        return self.transcript.transcript_view(slot)

    def run_transcript(self, *, subtitle_whisper_model_size="small",
                       subtitle_language="auto", caption_theme="auto",
                       job_id=None, actor="console", slot=1) -> dict:
        return self.transcript.run_transcript(
            subtitle_whisper_model_size=subtitle_whisper_model_size,
            subtitle_language=subtitle_language, caption_theme=caption_theme,
            job_id=job_id, actor=actor, slot=slot)

    def set_transcript_corrections(self, *, corrections, edited_by: str,
                                   slot: int = 1) -> dict:
        return self.transcript.set_transcript_corrections(
            corrections=corrections, edited_by=edited_by, slot=slot)


    def run_render(self, *, render_mode="blurred_background", preview_mode=True,
                   subtitle_whisper_model_size="small", subtitle_language="auto",
                   caption_style="active_word_yellow", caption_theme="auto",
                   job_id=None, actor="console", slot=1) -> dict:
        self.require_phase("render")
        slot = self.require_slot(slot)
        tracker = self.tracker("render", job_id=job_id)
        tracker.start(f"Render basladi (cikti {slot})" if slot > 1 else "Render basladi")
        try:
            return self._run_render(
                tracker, render_mode=render_mode, preview_mode=preview_mode,
                subtitle_whisper_model_size=subtitle_whisper_model_size,
                subtitle_language=subtitle_language,
                caption_style=caption_style, caption_theme=caption_theme,
                job_id=job_id, actor=actor, slot=slot)
        except Exception as exc:  # noqa: BLE001
            tracker.fail(f"{type(exc).__name__}: {exc}")
            raise

    def _run_render(self, tracker, *, render_mode, preview_mode,
                    subtitle_whisper_model_size, subtitle_language,
                    caption_style, caption_theme,
                    job_id, actor, slot=1) -> dict:
        plan = self.load_artifact("processing_plan", required=True)
        if plan["problem"]:
            raise ArtifactProblem(plan["problem"])

        # Render Studio's per-slot profile is the authority on PRESENTATION, so
        # it replaces the caller's four presentation arguments. It cannot touch
        # `subtitle_language` or the whisper model: those decide which WORDS
        # appear, and the drift check below still holds them to the approved
        # plan. A slot with no saved profile resolves to the plan itself, so
        # this changes nothing for a project nobody has edited.
        resolved = self.effective_render_config(slot)
        config = resolved["config"]
        render_mode = config["render_mode"]
        preview_mode = config["preview_mode"]
        caption_style = config["caption_style"]
        caption_theme = config["caption_theme"]
        quality = resolved["quality"]
        caption_overrides = resolved["caption_overrides"]

        drift = source_strategy.check_config_drift(
            plan["data"], render_config={"render_mode": render_mode, "preview_mode": preview_mode,
                                          "subtitle_whisper_model_size": subtitle_whisper_model_size,
                                          "subtitle_language": subtitle_language,
                                          "caption_style": caption_style,
                                          "caption_theme": caption_theme},
            presentation_overridden=resolved["presentation_overridden"])
        if drift:
            raise PipelineServiceError(
                "onaylanan plan ile istenen render ayari uyusmuyor: " + "; ".join(drift))

        hot = self.load_artifact("hot_moments", required=True)
        if hot["problem"]:
            raise ArtifactProblem(hot["problem"])
        render_dir = self.slot_render_dir(slot)
        # Framing now lives in the profile, which migrates the old
        # `render_adjustment` artifact on read -- so there is exactly one source
        # of truth and a project that used the revision-note framing commands
        # still renders with the framing it was given.
        frame = resolved["framing"]
        pipeline_mode = plan["data"]["pipeline_mode"]

        if pipeline_mode == "original_audio_subtitles":
            selection = self.load_artifact("moment_selection", required=True, slot=slot)
            if selection["problem"]:
                raise ArtifactProblem(selection["problem"])
            # The caption text is a human decision in this mode, exactly as the
            # script is in the narrated mode. Rendering without it would burn in
            # words nobody read -- the failure this phase was added to stop.
            transcript = self.load_artifact("moment_transcript", slot=slot)
            if transcript["problem"]:
                raise ArtifactProblem(transcript["problem"])
            transcript_state = self.approval_state("transcript", slot)
            if not transcript_state.get("valid"):
                raise PipelineServiceError(
                    "Render acilmadi: once Altyazi sekmesinde bu ciktinin transkriptini "
                    "uret, oku ve onayla. " + (transcript_state.get("reason") or ""))
            manifest = render_utils.run_original_audio_subtitle_pipeline(
                hot["data"], selection["data"],
                self.relative(self.path("hot_moments")),
                self.relative(self.path("moment_selection", slot)),
                moment_transcript=transcript["data"],
                render_dir=render_dir, render_mode=render_mode, preview_mode=preview_mode,
                encode_profile=quality,
                target_width=quality["target_width"],
                target_height=quality["target_height"],
                subtitle_whisper_model_size=subtitle_whisper_model_size,
                subtitle_language=subtitle_language,
                caption_style=caption_style, caption_theme=caption_theme,
                caption_font_size=caption_overrides["font_size"],
                caption_margin_v=caption_overrides["margin_v"],
                caption_active_color_ass=caption_overrides["active_color_ass"],
                frame_zoom=frame["frame_zoom"],
                frame_focus_x=frame["frame_focus_x"],
                frame_focus_y=frame["frame_focus_y"],
                source_video_path_override=str(self.source_path()),
                source_bounds_override=self.clip_bounds_override(slot),
                progress=tracker,
            )
        else:
            script = self.load_artifact("selected_script", required=True)
            voice = self.load_artifact("selected_voiceover", required=True)
            subtitle_track = self.load_artifact("subtitle_track")
            for loaded in (script, voice, subtitle_track):
                if loaded["problem"]:
                    raise ArtifactProblem(loaded["problem"])
            manifest = render_utils.run_render_pipeline(
                hot["data"], script["data"], voice["data"],
                self.relative(self.path("hot_moments")),
                self.relative(self.path("selected_script")),
                self.relative(self.path("selected_voiceover")),
                render_dir=render_dir, render_mode=render_mode, preview_mode=preview_mode,
                encode_profile=quality,
                target_width=quality["target_width"],
                target_height=quality["target_height"],
                frame_zoom=frame["frame_zoom"],
                frame_focus_x=frame["frame_focus_x"],
                frame_focus_y=frame["frame_focus_y"],
                source_video_path_override=str(self.source_path()),
                subtitle_cues_override=(subtitle_track["data"] or {}).get("cues"),
                source_bounds_override=self.clip_bounds_override(slot),
                progress=tracker,
            )
        manifest["output_slot"] = int(slot)
        # Stamp the profile that produced this video. Without it the screen
        # cannot tell "you changed the theme and have not re-rendered" from
        # "this is current", and would have to guess -- so it would either nag
        # about renders that are fine or stay silent about ones that are stale.
        manifest["render_profile"] = {
            "content_sha256": render_profile_module.content_sha256(
                resolved["profile_settings"]),
            "settings": resolved["profile_settings"],
            "quality_preset": quality["quality_preset"],
            "is_delivery_quality": quality["is_delivery_quality"],
        }
        self.write_artifact("render_manifest", manifest, job_id=job_id, actor=actor, slot=slot)
        # A render that failed technical validation is NOT a completed render,
        # and the panel must not claim otherwise.
        if manifest.get("status") == "failed":
            tracker.fail(manifest.get("status_reason") or "teknik dogrulama basarisiz",
                         message="Render teknik dogrulamayi gecemedi.")
        else:
            details = ((manifest.get("output") or {}).get("validation") or {}).get("details") or {}
            tracker.finish(message=f"{details.get('resolution', 'video')} hazir, inceleme bekliyor")
        return manifest

    def slot_render_dir(self, slot: int = 1):
        """Filesystem directory this slot renders into.

        Derived from the same rule as `artifact_relative`, so a slot's manifest
        and its video, subtitles and ASS file can never end up in different
        directories.
        """
        index = int(slot or 1)
        if index == 1:
            return self.storage.resolve(self.ownership, RESULTS_DIRNAME, "render",
                                         create_parent=True)
        return self.storage.resolve(self.ownership, RESULTS_DIRNAME, "render",
                                     f"output_{index:02d}", create_parent=True)

    # ------------------------------------------------------------- helpers
    def _call(self, factory):
        return factory() if callable(factory) else None

    def _build_google_client(self):
        if callable(self._google_client_factory):
            return self._google_client_factory()
        credential = self.secrets.credential("google_api")
        if not credential.configured:
            return None
        return google_ai.build_google_client(credential)

    def outputs(self) -> list:
        """Produced Shorts + their manifests, for the history screen.

        Reports a manifest whose media is missing or empty as a PROBLEM rather
        than listing it as a downloadable output.
        """
        results = []
        for index in self.slot_indexes():
            manifest = self.load_artifact("render_manifest", slot=index)
            selection = self.load_artifact("moment_selection", slot=index)
            entry = {
                "index": index,
                "relative_path": self.artifact_relative("render_manifest", index),
                "status": None,
                "problem": manifest["problem"] or selection["problem"],
                "video": None,
                "moment_rank": (selection["data"] or {}).get("selected_moment_rank"),
                "moment_selected": bool(selection["data"]),
                "planned_moment_rank": self.slot_moment_rank(index),
            }
            if manifest["data"]:
                entry["status"] = manifest["data"].get("status")
                video_path = (manifest["data"].get("output") or {}).get("final_video_path")
                entry["video"] = self._describe_media(video_path)
                if manifest["data"].get("status") in ("completed", "approved", "review_required") \
                        and entry["video"] and not entry["video"]["usable"]:
                    entry["problem"] = (
                        f"manifest '{manifest['data'].get('status')}' diyor ama video kullanilamaz: "
                        f"{entry['video']['problem']}"
                    )
            elif not entry["problem"]:
                # Distinguish "waiting for a human decision" from "rendered and
                # broken": the batch board offers a different action for each.
                entry["status"] = "insufficient_data"
                entry["problem"] = (
                    "bu cikti yuvasi icin moment secilmedi"
                    if not entry["moment_selected"]
                    else "moment secildi, render henuz calistirilmadi"
                )
            results.append(entry)
        return results

    def batch_state(self) -> dict:
        """What the batch board shows and what the batch queue may run.

        One place decides, for every slot, whether it is waiting on a human, ready
        for the machine, or finished -- so the board, the queue and the automation
        engine can never disagree about which Shorts are runnable.
        """
        plan = self.load_artifact("processing_plan")["data"] or {}
        decision = plan.get("decision") or {}
        plan_ok = self.approval_state("processing_plan").get("valid")
        render_allowed, render_reason = self.plan_gate("render")

        slots = []
        for entry in self.outputs():
            index = entry["index"]
            chain = self.approval_chain(index)
            render_state = chain.get("render") or {}
            review = self.render_review_state(index)
            rendered = bool(entry.get("video") and entry["video"].get("usable"))
            if not entry.get("moment_selected"):
                state, action = "needs_moment", "Moment sec"
            elif not rendered:
                state, action = "ready_to_render", "Render et"
            elif render_state.get("approved") and render_state.get("effective_valid"):
                state, action = "approved", None
            elif review.get("open_revision"):
                state, action = "needs_revision", "Revizyonu gider"
            else:
                state, action = "needs_review", "Izle ve onayla"
            slots.append({
                **entry,
                "state": state,
                "next_action": action,
                "runnable": state == "ready_to_render" and bool(render_allowed) and bool(plan_ok),
                "review_checked": review.get("done_count"),
                "review_total": review.get("total_count"),
                "review_complete": review.get("complete"),
                "approved": state == "approved",
            })
        return {
            "mode": decision.get("mode"),
            "output_count": len(slots),
            "plan_approved": bool(plan_ok),
            "render_allowed": bool(render_allowed),
            "render_blocker": None if render_allowed else render_reason,
            "slots": slots,
            "runnable_slots": [s["index"] for s in slots if s["runnable"]],
            "approved_count": sum(1 for s in slots if s["approved"]),
            "rendered_count": sum(1 for s in slots
                                  if s.get("video") and s["video"].get("usable")),
        }

    # ------------------------------------------------------------- faz 2 scene editor
    # EDL use cases are owned by `application.services.EditorService`; these
    # methods keep the public facade contract and delegate.
    def build_edl(self, *, source_path: str | None = None,
                  source_duration: float, title: str = "", actor="console") -> dict:
        return self.editor.create_edl(source_path=source_path,
                                      source_duration=source_duration,
                                      title=title, actor=actor)

    def load_edl(self) -> scene_editor.EDL:
        return self.editor.load_edl()

    def update_edl_clip(self, clip_index: int, *, start=None, end=None,
                        crop=None, zoom=None, caption_style=None,
                        b_roll=None, add_b_roll=None, actor="console") -> dict:
        return self.editor.update_clip(
            clip_index, start=start, end=end, crop=crop, zoom=zoom,
            caption_style=caption_style, b_roll=b_roll,
            add_b_roll=add_b_roll, actor=actor)

    def set_edl_music(self, music_path: str, actor="console") -> dict:
        return self.editor.set_music(music_path, actor=actor)

    def build_edl_preview(self, output_name: str = "preview.mp4") -> list[str]:
        return self.editor.build_preview_command(output_name)

    def add_edl_clip(self, *, source_path: str, source_start_sec: float = 0.0,
                     source_end_sec: float, actor="console") -> dict:
        return self.editor.add_clip(source_path=source_path,
                                    source_start_sec=source_start_sec,
                                    source_end_sec=source_end_sec, actor=actor)

    def render_edl_preview(self, output_name: str = "preview.mp4") -> dict:
        # Render EXECUTION stays in the facade until a RenderService exists: it
        # resolves the ffmpeg binary and spawns a subprocess. The editor service
        # owns only the deterministic command compilation (build_edl_preview).
        edl = self.load_edl()
        if not edl.is_valid():
            raise PipelineServiceError("EDL gecerli degil")
        render_dir = self.storage.resolve(self.ownership, RESULTS_DIRNAME, "render",
                                          create_parent=True)
        output = render_dir / output_name
        ffmpeg_bin, _ = self.binaries()
        return scene_editor.render_preview(edl, output, ffmpeg_bin=ffmpeg_bin)

    def request_edl_revision(self, *, notes: str, requested_by: str = "console") -> dict:
        return self.editor.request_revision(notes=notes,
                                            requested_by=requested_by)

    def resolve_edl_revision(self, *, resolved_by: str = "console") -> dict:
        return self.editor.resolve_revision(resolved_by=resolved_by)

    def _describe_media(self, path) -> dict:
        info = {"relative_path": None, "size_bytes": 0, "usable": False, "problem": None}
        if not path:
            info["problem"] = "manifest bir video yolu icermiyor"
            return info
        try:
            resolved = self.storage.resolve(self.ownership, path) if not Path(path).is_absolute() \
                else Path(path)
            info["relative_path"] = self.relative(resolved)
        except (StorageViolation, ValueError):
            info["problem"] = "video yolu bu proje namespace'inin disinda"
            return info
        if not resolved.is_file():
            info["problem"] = "manifestte adi gecen video dosyasi diskte yok"
            return info
        info["size_bytes"] = resolved.stat().st_size
        if info["size_bytes"] <= 0:
            info["problem"] = "video dosyasi 0 bayt (yarim/basarisiz render)"
            return info
        info["usable"] = True
        return info
