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
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from local_pipeline import (
    approval,
    google_ai,
    render_utils,
    script_generation,
    source_strategy,
    tts as tts_module,
    video_analysis,
)

from .audit import AuditLog
from . import progress as progress_module
from . import render_review as render_review_module
from . import voice_review as voice_review_module
from .ids import Ownership, new_id, safe_filename
from .storage import StorageViolation, atomic_write_json

#: Artifact names inside a project namespace. Relative on purpose: an absolute
#: host path must never reach the browser or an artifact field.
ARTIFACTS = {
    "project": "project.json",
    "source_assessment": "results/source_assessment.json",
    "processing_plan": "results/processing_plan.json",
    "hot_moments": "results/hot_moments.json",
    "moment_selection": "results/moment_selection.json",
    "selected_script": "results/selected_script.json",
    "selected_voiceover": "results/selected_voiceover.json",
    "moment_transcript": "results/moment_transcript.json",
    "render_manifest": "results/render/render_manifest.json",
}

#: Artifacts a batch produces once PER SHORT. Everything else in ARTIFACTS
#: describes the project as a whole and is shared by every slot: the source
#: assessment, the approved plan and the analysis are the common ground all the
#: Shorts of one project are cut from, and duplicating them per slot would let
#: two Shorts of the same project disagree about their own source.
#:
#: Narration artifacts are absent on purpose: `build_processing_plan` refuses a
#: `narrated_theory` plan with output_count != 1, so a narrated project has
#: exactly one slot and its script/voiceover cannot be ambiguous.
PER_SLOT_ARTIFACTS = frozenset({"moment_selection", "moment_transcript", "render_manifest"})

#: A full-source scan does not run the window pipeline's stages, so it declares
#: its own. Showing a window run's "Whisper transkript" stage during a scan that
#: transcribes several regions would describe work that is not happening.
FULL_SOURCE_ANALYSIS_STAGES = (
    {"key": "full_scan", "label": "Tum kaynak taraniyor"},
    {"key": "region_analysis", "label": "Aday bolgeler inceleniyor"},
    {"key": "scoring", "label": "Film geneli siralama"},
    {"key": "previews", "label": "Onizleme klipleri"},
)

SOURCE_DIRNAME = "source_videos"
RESULTS_DIRNAME = "results"
RUNTIME_DIRNAME = ".runtime"

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

#: Artifact kind -> the schema fields that must exist for it to be usable at all.
REQUIRED_FIELDS = {
    "source_assessment": ("artifact_type", "status", "source", "mode_scores"),
    "processing_plan": ("artifact_type", "status", "decision", "phase_gates"),
    "hot_moments": ("status", "hot_moments"),
    "moment_selection": ("artifact_type", "status", "selected_moment_rank"),
    "selected_script": ("status", "provider_attempts"),
    "selected_voiceover": ("status",),
    "moment_transcript": ("artifact_type", "status", "words", "cues"),
    "render_manifest": ("status", "output", "approval"),
}

SUPPORTED_SCHEMA_VERSIONS = {1}

# Rewinding never deletes user work.  The selected stage and everything after
# it is moved into `.revisions/<id>/...`, while upstream inputs remain live.
REWIND_PATHS = {
    "assessment": (
        "results/source_assessment.json", "results/processing_plan.json",
        "results/hot_moments.json", "results/moment_previews",
        "results/moment_selection.json", "results/selected_script.json",
        "results/selected_voiceover.json", "results/tts", "results/voice_review.json",
        "results/render", "results/render_review.json", "results/automation.json",
        "progress",
    ),
    "plan": (
        "results/processing_plan.json", "results/hot_moments.json",
        "results/moment_previews", "results/moment_selection.json",
        "results/selected_script.json", "results/selected_voiceover.json",
        "results/tts", "results/voice_review.json", "results/render",
        "results/render_review.json", "results/automation.json", "progress",
    ),
    "analysis": (
        "results/hot_moments.json", "results/moment_previews",
        "results/moment_selection.json", "results/selected_script.json",
        "results/selected_voiceover.json", "results/tts", "results/voice_review.json",
        "results/render", "results/render_review.json", "results/automation.json",
        "progress",
    ),
    "moment": (
        "results/moment_selection.json", "results/selected_script.json",
        "results/selected_voiceover.json", "results/tts", "results/voice_review.json",
        "results/render", "results/render_review.json", "results/automation.json",
        "progress",
    ),
    "script": (
        "results/selected_script.json", "results/selected_voiceover.json",
        "results/tts", "results/voice_review.json", "results/render",
        "results/render_review.json", "results/automation.json", "progress",
    ),
    "voice": (
        "results/selected_voiceover.json", "results/tts", "results/voice_review.json",
        "results/render", "results/render_review.json", "results/automation.json",
        "progress",
    ),
    "render": (
        "results/render", "results/render_review.json", "results/automation.json",
        "progress",
    ),
}

REWIND_LABELS = {
    "assessment": "Faz 0 degerlendirme",
    "plan": "isleme plani",
    "analysis": "video analizi",
    "moment": "moment secimi",
    "script": "script",
    "voice": "seslendirme",
    "render": "render",
}


class PipelineServiceError(RuntimeError):
    pass


class ArtifactProblem(PipelineServiceError):
    """An artifact exists but cannot be trusted. Never downgraded to a default."""


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class PipelineService:
    """Ownership-scoped facade over the canonical pipeline modules."""

    def __init__(self, storage, ownership: Ownership, secret_resolver, *,
                 google_client_factory=None, openai_client_factory=None,
                 anthropic_client_factory=None, elevenlabs_client_factory=None,
                 probe_fn=None, ffmpeg_bin=None, ffprobe_bin=None, audit: AuditLog | None = None):
        self.storage = storage
        self.ownership = ownership
        self.secrets = secret_resolver
        self.audit = audit or AuditLog(storage, ownership)
        # Injected factories: nothing in this class constructs a real SDK client
        # on its own, so a test can guarantee zero network access.
        self._google_client_factory = google_client_factory
        self._openai_client_factory = openai_client_factory
        self._anthropic_client_factory = anthropic_client_factory
        self._elevenlabs_client_factory = elevenlabs_client_factory
        self._probe_fn = probe_fn
        self._ffmpeg_bin = ffmpeg_bin
        self._ffprobe_bin = ffprobe_bin
        self._cancel_check = None

    def bind_cancel_check(self, callback) -> None:
        """Bind the current job's ownership-scoped cooperative stop signal."""
        self._cancel_check = callback

    # ------------------------------------------------------------------ paths
    def tracker(self, activity: str, *, job_id=None, attempt: int = 1, stages=None):
        """A progress writer for one activity.

        Separate from the audit log on purpose: the audit log records what
        HAPPENED (append-only, for accountability), progress records what is
        happening RIGHT NOW (overwritten, for the UI).
        """
        return progress_module.ProgressTracker(
            self.storage, self.ownership, activity,
            job_id=job_id, attempt=attempt, stages=stages,
            cancel_check=self._cancel_check)

    def artifact_relative(self, key: str, slot: int = 1) -> str:
        """Namespace-relative path of an artifact FOR ONE OUTPUT SLOT.

        Only the artifacts a batch produces once per Short are slot-scoped. The
        source assessment, the plan and the analysis describe the project as a
        whole and are deliberately shared: re-deriving them per slot would let
        two Shorts of the same project disagree about the source they came from.

        Slot 1 resolves to the historical path, so every existing project,
        artifact and test keeps working unchanged.
        """
        relative = ARTIFACTS[key]
        index = int(slot or 1)
        if index < 1:
            raise PipelineServiceError(f"gecersiz cikti yuvasi: {slot!r}")
        if index == 1 or key not in PER_SLOT_ARTIFACTS:
            return relative
        name = relative.rsplit("/", 1)[-1]
        return f"{RESULTS_DIRNAME}/render/output_{index:02d}/{name}"

    def path(self, key: str, slot: int = 1) -> Path:
        return self.storage.resolve(self.ownership, self.artifact_relative(key, slot),
                                     create_parent=True)

    def relative(self, path) -> str:
        return self.storage.relative_to_namespace(self.ownership, path)

    def binaries(self):
        if self._ffmpeg_bin is None or self._ffprobe_bin is None:
            self._ffmpeg_bin, self._ffprobe_bin = render_utils.resolve_ffmpeg_binaries()
        return self._ffmpeg_bin, self._ffprobe_bin

    # -------------------------------------------------------------- artifacts
    def load_artifact(self, key: str, *, required: bool = False, slot: int = 1) -> dict:
        """Read one artifact and REPORT its trustworthiness.

        Returns {'present','data','problem','relative_path'}. A corrupt or
        incompatible artifact yields `data=None` and a human-actionable
        `problem` -- it is never repaired, guessed at, or replaced by a default.
        """
        relative = self.artifact_relative(key, slot)
        info = {"key": key, "slot": int(slot or 1), "present": False, "data": None,
                "problem": None, "relative_path": relative}
        try:
            path = self.storage.resolve(self.ownership, relative)
        except StorageViolation as exc:
            info["problem"] = f"gecersiz artefakt yolu: {exc}"
            return info
        if not path.is_file():
            if required:
                info["problem"] = f"{relative} bulunamadi"
            return info
        info["present"] = True

        raw = path.read_bytes()
        if not raw.strip():
            info["problem"] = f"{relative} BOS (0 bayt); artefakt kullanilamaz"
            return info
        try:
            data = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError:
            info["problem"] = f"{relative} UTF-8 olarak okunamadi (bozuk dosya)"
            return info
        except json.JSONDecodeError as exc:
            info["problem"] = (
                f"{relative} gecerli JSON degil (satir {exc.lineno}, sutun {exc.colno}); "
                "dosya yarim yazilmis veya bozulmus olabilir"
            )
            return info
        if not isinstance(data, dict):
            info["problem"] = f"{relative} beklenen JSON nesnesi degil ({type(data).__name__})"
            return info

        version = data.get("schema_version")
        if version is not None and version not in SUPPORTED_SCHEMA_VERSIONS:
            info["problem"] = (
                f"{relative} desteklenmeyen schema_version={version!r} "
                f"(desteklenen: {sorted(SUPPORTED_SCHEMA_VERSIONS)}); artefakt yeniden uretilmeli"
            )
            return info
        missing = [f for f in REQUIRED_FIELDS.get(key, ()) if f not in data]
        if missing:
            info["problem"] = f"{relative} zorunlu alanlari eksik: {missing}"
            return info
        if data.get("status") == "production_ready":
            info["problem"] = (
                f"{relative} 'production_ready' iceriyor; bu proje hicbir artefakti "
                "otomatik olarak production_ready yapmaz, dosya elle degistirilmis olabilir"
            )
            return info

        info["data"] = data
        return info

    def load_artifact_path(self, relative: str):
        """Read one JSON artifact by RELATIVE path (not a registered key).

        Used by the automation engine for its own state file. Returns None when
        absent and None when unreadable -- a corrupt automation file must not
        wedge the console; the engine simply starts from a fresh idle state.
        """
        try:
            path = self.storage.resolve(self.ownership, relative)
        except StorageViolation:
            return None
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def write_artifact(self, key: str, data, *, job_id=None, actor="console", slot: int = 1) -> str:
        path = self.path(key, slot)
        atomic_write_json(path, data)
        relative = self.relative(path)
        self.audit.record("artifact.written", actor=actor, job_id=job_id, artifact=relative,
                          detail={"status": data.get("status")})
        return relative

    # ---------------------------------------------------------------- project
    def create_project(self, *, title: str, created_by: str,
                       operator_name: str | None = None) -> dict:
        operator = str(operator_name or created_by or "console").strip()[:120]
        record = {
            "schema_version": 1,
            "artifact_type": "console_project",
            **self.ownership.to_dict(),
            "title": str(title or "Isimsiz proje").strip()[:200],
            "notes": "",
            "created_by": str(created_by or "console")[:120],
            "operator_name": operator,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "status": "review_required",
            "status_reason": "proje olusturuldu; kaynak video ve Faz 0 plani bekleniyor",
            "source": None,
            "not_uploaded": True,
        }
        self.storage.ensure_namespace(self.ownership)
        self.write_artifact("project", record, actor=created_by)
        self.audit.record("project.created", actor=created_by,
                          detail={"title": record["title"]})
        return record

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

    # ------------------------------------------------------- project lifecycle
    def _archive_revision(self, paths, *, action: str, actor: str,
                          target: str | None = None) -> dict:
        """Move exact namespace-relative paths into a recoverable snapshot."""
        revision_id = new_id("revision")
        moved = []
        # Parents replace their children; accepting both would make the second
        # move look like missing data.  Shortest paths first makes this explicit.
        selected = []
        for relative in sorted(set(paths), key=lambda value: (value.count("/"), value)):
            if any(relative == parent or relative.startswith(parent + "/")
                   for parent in selected):
                continue
            selected.append(relative)
        for relative in selected:
            source = self.storage.resolve(self.ownership, relative)
            if not source.exists():
                continue
            destination = self.storage.resolve(
                self.ownership, ".revisions", revision_id, relative,
                create_parent=True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            moved.append(relative)
        manifest = {
            "schema_version": 1,
            "artifact_type": "project_revision",
            **self.ownership.to_dict(),
            "revision_id": revision_id,
            "action": action,
            "target": target,
            "created_by": str(actor or "console")[:120],
            "created_at": _now_iso(),
            "moved_paths": moved,
            "recoverable": True,
            "not_uploaded": True,
        }
        manifest_path = self.storage.resolve(
            self.ownership, ".revisions", revision_id, "manifest.json",
            create_parent=True)
        atomic_write_json(manifest_path, manifest)
        return manifest

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
        clean = str(script_text or "").strip()
        who = str(updated_by or "").strip()
        if not who:
            raise PipelineServiceError("scripti duzenleyen kisi zorunlu")
        if not clean:
            raise PipelineServiceError("script metni bos olamaz")
        if len(clean) > 20000:
            raise PipelineServiceError("script metni en fazla 20000 karakter olabilir")
        loaded = self.load_artifact("selected_script", required=True)
        if loaded["problem"]:
            raise ArtifactProblem(loaded["problem"])
        artifact = loaded["data"]
        old_hash = approval.content_hash_for("script", artifact)
        if clean == artifact.get("script_text"):
            return artifact
        downstream = tuple(path for path in REWIND_PATHS["voice"])
        revision = self._archive_revision(
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
            "updated_by": who[:120], "updated_at": _now_iso(),
            "notes": str(notes or "").strip()[:1000] or None,
            "previous_script_sha256": old_hash,
            "revision_id": revision["revision_id"],
        })
        artifact["manual_edits"] = artifact["manual_edits"][:50]
        self.write_artifact("selected_script", artifact, actor=who)
        self.audit.record("script.updated", actor=who,
                          artifact=ARTIFACTS["selected_script"], detail={
                              "previous_script_sha256": old_hash,
                              "revision_id": revision["revision_id"],
                              "downstream_archived": revision["moved_paths"],
                          })
        return artifact

    def register_source_video(self, *, filename: str, payload: bytes,
                              registered_by: str, track: bool = False) -> dict:
        """Store an uploaded source video inside the project namespace.

        The display filename is sanitized (never trusted as a path) and the stored
        name is prefixed with a generated id, so two uploads called `video.mp4`
        cannot collide or overwrite each other.
        """
        tracker = (self.tracker("upload") if track else progress_module.NullTracker())
        tracker.start("Yukleme basladi")
        try:
            return self._register_source_video_stream(
                tracker, filename=filename, stream=io.BytesIO(payload),
                content_length=len(payload), registered_by=registered_by)
        except Exception as exc:  # noqa: BLE001
            tracker.fail(f"{type(exc).__name__}: {exc}")
            raise

    def register_source_video_stream(self, *, filename: str, stream,
                                     content_length: int, registered_by: str,
                                     track: bool = True) -> dict:
        """Stream a large local upload to disk without buffering it in RAM."""
        tracker = (self.tracker("upload") if track else progress_module.NullTracker())
        tracker.start("Yukleme basladi")
        try:
            return self._register_source_video_stream(
                tracker, filename=filename, stream=stream,
                content_length=content_length, registered_by=registered_by)
        except Exception as exc:  # noqa: BLE001
            tracker.fail(f"{type(exc).__name__}: {exc}")
            raise

    def _register_source_video_stream(self, tracker, *, filename, stream,
                                      content_length, registered_by) -> dict:
        try:
            expected = int(content_length)
        except (TypeError, ValueError) as exc:
            raise PipelineServiceError("yukleme boyutu gecersiz") from exc
        if expected <= 0:
            raise PipelineServiceError("bos dosya yuklenemez")
        display = safe_filename(filename, default="source")
        extension = Path(display).suffix.lower()
        if extension not in SUPPORTED_SOURCE_VIDEO_EXTENSIONS:
            supported = ", ".join(sorted(SUPPORTED_SOURCE_VIDEO_EXTENSIONS))
            raise PipelineServiceError(
                f"desteklenmeyen kaynak video uzantisi: {extension or '(yok)'}; "
                f"desteklenenler: {supported}"
            )
        stored_name = f"{new_id('source')}__{display}"
        path = self.storage.resolve(self.ownership, SOURCE_DIRNAME, stored_name,
                                     create_parent=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        # ``stored_name`` already contains a fresh source id, so this temporary
        # name is collision-free without expanding the closed public id catalog.
        partial = path.with_name(f".{path.name}.part")
        digest = hashlib.sha256()
        received = 0
        tracker.stage("receive", f"{display} aliniyor ve diske yaziliyor")
        tracker.count(0, expected, unit="bayt", message=f"0/{expected} bayt")
        try:
            with open(partial, "xb") as output:
                while received < expected:
                    chunk = stream.read(min(8 * 1024 * 1024, expected - received))
                    if not chunk:
                        raise PipelineServiceError(
                            f"yukleme yarida kesildi ({received}/{expected} bayt)")
                    output.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    tracker.count(received, expected, unit="bayt",
                                  message=f"{received}/{expected} bayt")
                output.flush()
                os.fsync(output.fileno())
            tracker.stage("store", f"{display} atomik olarak etkinlestiriliyor")
            os.replace(partial, path)
        except BaseException:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        tracker.stage("fingerprint", "Parmak izi hesaplaniyor")

        project = self.load_artifact("project")
        record = project["data"] or self.create_project(title=display, created_by=registered_by)
        record["source"] = {
            "display_name": display,
            "stored_path": self.relative(path),
            "size_bytes": received,
            "container_extension": extension,
            "content_sha256": digest.hexdigest(),
            "fingerprint_sha256": source_strategy.source_fingerprint_sha256(path),
            "registered_at": _now_iso(),
            "registered_by": str(registered_by)[:120],
        }
        record["status_reason"] = "kaynak video kaydedildi; Faz 0 degerlendirmesi bekleniyor"
        self.write_artifact("project", record, actor=registered_by)
        self.audit.record("source.registered", actor=registered_by,
                          detail={"display_name": display, "size_bytes": received})
        tracker.finish(message=f"{display} ({received} bayt) kaydedildi")
        return record

    def source_path(self) -> Path:
        project = self.load_artifact("project", required=True)
        if project["problem"]:
            raise ArtifactProblem(project["problem"])
        source = (project["data"] or {}).get("source")
        if not source or not source.get("stored_path"):
            raise PipelineServiceError("bu projede kayitli bir kaynak video yok")
        path = self.storage.resolve(self.ownership, source["stored_path"])
        if not path.is_file():
            raise ArtifactProblem(
                f"kaynak video manifestte var ama diskte yok: {source['stored_path']}"
            )
        return path

    # ------------------------------------------------------------- phase 0
    def run_source_assessment(self, *, job_id=None, actor="console") -> dict:
        tracker = self.tracker("source_assessment", job_id=job_id)
        tracker.start("Kaynak degerlendirme basladi")
        try:
            return self._run_source_assessment(tracker, job_id=job_id, actor=actor)
        except Exception as exc:  # noqa: BLE001 - record, then re-raise unchanged
            tracker.fail(f"{type(exc).__name__}: {exc}")
            raise

    def _run_source_assessment(self, tracker, *, job_id, actor) -> dict:
        tracker.stage("probe", "Medya akislari okunuyor")
        source = self.source_path()
        ffmpeg_bin, ffprobe_bin = self.binaries()
        hot = self.load_artifact("hot_moments")
        tracker.stage("scoring", "Mod skorlari hesaplaniyor")
        assessment = source_strategy.assess_source(
            source, ffmpeg_bin, ffprobe_bin,
            hot_moments_data=hot["data"] if hot["data"] else None,
            google_credential_configured=self.secrets.reference("google_api").configured,
            probe_fn=self._probe_fn,
        )
        tracker.stage("write", "Degerlendirme yaziliyor")
        relative = self.write_artifact("source_assessment", assessment, job_id=job_id, actor=actor)
        self.audit.record("assessment.created", actor=actor, job_id=job_id, artifact=relative,
                          detail={"recommended_mode": assessment["recommended_mode"],
                                   "auto_selected": assessment["auto_selected"]})
        tracker.finish(message=f"Oneri: {assessment['recommended_mode']}")
        return assessment

    def create_processing_plan(self, *, mode: str, selected_by: str, output_count=None,
                               target_duration_sec=None, moment_ranks=None, theory_angle="",
                               render_config=None, notes="",
                               content_language=None,
                               narration_preset=None) -> dict:
        assessment = self.load_artifact("source_assessment", required=True)
        if assessment["problem"]:
            raise ArtifactProblem(assessment["problem"])
        render_root = self.storage.resolve(self.ownership, RESULTS_DIRNAME, "render",
                                            create_parent=True)
        plan = source_strategy.build_processing_plan(
            assessment["data"], mode, selected_by, output_count=output_count,
            target_duration_sec=target_duration_sec, moment_ranks=moment_ranks,
            theory_angle=theory_angle, render_config=render_config,
            content_language=content_language,
            narration_preset=narration_preset,
            render_root=render_root, notes=notes,
        )
        # Store namespace-relative output dirs so the artifact never carries the
        # operator's absolute filesystem layout.
        for slot in plan["outputs"]:
            for field in ("output_dir", "render_manifest_path", "final_video_path"):
                slot[field] = self.relative(slot[field])
        relative = self.write_artifact("processing_plan", plan, actor=selected_by)
        self.audit.record("plan.created", actor=selected_by, artifact=relative,
                          detail={"mode": mode, "output_count": plan["decision"]["output_count"],
                                   "followed_recommendation": plan["followed_recommendation"]})
        return plan

    def update_processing_plan(self, *, updated_by: str, mode=None, selected_by=None,
                               output_count=None, target_duration_sec=None,
                               moment_ranks=None, theory_angle=None,
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
        assessment = self.load_artifact("source_assessment", required=True)
        current = self.load_artifact("processing_plan", required=True)
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

        render_root = self.storage.resolve(
            self.ownership, RESULTS_DIRNAME, "render", create_parent=True)
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
            render_root=render_root,
            notes=chosen_notes,
            selected_at=old_plan.get("selected_at"),
        )
        for slot in rebuilt["outputs"]:
            for field in ("output_dir", "render_manifest_path", "final_video_path"):
                slot[field] = self.relative(slot[field])

        old_hash = approval.content_hash_for("processing_plan", old_plan)
        new_hash = approval.content_hash_for("processing_plan", rebuilt)
        rebuilt["revision"] = int(old_plan.get("revision") or 1) + 1
        rebuilt["updated_by"] = actor[:120]
        rebuilt["updated_at"] = _now_iso()
        relative = self.write_artifact("processing_plan", rebuilt, actor=actor)

        old_approval = old_plan.get("human_approval") or {}
        if old_approval.get("approved"):
            self.audit.record(
                "approval.invalidated", actor=actor, artifact=relative,
                detail={"kind": "processing_plan", "reason": "plan edited",
                        "previous_approved_content_sha256":
                            old_approval.get("approved_content_sha256")},
            )
        self.audit.record(
            "plan.updated", actor=actor, artifact=relative,
            detail={"previous_mode": old_decision.get("mode"),
                    "mode": chosen_mode,
                    "decision_changed": old_hash != new_hash,
                    "revision": rebuilt["revision"]},
        )
        return rebuilt

    def describe_plan_review(self) -> dict:
        plan = self.load_artifact("processing_plan")
        if plan["problem"]:
            raise ArtifactProblem(plan["problem"])
        if not plan["data"]:
            return {"present": False}
        info = approval.describe_for_review("processing_plan", plan["data"])
        assessment = self.load_artifact("source_assessment")
        errors = source_strategy.validate_processing_plan(
            plan["data"], assessment=assessment["data"] if assessment["data"] else None,
        )
        return {"present": True, "review": info, "staleness": errors, "plan": plan["data"]}

    # ------------------------------------------------------------- approvals
    APPROVAL_ARTIFACTS = {
        "processing_plan": "processing_plan",
        "script": "selected_script",
        "voiceover": "selected_voiceover",
        "transcript": "moment_transcript",
        "render": "render_manifest",
    }

    # ------------------------------------------------------- render review
    def render_review_relative(self, slot: int = 1) -> str:
        """Where THIS slot's checklist lives.

        Each Short is watched separately, so each Short owns its own checklist.
        Sharing one file would let ticks earned on slot 1 unlock the approval
        gate of a slot 2 nobody has watched -- the exact failure the checklist
        exists to prevent.
        """
        index = int(slot or 1)
        if index == 1:
            return render_review_module.RENDER_REVIEW_ARTIFACT
        name = render_review_module.RENDER_REVIEW_ARTIFACT.rsplit("/", 1)[-1]
        return f"{RESULTS_DIRNAME}/render/output_{index:02d}/{name}"

    def render_video_digest(self, slot: int = 1):
        """SHA-256 of the render currently on disk, or None.

        The SAME digest the approval gate binds to, so a checklist entry and an
        approval can never disagree about which video was reviewed.
        """
        loaded = self.load_artifact("render_manifest", slot=slot)
        if not loaded["data"] or loaded["problem"]:
            return None
        try:
            return approval.content_hash_for("render", loaded["data"])
        except ValueError:
            return None

    def render_review_state(self, slot: int = 1) -> dict:
        review = self.load_artifact_path(self.render_review_relative(slot))
        state = render_review_module.evaluate(
            review, self.render_video_digest(slot), self.ownership)
        state["technical"] = render_review_module.technical_summary(
            self.load_artifact("render_manifest", slot=slot)["data"])
        state["slot"] = int(slot or 1)
        return state

    def _save_render_review(self, review: dict, slot: int = 1) -> dict:
        path = self.storage.resolve(self.ownership, self.render_review_relative(slot),
                                     create_parent=True)
        atomic_write_json(path, review)
        return review

    def _load_or_new_review(self, slot: int = 1) -> dict:
        existing = self.load_artifact_path(self.render_review_relative(slot))
        if isinstance(existing, dict):
            return existing
        return render_review_module.new_review(self.ownership, self.render_video_digest(slot))

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

    def request_render_revision(self, *, requested_by: str, notes: str,
                                 target_tab: str = "moments", slot: int = 1) -> dict:
        slot = self.require_slot(slot)
        digest = self.render_video_digest(slot)
        if not digest:
            raise PipelineServiceError("Revizyon istenecek bir render videosu yok.")
        review = render_review_module.request_revision(
            self._load_or_new_review(slot), video_sha256=digest, requested_by=requested_by,
            notes=notes, target_tab=target_tab)
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
        loaded = self.load_artifact("selected_voiceover")
        if not loaded["data"] or loaded["problem"]:
            return None
        try:
            return approval.content_hash_for("voiceover", loaded["data"])
        except ValueError:
            return None

    def voice_review_state(self) -> dict:
        review = self.load_artifact_path(voice_review_module.VOICE_REVIEW_ARTIFACT)
        decision = ((self.load_artifact("processing_plan")["data"] or {})
                    .get("decision") or {})
        state = voice_review_module.evaluate(
            review, self.voice_audio_digest(), self.ownership,
            content_language=decision.get("content_language"),
            narration_preset=(decision.get("narration") or {}).get("preset"))
        voiceover = self.load_artifact("selected_voiceover")["data"]
        script = self.load_artifact("selected_script")["data"]
        state["technical"] = voice_review_module.technical_summary(voiceover, script)
        # The exact words being spoken, so the listener can follow along instead
        # of judging "does this sound right" from memory.
        state["script_text"] = (script or {}).get("script_text")
        state["script_status"] = (script or {}).get("status")
        state["audio_relative_path"] = (voiceover or {}).get("audio_path_relative")
        # A voiceover produced BEFORE a language change is not merely stale, it
        # is in the wrong language -- said plainly rather than left to the ear.
        spoken = (voiceover or {}).get("language_code")
        planned = decision.get("content_language")
        state["language_mismatch"] = bool(spoken and planned and spoken != planned)
        state["spoken_language"] = spoken
        state["planned_language"] = planned
        state["planned_voice"] = (decision.get("narration") or {}).get("voice")
        state["spoken_voice"] = (voiceover or {}).get("voice_id")
        # A preset switch makes existing audio the wrong DELIVERY, not merely
        # stale -- named here so the panel can say which.
        state["preset_mismatch"] = bool(
            state["planned_voice"] and state["spoken_voice"]
            and state["planned_voice"] != state["spoken_voice"])
        return state

    def _save_voice_review(self, review: dict) -> dict:
        path = self.storage.resolve(self.ownership,
                                     voice_review_module.VOICE_REVIEW_ARTIFACT,
                                     create_parent=True)
        atomic_write_json(path, review)
        return review

    def _load_or_new_voice_review(self) -> dict:
        existing = self.load_artifact_path(voice_review_module.VOICE_REVIEW_ARTIFACT)
        if isinstance(existing, dict):
            return existing
        return voice_review_module.new_review(self.ownership, self.voice_audio_digest())

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
            requested_by=requested_by, notes=notes, target_tab=target_tab)
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
        if kind not in self.APPROVAL_ARTIFACTS:
            raise PipelineServiceError(f"bilinmeyen onay turu: {kind!r}")
        key = self.APPROVAL_ARTIFACTS[kind]
        slot = self.require_slot(slot) if key in PER_SLOT_ARTIFACTS else 1
        loaded = self.load_artifact(key, required=True, slot=slot)
        if loaded["problem"]:
            raise ArtifactProblem(loaded["problem"])
        if kind in ("render", "voiceover"):
            # The hash proves the reviewer saw (or heard) the bytes; the
            # checklist proves they checked the things that actually go wrong.
            # This ADDS to the canonical gate, it does not replace it.
            state = (self.render_review_state(slot) if kind == "render"
                     else self.voice_review_state())
            if not state["complete"] or state["open_revision"]:
                raise PipelineServiceError(
                    "ONAY REDDEDILDI: "
                    + (state["blocker"] or "insan inceleme listesi tamamlanmadi"))
        try:
            approved = approval.approve_artifact(
                loaded["data"], kind, reviewed_sha256, approved_by, notes)
        except ValueError as exc:
            self.audit.record("approval.rejected", actor=approved_by,
                              artifact=self.artifact_relative(key, slot),
                              detail={"kind": kind, "reason": str(exc), "slot": slot})
            raise PipelineServiceError(str(exc)) from None
        relative = self.write_artifact(key, approved, actor=approved_by, slot=slot)
        self.audit.record("approval.granted", actor=approved_by, artifact=relative,
                          detail={"kind": kind, "slot": slot,
                                   "approved_content_sha256":
                                       approved["human_approval"]["approved_content_sha256"]})
        return approved

    def approval_state(self, kind: str, slot: int = 1) -> dict:
        """Live approval validity, re-derived from current content every time."""
        key = self.APPROVAL_ARTIFACTS[kind]
        slot = int(slot or 1) if key in PER_SLOT_ARTIFACTS else 1
        loaded = self.load_artifact(key, slot=slot)
        if loaded["problem"]:
            return {"kind": kind, "slot": slot, "present": True, "valid": False,
                    "approved": False, "reason": loaded["problem"]}
        if not loaded["data"]:
            return {"kind": kind, "slot": slot, "present": False, "valid": False,
                    "approved": False, "reason": "artefakt henuz uretilmedi"}
        state = approval.check_approval(loaded["data"], kind)
        try:
            review = approval.describe_for_review(kind, loaded["data"])
        except Exception as exc:  # noqa: BLE001 - review must never crash the page
            review = {"reviewable": False, "blocker": str(exc), "content_sha256": None}
        return {
            "kind": kind, "slot": slot, "present": True,
            "status": loaded["data"].get("status"),
            "approved": state["approved"], "valid": state["valid"], "reason": state["reason"],
            "content_sha256": review.get("content_sha256"),
            "reviewable": review.get("reviewable"),
            "blocker": review.get("blocker"),
            "blocking_conditions": review.get("blocking_conditions") or [],
        }

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
        plan_loaded = self.load_artifact("processing_plan")
        plan = plan_loaded["data"] or {}
        mode = (plan.get("decision") or {}).get("mode")
        chain = ["processing_plan"]
        if mode == source_strategy.MODE_NARRATED_THEORY:
            chain += ["script", "voiceover"]
        else:
            # The caption text of a clip Short is as much a human decision as a
            # narrated script is, so it belongs in the chain: correcting a word
            # after approving the render must invalidate that render.
            chain.append("transcript")
        chain.append("render")

        upstream_problems: list = []

        # The moment selection is upstream of everything after the plan even
        # though it is not an approvable "kind": a stale selection means the
        # script/render were built from a moment that no longer exists.
        selection = self.load_artifact("moment_selection", slot=slot)
        hot = self.load_artifact("hot_moments")
        selection_problem = None
        if selection["problem"]:
            selection_problem = f"moment_selection: {selection['problem']}"
        elif selection["data"] and hot["data"]:
            errors = video_analysis.validate_moment_selection(
                selection["data"], hot["data"], require_preview=False)
            if errors:
                selection_problem = f"moment_selection bayat: {errors}"

        states = {}
        for kind in chain:
            state = self.approval_state(kind, slot)
            problems = list(upstream_problems)
            if kind != "processing_plan" and selection_problem:
                problems.append(selection_problem)
            state["upstream_invalid"] = problems
            state["effective_valid"] = bool(state.get("valid")) and not problems
            if state.get("approved") and problems and state.get("valid"):
                state["reason"] = (
                    "Kendi icerigi degismedi ama YUKARI-AKIS degisti, bu yuzden bu onay "
                    "artik gecerli sayilmiyor: " + "; ".join(problems)
                )
            states[kind] = state
            if state.get("present") and not state.get("effective_valid") \
                    and state.get("approved"):
                upstream_problems.append(f"{kind} onayi gecersiz")
            elif kind == "processing_plan" and not state.get("valid"):
                upstream_problems.append("processing_plan onayi gecerli degil")

        for kind in self.APPROVAL_ARTIFACTS:
            states.setdefault(kind, {**self.approval_state(kind, slot),
                                      "upstream_invalid": [], "effective_valid": False,
                                      "in_chain": False})
            states[kind].setdefault("in_chain", kind in chain)
        return states

    def invalidated_approvals(self) -> list:
        """Approvals that were granted but no longer bind -- their own content or
        anything upstream of them changed.

        Every slot is checked: a batch can have one Short whose approval still
        binds and another whose source moment was re-analysed underneath it.

        Reported, never deleted: the audit trail of the original approval stays
        intact even after it stops being valid.
        """
        stale = []
        seen = set()
        for index in self.slot_indexes():
            for kind, state in self.approval_chain(index).items():
                if not (state.get("approved") and not state.get("effective_valid")):
                    continue
                # Project-wide approvals (plan, script, voiceover) are identical
                # for every slot; reporting them once per Short would turn one
                # stale plan into N identical warnings.
                marker = (kind, state.get("slot") if kind == "render" else None)
                if marker in seen:
                    continue
                seen.add(marker)
                stale.append(state)
        return stale

    # ------------------------------------------------------------- phase gate
    def plan_gate(self, phase: str) -> tuple:
        plan = self.load_artifact("processing_plan")
        if plan["problem"]:
            return False, plan["problem"]
        return source_strategy.plan_allows_phase(plan["data"], phase)

    def require_phase(self, phase: str) -> None:
        allowed, reason = self.plan_gate(phase)
        if not allowed:
            raise PipelineServiceError(reason)

    # ------------------------------------------------------------ output slots
    def slot_count(self) -> int:
        """How many Shorts the APPROVED plan asks for (1 when there is no plan).

        Read from `outputs` rather than `decision.output_count` so the slots the
        engine walks are exactly the slots the plan describes, directories
        included.
        """
        plan = self.load_artifact("processing_plan")["data"] or {}
        slots = plan.get("outputs") or []
        return max(1, len(slots))

    def slot_indexes(self) -> list:
        return list(range(1, self.slot_count() + 1))

    def require_slot(self, slot: int) -> int:
        """Reject a slot the approved plan does not define.

        Without this an out-of-range slot would quietly create
        `results/render/output_47/` and render a Short nobody planned -- outside
        every count the plan, the batch board and the approval chain reason about.
        """
        try:
            index = int(slot)
        except (TypeError, ValueError):
            raise PipelineServiceError(f"gecersiz cikti yuvasi: {slot!r}") from None
        total = self.slot_count()
        if not 1 <= index <= total:
            raise PipelineServiceError(
                f"cikti yuvasi {index} bu planda yok (plan {total} cikti tanimliyor)")
        return index

    def slot_moment_rank(self, slot: int):
        """The moment rank this slot's plan entry pre-assigned, or None."""
        plan = self.load_artifact("processing_plan")["data"] or {}
        for entry in (plan.get("outputs") or []):
            if int(entry.get("index") or 0) == int(slot):
                return entry.get("moment_rank")
        return None

    # ------------------------------------------------------------ pipeline
    def run_video_analysis(self, *, clip_start_sec=0, clip_duration_sec=240,
                           focus_region=None, clip_duration_min=None,
                           custom_start_min=None, analysis_scope="window",
                           whisper_model_size="small", scene_detector="content",
                           top_n=5, job_id=None, actor="console") -> dict:
        self.require_phase("video_analysis")
        # The two scopes genuinely have different stages. Declaring the real ones
        # keeps the panel honest: a window run must not display a "region" stage
        # it will never enter, and a full scan must not be described as a single
        # clip extraction.
        tracker = self.tracker(
            "video_analysis", job_id=job_id,
            stages=FULL_SOURCE_ANALYSIS_STAGES if analysis_scope == "full_source" else None)
        tracker.start("Video analizi basladi")
        try:
            assessment = self.load_artifact("source_assessment", required=True)
            source_duration = (((assessment.get("data") or {}).get("source") or {})
                               .get("duration_sec"))
            if analysis_scope == "full_source":
                return self._run_full_source_analysis(
                    tracker, source_duration_sec=source_duration,
                    whisper_model_size=whisper_model_size, scene_detector=scene_detector,
                    top_n=top_n, job_id=job_id, actor=actor)
            if analysis_scope != "window":
                raise PipelineServiceError(
                    f"bilinmeyen analiz kapsami: {analysis_scope!r} "
                    "(beklenen: window, full_source)")
            clip_start_sec, clip_duration_sec = video_analysis.resolve_analysis_range(
                source_duration_sec=source_duration,
                focus_region=focus_region,
                clip_duration_min=clip_duration_min,
                custom_start_min=custom_start_min,
                clip_start_sec=clip_start_sec,
                clip_duration_sec=clip_duration_sec,
            )
            return self._run_video_analysis(
                tracker, clip_start_sec=clip_start_sec, clip_duration_sec=clip_duration_sec,
                focus_region=focus_region, clip_duration_min=clip_duration_min,
                custom_start_min=custom_start_min,
                whisper_model_size=whisper_model_size, scene_detector=scene_detector,
                top_n=top_n, job_id=job_id, actor=actor)
        except Exception as exc:  # noqa: BLE001
            tracker.fail(f"{type(exc).__name__}: {exc}")
            raise

    def _run_full_source_analysis(self, tracker, *, source_duration_sec,
                                  whisper_model_size, scene_detector, top_n,
                                  job_id, actor) -> dict:
        """Scan the WHOLE source instead of one window.

        A batch of N Shorts cut from one 240-second window would be N variations
        of the same scene. Producing N genuinely different Shorts from a feature
        film requires candidates from the whole film, which is what this scope is
        for.
        """
        if not source_duration_sec:
            raise PipelineServiceError(
                "Tam kaynak taramasi icin video suresi bilinmiyor; once kaynak "
                "degerlendirmesini calistir.")
        source = self.source_path()
        ffmpeg_bin, _ = self.binaries()
        runtime_dir = self.storage.resolve(self.ownership, RUNTIME_DIRNAME, create_parent=True)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        preview_dir = self.storage.resolve(self.ownership, RESULTS_DIRNAME, "moment_previews",
                                            create_parent=True)
        data = video_analysis.run_full_source_analysis(
            ffmpeg_bin, source, runtime_dir,
            source_duration_sec=float(source_duration_sec),
            whisper_model_size=whisper_model_size, scene_detector=scene_detector,
            top_n=top_n, preview_dir=preview_dir, progress=tracker,
        )
        self.write_artifact("hot_moments", data, job_id=job_id, actor=actor)
        tracker.finish(
            message=f"{len(data.get('hot_moments') or [])} aday moment "
                    f"({(data.get('full_source_scan') or {}).get('regions_scanned', 0)} bolge)")
        return data

    def _run_video_analysis(self, tracker, *, clip_start_sec, clip_duration_sec,
                            focus_region, clip_duration_min, custom_start_min,
                            whisper_model_size, scene_detector, top_n, job_id, actor) -> dict:
        source = self.source_path()
        ffmpeg_bin, _ = self.binaries()
        runtime_dir = self.storage.resolve(self.ownership, RUNTIME_DIRNAME, create_parent=True)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        preview_dir = self.storage.resolve(self.ownership, RESULTS_DIRNAME, "moment_previews",
                                            create_parent=True)
        data = video_analysis.run_video_analysis(
            ffmpeg_bin, source, runtime_dir, clip_start_sec=clip_start_sec,
            clip_duration_sec=clip_duration_sec, whisper_model_size=whisper_model_size,
            scene_detector=scene_detector, top_n=top_n, preview_dir=preview_dir,
            progress=tracker, adaptive_candidate_windows=True,
        )
        data["analysis_scope"] = "window"
        if focus_region:
            data.setdefault("candidate_request", {}).update({
                "focus_region": focus_region,
                "clip_duration_min": float(clip_duration_min),
                "custom_start_min": (float(custom_start_min)
                                     if focus_region == "custom" else None),
                "resolved_start_sec": clip_start_sec,
                "resolved_duration_sec": clip_duration_sec,
            })
        self.write_artifact("hot_moments", data, job_id=job_id, actor=actor)
        tracker.finish(message=f"{len(data.get('hot_moments') or [])} aday moment")
        return data

    def record_moment_selection(self, *, selected_rank: int, reviewed_by: str,
                                rejected_ranks=None, notes: str = "",
                                require_preview: bool = True, slot: int = 1) -> dict:
        self.require_phase("moment_selection")
        self.require_slot(slot)
        hot = self.load_artifact("hot_moments", required=True)
        if hot["problem"]:
            raise ArtifactProblem(hot["problem"])
        taken = self.moment_rank_owner(selected_rank, slot)
        if taken is not None:
            raise PipelineServiceError(
                f"rank {selected_rank} zaten {taken}. cikti yuvasi icin secilmis; "
                "her Short farkli bir moment kullanmali")
        selection = video_analysis.build_moment_selection(
            hot["data"], selected_moment_rank=selected_rank, reviewed_by=reviewed_by,
            rejected_moment_ranks=rejected_ranks, review_notes=notes,
            require_preview=require_preview,
        )
        self.write_artifact("moment_selection", selection, actor=reviewed_by, slot=slot)
        return selection

    def moment_rank_owner(self, rank: int, slot: int):
        """Index of another slot that already selected `rank`, or None.

        Producing the same moment twice in one batch is almost always a mistake
        rather than an intent, and it is invisible until both Shorts are rendered
        and watched. It is rejected at selection time, where the fix is one click.
        """
        for other in self.slot_indexes():
            if int(other) == int(slot):
                continue
            existing = self.load_artifact("moment_selection", slot=other)["data"]
            if existing and existing.get("selected_moment_rank") == int(rank):
                return int(other)
        return None

    def run_script_generation(self, *, job_id=None, actor="console",
                              enable_google_model_fallback: bool = True,
                              max_google_model_attempts: int = 4,
                              max_google_transient_retries: int = 2) -> dict:
        """Defaults keep the normal Route A behaviour. The three knobs exist so a
        deliberate single-call validation can pin the request to exactly one
        model, one attempt, zero retries."""
        self.require_phase("script_generation")
        tracker = self.tracker("script_generation", job_id=job_id)
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
        hot = self.load_artifact("hot_moments", required=True)
        selection = self.load_artifact("moment_selection", required=True)
        for loaded in (hot, selection):
            if loaded["problem"]:
                raise ArtifactProblem(loaded["problem"])
        errors = video_analysis.validate_moment_selection(
            selection["data"], hot["data"], require_preview=True)
        if errors:
            raise PipelineServiceError(f"moment secimi gecersiz/bayat: {errors}")

        plan = self.load_artifact("processing_plan")["data"] or {}
        decision = plan.get("decision") or {}
        google_client = self._build_google_client()
        tracker.stage("request", "Gemini yaniti bekleniyor")
        result = script_generation.run_script_generation(
            hot["data"], self.relative(self.path("hot_moments")),
            selected_moment_rank=selection["data"]["selected_moment_rank"],
            google_client=google_client,
            openai_client=self._call(self._openai_client_factory),
            anthropic_client=self._call(self._anthropic_client_factory),
            target_duration_sec=decision.get("target_duration_sec", 60),
            moment_approved=True, moment_approved_by=selection["data"]["reviewed_by"],
            script_provider="google", allow_provider_fallback=False,
            content_language=decision.get("content_language"),
            enable_google_model_fallback=enable_google_model_fallback,
            max_google_model_attempts=max_google_model_attempts,
            max_google_transient_retries=max_google_transient_retries,
        )
        tracker.stage("parse", "Yanit ayristirildi")
        result["theory_angle"] = decision.get("theory_angle")
        result["content_language"] = decision.get("content_language")
        tracker.stage("write", "Script artefakti yaziliyor")
        self.write_artifact("selected_script", result, job_id=job_id, actor=actor)
        self.audit.record("provider.call", actor=actor, job_id=job_id,
                          detail={"phase": "script_generation",
                                   "provider": result.get("provider"),
                                   "model": result.get("model")})
        if result.get("status") == "failed":
            tracker.fail(result.get("status_reason") or "script uretilemedi")
        else:
            tracker.finish(message=f"{result.get('model') or 'model'} · "
                                   f"{len(result.get('script_text') or '')} karakter")
        return result

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

    def transcript_view(self, slot: int = 1) -> dict:
        """A curated slice for the caption screen.

        The raw artifact carries every word with its timings; dumping that into
        the page would bury the two things a reviewer needs -- the text, and
        which words the recogniser was unsure about.
        """
        slot = int(slot or 1)
        loaded = self.load_artifact("moment_transcript", slot=slot)
        data = loaded["data"] or {}
        allowed, gate_reason = self.plan_gate("transcript")
        state = self.approval_state("transcript", slot) if data else {}
        selection = self.load_artifact("moment_selection", slot=slot)["data"] or {}
        return {
            "slot": slot,
            "available": bool(data),
            "problem": loaded["problem"],
            "phase_allowed": bool(allowed),
            "phase_reason": gate_reason,
            "moment_rank": data.get("moment_rank") or selection.get("selected_moment_rank"),
            "status": data.get("status"),
            "status_reason": data.get("status_reason"),
            "language": data.get("language"),
            "whisper_model_size": data.get("whisper_model_size"),
            "source": data.get("source"),
            "text": data.get("text"),
            "word_count": data.get("word_count"),
            "cue_count": data.get("cue_count"),
            "has_speech": data.get("has_speech"),
            "average_word_probability": data.get("average_word_probability"),
            "low_confidence_words": data.get("low_confidence_words") or [],
            "low_confidence_count": data.get("low_confidence_count"),
            "corrections": data.get("corrections") or [],
            # Words carry the highlight timings, so the page can show the caption
            # the way it will actually appear, with unsure words marked.
            "words": [
                {"text": w.get("text"), "start_sec": w.get("start"),
                 "probability": w.get("probability"),
                 "low_confidence": float(w.get("probability") or 0.0)
                                   < render_utils.LOW_CONFIDENCE_WORD_THRESHOLD,
                 "corrected": bool(w.get("corrected"))}
                for w in (data.get("words") or [])[:2000]
            ],
            "approval": state,
        }

    def run_transcript(self, *, subtitle_whisper_model_size="small",
                       subtitle_language="auto", caption_theme="auto",
                       job_id=None, actor="console", slot=1) -> dict:
        """Transcribe the selected moment's ORIGINAL audio for human review.

        This is `direct_clip_series`'s caption step. It calls no provider: the
        text is what the source already says, recognised locally. Existing
        corrections are carried over, because re-running the recognition should
        not silently discard the names a human already fixed.
        """
        self.require_phase("transcript")
        slot = self.require_slot(slot)
        tracker = self.tracker("transcript", job_id=job_id)
        tracker.start("Transkript uretimi basladi")
        try:
            hot = self.load_artifact("hot_moments", required=True)
            selection = self.load_artifact("moment_selection", required=True, slot=slot)
            for loaded in (hot, selection):
                if loaded["problem"]:
                    raise ArtifactProblem(loaded["problem"])
            previous = self.load_artifact("moment_transcript", slot=slot)["data"] or {}
            runtime_dir = self.storage.resolve(self.ownership, RUNTIME_DIRNAME,
                                               create_parent=True)
            artifact = render_utils.run_moment_transcription(
                hot["data"], selection["data"], runtime_dir,
                subtitle_whisper_model_size=subtitle_whisper_model_size,
                subtitle_language=subtitle_language, caption_theme=caption_theme,
                source_video_path_override=str(self.source_path()),
                progress=tracker)
            carried = [
                {"from": item.get("from"), "to": item.get("to")}
                for item in (previous.get("corrections") or [])
                if item.get("from") and item.get("to")
            ]
            if carried:
                artifact = self._apply_transcript_corrections(artifact, carried)
                tracker.note(f"{len(carried)} onceki duzeltme yeniden uygulandi")
            self.write_artifact("moment_transcript", artifact, job_id=job_id,
                                actor=actor, slot=slot)
            tracker.finish(message=(f"{artifact['word_count']} kelime, inceleme bekliyor"
                                    if artifact["has_speech"] else "konusma yok"))
            return artifact
        except Exception as exc:  # noqa: BLE001
            tracker.fail(f"{type(exc).__name__}: {exc}")
            raise

    def _apply_transcript_corrections(self, artifact: dict, corrections) -> dict:
        """Re-derive the corrected text from the ORIGINAL recognition.

        Corrections are always applied to the untouched recognition rather than
        on top of a previous pass, so removing a correction really removes it
        instead of leaving its effect baked in.
        """
        base_words = [
            {k: v for k, v in word.items() if k != "corrected"}
            for word in (artifact.get("words") or [])
        ]
        rebuilt = render_utils.build_moment_transcript_artifact(
            base_words, artifact.get("cues") or [],
            source=artifact.get("source"), language=artifact.get("language"),
            moment_rank=artifact.get("moment_rank"),
            moment_selection_sha256=artifact.get("moment_selection_sha256"),
            whisper_model_size=artifact.get("whisper_model_size"),
            initial_prompt_used=artifact.get("initial_prompt_used"),
            corrections=corrections)
        return rebuilt

    def set_transcript_corrections(self, *, corrections, edited_by: str, slot: int = 1) -> dict:
        """Replace this slot's correction list and re-derive the caption text.

        Any existing approval is dropped by construction: the approval hash
        covers the words, so changed words cannot stay approved.
        """
        slot = self.require_slot(slot)
        loaded = self.load_artifact("moment_transcript", required=True, slot=slot)
        if loaded["problem"]:
            raise ArtifactProblem(loaded["problem"])
        cleaned = []
        for item in (corrections or []):
            source = str((item or {}).get("from") or "").strip()
            target = str((item or {}).get("to") or "").strip()
            if not source or not target:
                continue
            if len(source) > 80 or len(target) > 80:
                raise PipelineServiceError("duzeltme kelimeleri 80 karakteri gecemez")
            cleaned.append({"from": source, "to": target})
        if len(cleaned) > 100:
            raise PipelineServiceError("en fazla 100 duzeltme tanimlanabilir")
        artifact = self._apply_transcript_corrections(loaded["data"], cleaned)
        relative = self.write_artifact("moment_transcript", artifact, actor=edited_by, slot=slot)
        self.audit.record("artifact.written", actor=edited_by, artifact=relative,
                          detail={"corrections": len(cleaned), "slot": slot})
        return artifact

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
        drift = source_strategy.check_config_drift(
            plan["data"], render_config={"render_mode": render_mode, "preview_mode": preview_mode,
                                          "subtitle_whisper_model_size": subtitle_whisper_model_size,
                                          "subtitle_language": subtitle_language,
                                          "caption_style": caption_style,
                                          "caption_theme": caption_theme})
        if drift:
            raise PipelineServiceError(
                "onaylanan plan ile istenen render ayari uyusmuyor: " + "; ".join(drift))

        hot = self.load_artifact("hot_moments", required=True)
        if hot["problem"]:
            raise ArtifactProblem(hot["problem"])
        render_dir = self.slot_render_dir(slot)
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
                subtitle_whisper_model_size=subtitle_whisper_model_size,
                subtitle_language=subtitle_language,
                caption_style=caption_style, caption_theme=caption_theme,
                source_video_path_override=str(self.source_path()),
                progress=tracker,
            )
        else:
            script = self.load_artifact("selected_script", required=True)
            voice = self.load_artifact("selected_voiceover", required=True)
            for loaded in (script, voice):
                if loaded["problem"]:
                    raise ArtifactProblem(loaded["problem"])
            manifest = render_utils.run_render_pipeline(
                hot["data"], script["data"], voice["data"],
                self.relative(self.path("hot_moments")),
                self.relative(self.path("selected_script")),
                self.relative(self.path("selected_voiceover")),
                render_dir=render_dir, render_mode=render_mode, preview_mode=preview_mode,
                source_video_path_override=str(self.source_path()),
                progress=tracker,
            )
        manifest["output_slot"] = int(slot)
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
