"""Console application core: configuration, job execution and view models.

WHY THIS EXISTS
---------------
`api.py` speaks HTTP, `pipeline_service.py` speaks pipeline. This module is the
bit in between: it owns configuration, builds an ownership-scoped service per
request, maps a job type to exactly one canonical pipeline call, and builds the
VIEW MODELS the browser renders.

View models are built here, on the server, on purpose: the frontend must not
re-derive pipeline state (that is how notebook logic leaks into a UI). The
browser receives already-decided booleans and already-formatted reasons.
"""
from __future__ import annotations

import json
import os
import re as _re
import shutil
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from local_pipeline import google_ai, narration_presets, source_strategy, video_analysis

from . import APP_NAME, __version__
from .audit import AuditLog
from .automation import AutomationEngine
from .ids import Ownership, new_id, validate_id
from .jobs import (
    ACTIVE_STATUSES,
    JOB_TYPES,
    STATUS_COMPLETED,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_REVIEW_REQUIRED,
    InlineJobRunner,
    JobConflict,
    JobError,
    JobNotFound,
    JobStore,
    ThreadJobRunner,
)
from .pipeline_service import ArtifactProblem, PipelineService, PipelineServiceError
from .secrets_ref import CONSOLE_SECRET_ALIASES, SecretResolver
from .progress import ACTIVITIES as PROGRESS_ACTIVITIES
from .progress import (
    OperationCancelled,
    ProgressTracker,
    load_progress,
    mark_interrupted,
)
from .status import build_workflow_map, run_preflight
from .storage import LocalFilesystemStorage, StorageViolation, atomic_write_json
from .env_file import write_env_value
from .redaction import register_secret, scrub

#: Pre-alpha is single-user. These are REAL ownership values, not placeholders:
#: swapping them for session-derived ones is the only change authentication needs.
DEFAULT_USER_ID = "usr_local"
DEFAULT_WORKSPACE_ID = "wsp_local"

#: Capabilities that exist in the roadmap but NOT in this build. The UI renders
#: them as "planned" so pre-alpha never fakes production behaviour.
PLANNED_NOT_IMPLEMENTED = (
    {"key": "multi_user_auth", "label": "Kimlik dogrulama ve RBAC", "milestone": "M1.1"},
    {"key": "multi_tenant", "label": "Gercek cok kiracili calisma", "milestone": "M2.0"},
    {"key": "billing", "label": "Abonelik ve faturalandirma", "milestone": "M2.0"},
    {"key": "public_deploy", "label": "Public internet dagitimi", "milestone": "M2.0"},
    {"key": "youtube_publish", "label": "Otomatik YouTube yayini", "milestone": "M0.7"},
    {"key": "object_storage", "label": "Nesne depolamaya gecis", "milestone": "M1.0"},
)


#: How often a running job renews its lease. Must be comfortably shorter than
#: `jobs.DEFAULT_LEASE_SECONDS` so one missed beat does not expire the lease.
HEARTBEAT_INTERVAL_SEC = 20.0


def _tree_inventory(root: Path) -> dict[str, int]:
    """Return a deterministic relative-path -> byte-length archive inventory.

    Links are refused: an archive must contain project-owned bytes, never follow
    an operator-created junction or symlink into an unrelated directory.
    """
    root = Path(root)
    inventory: dict[str, int] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise StorageViolation("proje arsivlenirken sembolik baglanti kullanilamaz")
        if path.is_file():
            inventory[path.relative_to(root).as_posix()] = path.stat().st_size
    return inventory


class _Heartbeat:
    """Renews a job lease while its worker is genuinely alive.

    Stops on its own if the job is no longer ours (another worker took over, or
    the job finished), so a stale heartbeat can never keep a dead job alive.
    """

    def __init__(self, store, job_id, worker_id, interval=HEARTBEAT_INTERVAL_SEC):
        self.store, self.job_id, self.worker_id = store, job_id, worker_id
        self.interval = interval
        self.beats = 0
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                         name=f"heartbeat-{self.job_id}")
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self.store.heartbeat(self.job_id, self.worker_id)
                self.beats += 1
            except Exception:  # noqa: BLE001 - job finished or taken over
                return

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


@dataclass
class ConsoleConfig:
    storage_root: Path
    archive_root: Path | None = None
    source_library_root: Path | None = None
    #: Optional licensed music library. Tracks are listed with their
    #: licence status; none is ever reported as cleared for use.
    music_root: Path | None = None
    user_id: str = DEFAULT_USER_ID
    workspace_id: str = DEFAULT_WORKSPACE_ID
    host: str = "127.0.0.1"
    port: int = 8765
    inline_jobs: bool = False
    secret_getter: object = None
    secret_setter: object = None
    env_file_path: Path | None = None
    google_client_factory: object = None
    openai_client_factory: object = None
    anthropic_client_factory: object = None
    elevenlabs_client_factory: object = None
    probe_fn: object = None
    ffmpeg_bin: str | None = None
    ffprobe_bin: str | None = None
    # Local long-form sources commonly exceed 4 GiB. Uploads are streamed to
    # disk (not buffered in RAM), so the guard protects disk capacity rather
    # than process memory. The UI exposes this exact limit before sending.
    max_upload_bytes: int = 20 * 1024 * 1024 * 1024
    extra: dict = field(default_factory=dict)



#: Anything that looks like a host filesystem path. Artifacts legitimately hold
#: absolute paths internally (ffmpeg needs them); the BROWSER must never see one.
_HOST_PATH_RE = _re.compile(r"(?:[A-Za-z]:[\/]|\\|/(?:Users|home|mnt|var|tmp|opt)/)")
HOST_PATH_PLACEHOLDER = "<host-path-hidden>"


def scrub_host_paths(value, storage, ownership):
    """Rewrite absolute host paths out of an outgoing view model.

    A path inside the project namespace becomes namespace-relative (so the media
    route can serve it); anything else is replaced by a placeholder. Applied once
    at the API boundary, so no artifact writer has to remember this rule and a
    newly added field cannot quietly leak the operator's directory layout.
    """
    if isinstance(value, str):
        if not _HOST_PATH_RE.search(value):
            return value
        try:
            return storage.relative_to_namespace(ownership, value)
        except Exception:  # noqa: BLE001 - outside the namespace, or not a path
            return HOST_PATH_PLACEHOLDER
    if isinstance(value, dict):
        return {k: scrub_host_paths(v, storage, ownership) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_host_paths(v, storage, ownership) for v in value]
    if isinstance(value, tuple):
        return [scrub_host_paths(v, storage, ownership) for v in value]
    return value


class ConsoleApp:
    """Everything the HTTP layer needs, with no HTTP knowledge of its own."""

    def __init__(self, config: ConsoleConfig):
        self.config = config
        self.storage = LocalFilesystemStorage(config.storage_root)
        self.secrets = SecretResolver(config.secret_getter)
        self._runtime_log_lock = threading.Lock()
        runner_cls = InlineJobRunner if config.inline_jobs else ThreadJobRunner
        self.runner = runner_cls(self._execute_job)
        # A worker process must NOT run startup recovery: it would reclaim jobs
        # belonging to other live workers.
        self._recovered_on_start = (
            [] if config.extra.get("skip_startup_recovery") else self.recover_all_projects())

    # ------------------------------------------------------------ ownership
    def ownership(self, project_id: str | None = None) -> Ownership:
        return Ownership(self.config.user_id, self.config.workspace_id, project_id)

    def service(self, ownership: Ownership) -> PipelineService:
        return PipelineService(
            self.storage, ownership, self.secrets,
            google_client_factory=self.config.google_client_factory,
            openai_client_factory=self.config.openai_client_factory,
            anthropic_client_factory=self.config.anthropic_client_factory,
            elevenlabs_client_factory=self.config.elevenlabs_client_factory,
            probe_fn=self.config.probe_fn,
            ffmpeg_bin=self.config.ffmpeg_bin, ffprobe_bin=self.config.ffprobe_bin,
            music_root=self.config.music_root,
        )

    def save_local_secret(self, *, kind: str, value: str) -> dict:
        """Persist one supported credential without ever echoing its value."""
        if kind != "github":
            raise PipelineServiceError("bu arayuz yalniz GitHub token kaydini destekliyor")
        aliases = tuple(CONSOLE_SECRET_ALIASES.get(kind) or ())
        if not aliases:
            raise PipelineServiceError("bilinmeyen secret turu")
        path = self.config.env_file_path
        if path is None:
            raise PipelineServiceError(
                "yerel secret dosyasi yapilandirilmamis; Console'u .env.local destegiyle baslat")
        clean = str(value or "").strip()
        # Register before validation/write so even a later exception cannot put
        # the submitted token into a server error line.
        if clean:
            register_secret(clean)
        summary = write_env_value(path, aliases[0], clean)
        if callable(self.config.secret_setter):
            self.config.secret_setter(aliases[0], clean)
        else:
            os.environ[aliases[0]] = clean
        reference = self.secrets.reference(kind).to_public_dict()
        # A custom test getter may not observe the process environment.  The
        # write succeeded nonetheless; never return the value to prove it.
        reference.update({"state": "configured", "alias_used": aliases[0]})
        return {"saved": summary["saved"], "secret": reference, "restart_required": False}

    def job_store(self, ownership: Ownership) -> JobStore:
        return JobStore(self.storage, ownership)

    def automation(self, ownership: Ownership) -> AutomationEngine:
        """The PERSISTED automatic flow for one project.

        State lives in `results/automation.json`, so a refresh or a backend
        restart resumes exactly where the flow was.
        """
        return AutomationEngine(self, ownership)

    def automation_action(self, ownership: Ownership, action: str, *,
                          actor: str = "console") -> dict:
        engine = self.automation(ownership)
        if action == "start":
            engine.start(actor=actor)
            engine.tick(actor=f"{actor}:auto")
        elif action == "pause":
            engine.pause(actor=actor)
        elif action == "resume":
            engine.resume(actor=actor)
            engine.tick(actor=f"{actor}:auto")
        elif action == "cancel":
            engine.cancel(actor=actor)
        elif action == "tick":
            engine.tick(actor=f"{actor}:auto")
        else:
            raise JobError(f"bilinmeyen otomasyon eylemi: {action!r}")
        return engine.view()

    # ------------------------------------------------------------- projects
    def project_ids(self) -> list:
        base = self.storage.workspace_root(self.ownership()) / self.storage.PROJECTS_DIR
        if not base.is_dir():
            return []
        return sorted(p.name for p in base.iterdir() if p.is_dir())

    def create_project(self, *, title: str, created_by: str,
                       operator_name: str | None = None) -> dict:
        ownership = self.ownership(new_id("project"))
        service = self.service(ownership)
        record = service.create_project(
            title=title, created_by=created_by,
            operator_name=operator_name or created_by)
        return record

    def _assert_lifecycle_idle(self, ownership: Ownership) -> None:
        active = [job for job in self.job_store(ownership).list(limit=200)
                  if job.get("status") in ACTIVE_STATUSES]
        if active:
            kinds = ", ".join(sorted({str(job.get("job_type")) for job in active}))
            raise JobConflict(
                f"proje degistirilemez; aktif isler tamamlanmali veya durdurulmali: {kinds}")

    def rewind_project(self, ownership: Ownership, *, target: str,
                       actor: str = "console") -> dict:
        self._assert_lifecycle_idle(ownership)
        auto = self.automation(ownership)
        if auto.view().get("can_cancel"):
            auto.cancel(actor=actor)
        return self.service(ownership).rewind_project(target=target, actor=actor)

    def reset_project(self, ownership: Ownership, *, keep_source: bool,
                      confirmation: str, actor: str = "console") -> dict:
        self._assert_lifecycle_idle(ownership)
        loaded = self.service(ownership).load_artifact("project", required=True)
        if loaded["problem"]:
            raise ArtifactProblem(loaded["problem"])
        title = str((loaded["data"] or {}).get("title") or "").strip()
        if str(confirmation or "").strip() != title:
            raise PipelineServiceError(
                "projeyi sifirlamak icin proje adini birebir yazmalisin")
        auto = self.automation(ownership)
        if auto.view().get("can_cancel"):
            auto.cancel(actor=actor)
        return self.service(ownership).reset_project(
            keep_source=bool(keep_source), actor=actor)

    def delete_project(self, ownership: Ownership, *, confirmation: str,
                       actor: str = "console") -> dict:
        self._assert_lifecycle_idle(ownership)
        service = self.service(ownership)
        loaded = service.load_artifact("project", required=True)
        if loaded["problem"]:
            raise ArtifactProblem(loaded["problem"])
        title = str((loaded["data"] or {}).get("title") or "").strip()
        if str(confirmation or "").strip() != title:
            raise PipelineServiceError(
                "projeyi silmek icin proje adini birebir yazmalisin")
        service.audit.record("project.deleted", actor=actor,
                             detail={"title": title, "recoverable": True})
        recovery_token = self.storage.trash_namespace(ownership)
        return {"deleted": True, "project_id": ownership.project_id,
                "recoverable": True, "recovery_token": recovery_token}

    def archive_status(self) -> dict:
        """Public, path-safe summary of the operator-configured archive."""
        root = self.config.archive_root
        if root is None:
            return {"configured": False, "available": False,
                    "location_name": None, "projects": []}
        root = Path(root).expanduser()
        drive = root.drive.rstrip(":")
        leaf = root.name or "arsiv"
        location = f"{drive} surucusu / {leaf} / CreatorConsoleArchive" if drive else (
            f"{leaf} / CreatorConsoleArchive")
        archive_base = root / "CreatorConsoleArchive" / self.config.workspace_id
        projects = []
        available = root.is_dir()
        if available and archive_base.is_dir():
            try:
                candidates = list(archive_base.iterdir())
            except OSError:
                candidates = []
                available = False
            for candidate in candidates:
                try:
                    manifest_path = candidate / "archive_manifest.json"
                    if not candidate.is_dir() or not manifest_path.is_file():
                        continue
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                projects.append({
                    "archive_id": candidate.name,
                    "project_id": manifest.get("project_id"),
                    "title": manifest.get("title") or "(okunamadi)",
                    "source_name": manifest.get("source_name"),
                    "archived_at": manifest.get("archived_at"),
                    "archived_by": manifest.get("archived_by"),
                    "file_count": int(manifest.get("file_count") or 0),
                    "total_bytes": int(manifest.get("total_bytes") or 0),
                })
        projects.sort(key=lambda item: item.get("archived_at") or "", reverse=True)
        return {"configured": True, "available": available,
                "location_name": location, "projects": projects}

    def storage_policy_view(self) -> dict:
        """Path-safe active/archive placement shown in the local UI."""
        working = Path(self.storage.root)
        drive = working.drive.rstrip(":")
        working_name = f"{drive} surucusu / aktif calisma" if drive else "aktif calisma alani"
        archive = self.archive_status()
        return {
            "working_location_name": working_name,
            "archive_location_name": archive.get("location_name"),
            "archive_configured": archive.get("configured", False),
            "archive_available": archive.get("available", False),
            "policy": "Aktif projeler calisma diskinde; arsivlenen projeler harici diskte tutulur.",
        }

    def source_library_view(self) -> dict:
        """Path-safe list of videos the server may import without browser upload."""
        configured = self.config.source_library_root is not None
        if not configured:
            return {"configured": False, "available": False, "videos": []}
        root = Path(self.config.source_library_root).expanduser()
        if not root.is_dir():
            return {"configured": True, "available": False, "videos": []}
        videos = []
        try:
            candidates = list(root.iterdir())
        except OSError:
            return {"configured": True, "available": False, "videos": []}
        for candidate in candidates:
            try:
                if (not candidate.is_file()
                        or candidate.suffix.lower() not in
                        video_analysis.SUPPORTED_SOURCE_VIDEO_EXTENSIONS):
                    continue
                stat = candidate.stat()
            except OSError:
                continue
            videos.append({
                "name": candidate.name,
                "size_bytes": int(stat.st_size),
                "modified_at": datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc).isoformat(),
            })
        videos.sort(key=lambda item: item["name"].casefold())
        return {"configured": True, "available": True, "videos": videos}

    def import_source_from_library(self, ownership: Ownership, *, filename: str,
                                   actor: str = "console") -> dict:
        """Copy one explicitly selected library video into an idle project."""
        self._assert_lifecycle_idle(ownership)
        root_value = self.config.source_library_root
        if root_value is None:
            raise PipelineServiceError("kaynak video kutuphanesi yapilandirilmamis")
        requested = str(filename or "").strip()
        if not requested or Path(requested).name != requested:
            raise PipelineServiceError("gecersiz kaynak kutuphanesi dosya adi")
        root = Path(root_value).expanduser().resolve()
        source = (root / requested).resolve()
        if source.parent != root or not source.is_file():
            raise PipelineServiceError("kaynak kutuphanesinde video bulunamadi")
        if source.suffix.lower() not in video_analysis.SUPPORTED_SOURCE_VIDEO_EXTENSIONS:
            raise PipelineServiceError("kaynak kutuphanesi dosya turu desteklenmiyor")
        service = self.service(ownership)
        project = service.load_artifact("project", required=True)
        if ((project.get("data") or {}).get("source")):
            raise PipelineServiceError(
                "projede zaten kaynak video var; degistirmek icin yasam dongusu aracini kullan")
        size = source.stat().st_size
        if size <= 0:
            raise PipelineServiceError("kaynak kutuphanesi videosu bos")
        with source.open("rb") as stream:
            return service.register_source_video_stream(
                filename=source.name, stream=stream, content_length=size,
                registered_by=actor, track=True)

    def archive_project(self, ownership: Ownership, *, confirmation: str,
                        actor: str = "console") -> dict:
        """Move one idle project into the configured external archive.

        Same-volume archives are an atomic directory rename (the normal setup
        on this workstation). Cross-volume archives copy to a staging folder,
        verify every relative filename and byte length, publish the staged copy,
        and only then remove the working namespace.
        """
        self._assert_lifecycle_idle(ownership)
        archive_root = self.config.archive_root
        if archive_root is None:
            raise PipelineServiceError("proje arsiv konumu yapilandirilmamis")
        service = self.service(ownership)
        loaded = service.load_artifact("project", required=True)
        if loaded["problem"]:
            raise ArtifactProblem(loaded["problem"])
        project = loaded["data"] or {}
        title = str(project.get("title") or "").strip()
        if str(confirmation or "").strip() != title:
            raise PipelineServiceError(
                "projeyi arsivlemek icin proje adini birebir yazmalisin")

        source = self.storage.namespace_root(ownership).resolve()
        root = Path(archive_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        if source == root or source in root.parents:
            raise StorageViolation("arsiv konumu aktif proje klasorunun icinde olamaz")
        archive_base = root / "CreatorConsoleArchive" / ownership.workspace_id
        archive_base.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        archive_id = f"{ownership.project_id}__{stamp}"
        destination = archive_base / archive_id
        if destination.exists():
            raise FileExistsError("arsiv hedefi zaten var")

        automation = self.automation(ownership)
        if automation.view().get("can_cancel"):
            automation.cancel(actor=actor)
        service.audit.record("project.archived", actor=actor,
                             detail={"title": title, "archive_id": archive_id})
        inventory = _tree_inventory(source)
        source_record = project.get("source") or {}
        manifest = {
            "schema_version": 1,
            "archive_id": archive_id,
            "project_id": ownership.project_id,
            "workspace_id": ownership.workspace_id,
            "title": title,
            "source_name": source_record.get("display_name"),
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "archived_by": actor,
            "file_count": len(inventory),
            "total_bytes": sum(inventory.values()),
        }
        atomic_write_json(source / "archive_manifest.json", manifest)
        inventory = _tree_inventory(source)

        if source.stat().st_dev == archive_base.stat().st_dev:
            os.replace(source, destination)
        else:
            staging = archive_base / f".{archive_id}.partial"
            if staging.exists():
                raise FileExistsError("tamamlanmamis arsiv hedefi zaten var")
            try:
                shutil.copytree(source, staging)
                if _tree_inventory(staging) != inventory:
                    raise OSError("arsiv kopyasi byte envanteri dogrulamasini gecemedi")
                os.replace(staging, destination)
                shutil.rmtree(source)
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)

        return {"archived": True, "project_id": ownership.project_id,
                "archive_id": archive_id,
                "location_name": self.archive_status()["location_name"],
                "file_count": len(inventory),
                "total_bytes": sum(inventory.values())}

    def update_script_text(self, ownership: Ownership, *, script_text: str,
                           updated_by: str, notes: str = "") -> dict:
        self._assert_lifecycle_idle(ownership)
        return self.service(ownership).update_script_text(
            script_text=script_text, updated_by=updated_by, notes=notes)

    def assert_owned(self, project_id: str) -> Ownership:
        """Resolve a project id from the request, or refuse.

        A project that does not exist under THIS user's workspace is reported as
        not-found. That is deliberate: a cross-workspace id must not be
        distinguishable from a non-existent one.
        """
        ownership = self.ownership(validate_id(project_id, field="project_id"))
        root = self.storage.namespace_root(ownership)
        if not root.is_dir():
            raise LookupError(f"proje bulunamadi: {project_id}")
        return ownership

    # ----------------------------------------------------------------- jobs
    #: job_type -> (phase gate, PipelineService method, terminal status builder)
    JOB_EXECUTORS = {
        "source_ingest": "run_source_ingest",
        "source_assessment": "run_source_assessment",
        "video_analysis": "run_video_analysis",
        "transcript": "run_transcript",
        "script_generation": "run_script_generation",
        "tts": "run_tts",
        "render": "run_render",
        "metadata_generation": "run_metadata_generation",
        "publish_upload": "run_publish_upload",
        "visibility_change": "run_visibility_change",
    }

    def submit_job(self, ownership: Ownership, job_type: str, *, params=None,
                   submitted_by="console", run_request_id: str | None = None) -> tuple:
        if job_type not in JOB_TYPES:
            raise JobError(f"bilinmeyen is turu: {job_type!r}")
        store = self.job_store(ownership)
        store.recover_abandoned()
        key = self._idempotency_key(
            ownership, job_type, params, run_request_id=run_request_id)
        record, created = store.submit(job_type, idempotency_key=key, params=params,
                                        submitted_by=submitted_by)
        audit = AuditLog(self.storage, ownership)
        audit.record("job.submitted" if created else "job.deduplicated",
                     actor=submitted_by, job_id=record["job_id"],
                     detail={"job_type": job_type, "idempotency_key": key,
                              "existing_status": record.get("status")})
        if created:
            self.runner.submit(store, record["job_id"])
        return store.get(record["job_id"]), created

    def _idempotency_key(self, ownership: Ownership, job_type: str, params,
                         *, run_request_id: str | None = None) -> str:
        """Stable across identical submissions, different across real changes.

        Includes the current plan decision hash so that changing the strategy
        legitimately produces a NEW job instead of returning the old one.
        """
        service = self.service(ownership)
        plan = service.load_artifact("processing_plan")["data"] or {}
        decision = plan.get("decision") or {}
        return google_ai.sha256_text({
            "project": ownership.project_id,
            "job_type": job_type,
            "params": params or {},
            "decision": decision,
            # A new UI click may intentionally rerun the same phase. Reusing
            # the same request id (double click / HTTP retry) still deduplicates.
            "run_request_id": str(run_request_id or "")[:160],
        })

    def automatic_next_step(self, ownership: Ownership, *, submit=False) -> dict:
        """Find or submit the next safe pipeline step.

        Automation never chooses a strategy, a moment, or an approval. It runs
        deterministic/provider phases until the next human gate and reports
        that gate explicitly. Calling it again after approval resumes the flow.
        """
        service = self.service(ownership)
        store = self.job_store(ownership)
        active = store.list(statuses=ACTIVE_STATUSES, limit=20)
        if active:
            return {"state": "running", "job": _job_view(active[0]),
                    "message": "Bir faz halen calisiyor; bitince otomasyon devam edecek."}

        project = service.load_artifact("project")
        source = (project.get("data") or {}).get("source")
        if not source:
            return {"state": "blocked", "blocker": "source_video",
                    "message": "Once Faz 0 sekmesinden kaynak video yukle."}

        assessment = service.load_artifact("source_assessment")
        if not assessment["data"]:
            return self._automatic_job(
                ownership, "source_assessment", {}, submit=submit,
                message="Kaynak degerlendirmesi baslatildi.")

        plan = service.load_artifact("processing_plan")
        if plan["problem"]:
            return {"state": "blocked", "blocker": "processing_plan",
                    "message": plan["problem"]}
        if not plan["data"]:
            return {"state": "blocked", "blocker": "plan_selection",
                    "message": "Faz 0'da isleme turunu sec, plani olustur ve onayla."}
        allowed, reason = service.plan_gate("video_analysis")
        if not allowed:
            return {"state": "blocked", "blocker": "plan_approval", "message": reason}

        hot = service.load_artifact("hot_moments")
        if not hot["data"]:
            return self._automatic_job(
                ownership, "video_analysis",
                {"whisper_model_size": "small", "clip_duration_sec": 240, "top_n": 5},
                submit=submit, message="Video analizi baslatildi.")
        if not (hot["data"].get("hot_moments") or []):
            return {"state": "blocked", "blocker": "no_hot_moments",
                    "message": "Analiz aday moment uretmedi. Moment Explorer'da ayarlari degistirip yeniden calistir."}

        decision = plan["data"].get("decision") or {}
        mode = decision.get("mode")
        slot_indexes = service.slot_indexes()

        selection = service.load_artifact("moment_selection")
        if not selection["data"] or selection["problem"]:
            return {"state": "blocked", "blocker": "moment_selection", "slot": 1,
                    "message": "Moment Explorer'da adaylari izle ve bir moment sec."}

        approvals = service.approval_chain()
        if mode == source_strategy.MODE_NARRATED_THEORY:
            script = service.load_artifact("selected_script")
            script_state = approvals.get("script") or {}
            if (not script["data"] or script["problem"] or
                    (script["data"].get("status") == "failed") or
                    (script_state.get("approved") and not script_state.get("effective_valid"))):
                return self._automatic_job(
                    ownership, "script_generation", {}, submit=submit,
                    message="Script uretimi baslatildi.")
            if not script_state.get("effective_valid"):
                return {"state": "blocked", "blocker": "script_approval",
                        "message": "Script Studio'da scripti incele ve onayla."}

            voice = service.load_artifact("selected_voiceover")
            voice_state = approvals.get("voiceover") or {}
            if (not voice["data"] or voice["problem"] or
                    voice["data"].get("status") in ("failed", "insufficient_data") or
                    (voice_state.get("approved") and not voice_state.get("effective_valid"))):
                return self._automatic_job(
                    ownership, "tts", {"voice": "Kore", "language_code": "tr-TR"},
                    submit=submit, message="Seslendirme baslatildi.")
            if not voice_state.get("effective_valid"):
                return {"state": "blocked", "blocker": "voiceover_approval",
                        "message": "Voice Studio'da sesi dinle ve onayla."}

        # Every Short of a batch is walked in order. Automation renders the
        # machine part of each slot and then STOPS at that slot's human gates --
        # a batch does not turn N approvals into one.
        render_config = dict(decision.get("render") or {})
        for index in slot_indexes:
            slot_label = f" (cikti {index})" if len(slot_indexes) > 1 else ""

            slot_selection = service.load_artifact("moment_selection", slot=index)
            if not slot_selection["data"] or slot_selection["problem"]:
                return {"state": "blocked", "blocker": "moment_selection", "slot": index,
                        "message": f"Moment Explorer'da{slot_label} bir moment sec."}

            # A clip Short's caption text is a human decision. Automation may
            # RUN the recognition, but it stops for the reading and the fix --
            # burning in words nobody checked is exactly what this gate prevents.
            if mode != source_strategy.MODE_NARRATED_THEORY:
                transcript = service.load_artifact("moment_transcript", slot=index)
                transcript_state = service.approval_chain(index).get("transcript") or {}
                if not transcript["data"] or transcript["problem"]:
                    render_cfg = dict(decision.get("render") or {})
                    return self._automatic_job(
                        ownership, "transcript", {
                            "subtitle_whisper_model_size": render_cfg.get(
                                "subtitle_whisper_model_size", "small"),
                            "subtitle_language": render_cfg.get("subtitle_language", "auto"),
                            "caption_theme": render_cfg.get("caption_theme", "auto"),
                            "slot": index,
                        }, submit=submit,
                        message=f"Altyazi transkripti baslatildi{slot_label}.")
                if not transcript_state.get("effective_valid"):
                    return {"state": "blocked", "blocker": "transcript_approval",
                            "slot": index,
                            "message": f"Altyazi sekmesinde{slot_label} metni oku, "
                                       "yanlis duyulan kelimeleri duzelt ve onayla."}

            render = service.load_artifact("render_manifest", slot=index)
            if not render["data"] or render["problem"]:
                params = {
                    "render_mode": render_config.get("render_mode", "blurred_background"),
                    "preview_mode": render_config.get("preview_mode", True),
                    "subtitle_whisper_model_size": render_config.get(
                        "subtitle_whisper_model_size", "small"),
                    "subtitle_language": render_config.get("subtitle_language", "auto"),
                    "caption_style": render_config.get("caption_style", "active_word_yellow"),
                    "caption_theme": render_config.get("caption_theme", "auto"),
                    "slot": index,
                }
                return self._automatic_job(
                    ownership, "render", params, submit=submit,
                    message=f"Render baslatildi{slot_label}.")

            slot_render_state = (service.approval_chain(index).get("render")
                                 if index != 1 else approvals.get("render")) or {}
            if not slot_render_state.get("effective_valid"):
                return {"state": "blocked", "blocker": "render_approval", "slot": index,
                        "message": f"Render Studio'da{slot_label} videoyu izle ve final onayi ver."}

        total = len(slot_indexes)
        return {"state": "completed",
                "message": (f"Akis tamamlandi; {total} ciktinin render onayi gecerli."
                            if total > 1 else "Akis tamamlandi; render onayi gecerli.")}

    def submit_batch_render(self, ownership: Ownership, *, submitted_by="console",
                            run_request_id=None, slots=None) -> dict:
        """Queue a render for every slot that is ready, in one click.

        "Ready" means a human already chose that slot's moment and the plan gate
        is open -- the batch queues machine work only. Slots still waiting on a
        person are REPORTED as skipped with the reason, never quietly rendered
        from a moment nobody picked.

        Each slot is a separate job with its own params, so the existing
        idempotency key already distinguishes them: a double-clicked batch button
        deduplicates per slot instead of producing a second set of renders.
        """
        service = self.service(ownership)
        state = service.batch_state()
        requested = set(int(s) for s in slots) if slots else None

        submitted, skipped = [], []
        for entry in state["slots"]:
            index = entry["index"]
            if requested is not None and index not in requested:
                continue
            if not entry["runnable"]:
                skipped.append({"slot": index, "state": entry["state"],
                                "reason": entry.get("problem") or state.get("render_blocker")
                                or "bu yuva makine isine hazir degil"})
                continue
            record, created = self.submit_job(
                ownership, "render",
                params=self._slot_render_params(service, index),
                submitted_by=submitted_by,
                run_request_id=f"{run_request_id or 'batch'}:{index}")
            submitted.append({"slot": index, "job": _job_view(record), "created": created})

        return {"submitted": submitted, "skipped": skipped,
                "submitted_count": len(submitted), "skipped_count": len(skipped),
                "batch": service.batch_state()}

    def _slot_render_params(self, service, slot: int) -> dict:
        """Render params for one slot, taken from the APPROVED plan.

        Read from the plan rather than from the request so a batch cannot render
        at settings the approval hash never covered.
        """
        decision = ((service.load_artifact("processing_plan")["data"] or {})
                    .get("decision") or {})
        render_config = dict(decision.get("render") or {})
        return {
            "render_mode": render_config.get("render_mode", "blurred_background"),
            "preview_mode": render_config.get("preview_mode", True),
            "subtitle_whisper_model_size": render_config.get(
                "subtitle_whisper_model_size", "small"),
            "subtitle_language": render_config.get("subtitle_language", "auto"),
            "caption_style": render_config.get("caption_style", "active_word_yellow"),
            "caption_theme": render_config.get("caption_theme", "auto"),
            "slot": int(slot),
        }

    def _automatic_job(self, ownership, job_type, params, *, submit, message) -> dict:
        if not submit:
            return {"state": "ready", "next_job_type": job_type, "message": message}
        record, created = self.submit_job(
            ownership, job_type, params=params, submitted_by="console:auto")
        if not created:
            return {"state": "blocked", "blocker": "deduplicated_job",
                    "job": _job_view(record),
                    "message": "Bu faz daha once ayni girdilerle calistirildi. Faz sekmesinden manuel yeniden calistir."}
        return {"state": "submitted", "next_job_type": job_type,
                "job": _job_view(record), "message": message}

    def log_runtime(self, event: str, **detail) -> None:
        """Append one scrubbed, process-safe runtime diagnostic event.

        Audit events explain domain changes inside a project. This log instead
        explains transport/runtime behaviour (request started, response sent,
        API error) and therefore lives at the console root. Bodies, query
        strings and headers are deliberately never accepted by this method.
        """
        try:
            record = {
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "event": str(event)[:120],
                "detail": scrub(detail, max_len=2000),
            }
            target = self.storage.root / "logs" / "runtime_events.jsonl"
            with self._runtime_log_lock:
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                    handle.flush()
        except Exception:  # noqa: BLE001 - diagnostics must never break a request
            pass

    def log_error(self, error) -> None:
        """Server-side ONLY. The scrubbed detail lands here; the client gets a code.

        Written to stderr so it is captured by whatever supervises the process,
        and correlated to the response by `debug_id`.
        """
        try:
            line = error.log_line()
            print(f"[console:error] {line}", file=sys.stderr, flush=True)
            self.log_runtime(
                "api.error",
                debug_id=getattr(error, "debug_id", None),
                code=getattr(error, "code", "internal_error"),
                status=getattr(error, "status", 500),
                message=line,
            )
        except Exception:  # noqa: BLE001 - logging must never break a response
            pass

    def _execute_job(self, store: JobStore, job_id: str, worker_id: str = "worker") -> None:
        record = store.get(job_id)
        ownership = Ownership(record["user_id"], record["workspace_id"], record["project_id"])
        audit = AuditLog(self.storage, ownership)
        audit.record("job.started", job_id=job_id, actor=record.get("submitted_by", "console"),
                     detail={"job_type": record["job_type"], "attempt": record.get("attempts")})
        service = self.service(ownership)
        service.bind_cancel_check(lambda: store.cancel_requested(job_id))
        method = getattr(service, self.JOB_EXECUTORS[record["job_type"]])
        # Analysis and render can run for many minutes. Without a heartbeat the
        # lease expires mid-run and a recovery pass would declare the job
        # abandoned while its worker is very much alive.
        heartbeat = _Heartbeat(store, job_id, worker_id)
        heartbeat.start()
        try:
            result = method(job_id=job_id, **(record.get("params") or {}))
        except OperationCancelled:
            store.finish(job_id, STATUS_CANCELLED, error_kind="cancelled_by_user",
                         recoverable=True)
            tracker = ProgressTracker(self.storage, ownership, record["job_type"],
                                      job_id=job_id)
            tracker.attach()
            tracker.stop("kullanici_istegi", message="Kullanici tarafindan durduruldu.")
            audit.record("job.cancelled", job_id=job_id,
                         actor=record.get("cancel_requested_by") or "console")
            return
        except (PipelineServiceError, ArtifactProblem) as exc:
            store.finish(job_id, STATUS_FAILED, error=str(exc),
                         error_kind="pipeline_precondition", recoverable=True)
            # A precondition failure happens BEFORE the phase starts tracking,
            # so record it here or the panel would show nothing at all.
            self._fail_progress(ownership, record, str(exc))
            audit.record("job.failed", job_id=job_id, detail={"reason": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            store.finish(job_id, STATUS_FAILED, error=f"{type(exc).__name__}: {exc}",
                         error_kind="pipeline_error", recoverable=True)
            self._fail_progress(ownership, record, f"{type(exc).__name__}: {exc}")
            audit.record("job.failed", job_id=job_id,
                         detail={"reason": f"{type(exc).__name__}: {exc}"})
            return
        finally:
            heartbeat.stop()

        # Close the tiny race between the phase's final checkpoint and the job
        # record transition. A late stop request must still win over success.
        if store.cancel_requested(job_id):
            store.finish(job_id, STATUS_CANCELLED, error_kind="cancelled_by_user",
                         recoverable=True)
            tracker = ProgressTracker(self.storage, ownership, record["job_type"],
                                      job_id=job_id)
            tracker.attach()
            tracker.stop("kullanici_istegi", message="Kullanici tarafindan durduruldu.")
            audit.record("job.cancelled", job_id=job_id,
                         actor=record.get("cancel_requested_by") or "console")
            return

        artifact_status = (result or {}).get("status")
        # Execution succeeded; the ARTIFACT still decides whether a human is
        # needed. A clean run that produced insufficient_data is not "completed".
        if artifact_status in ("review_required", "experimental"):
            job_status = STATUS_REVIEW_REQUIRED
        elif artifact_status in ("failed", "insufficient_data"):
            job_status = STATUS_FAILED
        else:
            job_status = STATUS_COMPLETED
        store.finish(job_id, job_status, artifact_status=artifact_status,
                     result=_summarize(result),
                     error=None if job_status != STATUS_FAILED
                     else (result or {}).get("status_reason") or "artefakt kullanima hazir degil",
                     error_kind=None if job_status != STATUS_FAILED else "artifact_not_usable",
                     recoverable=job_status == STATUS_FAILED)
        audit.record("job.succeeded" if job_status != STATUS_FAILED else "job.failed",
                     job_id=job_id, detail={"artifact_status": artifact_status})

    def _fail_progress(self, ownership: Ownership, record, error: str) -> None:
        activity = record.get("job_type")
        if activity not in PROGRESS_ACTIVITIES:
            return
        tracker = ProgressTracker(self.storage, ownership, activity,
                                   job_id=record.get("job_id"),
                                   attempt=int(record.get("attempts") or 1))
        tracker.attach()
        existing = load_progress(self.storage, ownership).get(activity)
        if existing and existing.get("state") == "failed":
            return          # the phase already recorded a more specific failure
        tracker.fail(error)

    def retry_job(self, ownership: Ownership, job_id: str, *, requested_by="console") -> dict:
        store = self.job_store(ownership)
        record = store.request_retry(job_id, requested_by=requested_by)
        # Mark the retry BEFORE the worker starts, so the panel flips out of the
        # failed state the moment the user clicks rather than after the phase
        # gets around to its first stage.
        activity = record.get("job_type")
        if activity in PROGRESS_ACTIVITIES:
            tracker = ProgressTracker(self.storage, ownership, activity, job_id=job_id,
                                       attempt=int(record.get("attempts") or 1) + 1)
            tracker.start(f"{int(record.get('attempts') or 1) + 1}. deneme kuyruga alindi")
        AuditLog(self.storage, ownership).record(
            "job.retry_requested", actor=requested_by, job_id=job_id,
            detail={"attempt": record.get("attempts")})
        self.runner.submit(store, job_id)
        return store.get(job_id)

    def cancel_job(self, ownership: Ownership, job_id: str, *, requested_by="console") -> dict:
        """Request stop for one owned job and persist the intent immediately."""
        store = self.job_store(ownership)
        record = store.request_cancel(job_id, requested_by=requested_by)
        activity = record.get("job_type")
        if activity in PROGRESS_ACTIVITIES:
            tracker = ProgressTracker(self.storage, ownership, activity, job_id=job_id)
            tracker.attach()
            if record.get("status") == STATUS_CANCELLED:
                tracker.stop("kullanici_istegi", message="Kuyruktaki is durduruldu.")
            else:
                # The worker still owns the process until its next checkpoint.
                # Keep the state running and say exactly what is happening.
                tracker.note("Durdurma istegi alindi; guvenli cikis bekleniyor.")
        AuditLog(self.storage, ownership).record(
            "job.cancel_requested", actor=requested_by, job_id=job_id,
            detail={"job_type": activity, "status": record.get("status")})
        return store.get(job_id)

    def recover_all_projects(self) -> list:
        """On startup, no job may be left `running` by a dead process.

        Recovery is OWNER-AWARE, not blanket: a job is reclaimed only when its
        recorded owner process is provably dead on this host, or its lease has
        expired. That distinction matters now that `creator_console.worker` can
        run as a separate process -- a restarting server must not kill a healthy
        external worker's job.
        """
        recovered = []
        for project_id in self.project_ids():
            try:
                ownership = self.ownership(project_id)
                store = self.job_store(ownership)
                for record in store.recover_abandoned():
                    recovered.append(record)
                    # Its worker is gone; a bar left at `running` would spin
                    # forever and imply work is still happening.
                    if record.get("job_type") in PROGRESS_ACTIVITIES:
                        mark_interrupted(self.storage, ownership, record["job_type"],
                                          reason="is kurtarma sirasinda calisir bulundu")
                    AuditLog(self.storage, ownership).record(
                        "job.recovered", job_id=record["job_id"],
                        detail={"job_type": record.get("job_type"),
                                 "recoverable": record.get("recoverable")})
            except (JobError, StorageViolation, ValueError):
                continue
        return recovered

    # ---------------------------------------------------------- view models
    def preflight(self, ownership=None) -> dict:
        """Local capability probe. Never reads a secret value, never calls out."""
        return run_preflight(self, ownership)

    def run_diagnostics(self, *, only=None) -> dict:
        """Exercise every capability in a THROWAWAY fixture and report.

        Deliberately slow and deliberately explicit: it runs real ffmpeg work.
        The user's own projects are neither read nor written -- see
        `diagnostics._Fixture`, which builds its own storage root.
        """
        from . import diagnostics

        return diagnostics.run_diagnostics(self, only=only)

    def last_diagnostics(self) -> dict:
        from . import diagnostics

        return diagnostics.load_last_report(self) or diagnostics.empty_report()

    def workspace_view(self) -> dict:
        projects = []
        for project_id in self.project_ids():
            ownership = self.ownership(project_id)
            record = self.service(ownership).load_artifact("project")
            projects.append({
                "project_id": project_id,
                "title": (record["data"] or {}).get("title") or "(okunamadi)",
                "notes": (record["data"] or {}).get("notes") or "",
                "operator_name": (record["data"] or {}).get("operator_name")
                                 or self.service(ownership).project_actor(),
                "problem": record["problem"],
                "created_at": (record["data"] or {}).get("created_at"),
                "updated_at": ((record["data"] or {}).get("updated_at")
                               or (record["data"] or {}).get("created_at")),
                "status": (record["data"] or {}).get("status"),
                "source_name": (((record["data"] or {}).get("source") or {})
                                .get("display_name")),
                "has_source": bool((record["data"] or {}).get("source")),
                "revision_count": len(self.service(ownership).revisions(limit=100)),
            })
        projects.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return {
            "app": {"name": APP_NAME, "version": __version__, "stage": "pre-alpha",
                     "single_user": True, "authenticated": False},
            "owner": self.ownership().to_dict(),
            "projects": projects,
            "archive": self.archive_status(),
            "storage_policy": self.storage_policy_view(),
            "source_library": self.source_library_view(),
            "secrets": self.secrets.public_state(),
            "planned_not_implemented": [dict(item) for item in PLANNED_NOT_IMPLEMENTED],
            "preflight": run_preflight(self),
            "recovered_on_start": [
                {"job_id": r.get("job_id"), "job_type": r.get("job_type")}
                for r in self._recovered_on_start
            ],
            "not_uploaded": True,
            "upload_limits": {"max_bytes": self.config.max_upload_bytes,
                              "streamed_to_disk": True,
                              "supported_extensions": sorted(
                                  video_analysis.SUPPORTED_SOURCE_VIDEO_EXTENSIONS)},
        }

    def project_view(self, ownership: Ownership) -> dict:
        service = self.service(ownership)
        store = self.job_store(ownership)
        store.recover_abandoned()

        project = service.load_artifact("project")
        assessment = service.load_artifact("source_assessment")
        plan = service.load_artifact("processing_plan")
        hot = service.load_artifact("hot_moments")
        selection = service.load_artifact("moment_selection")
        long_form = service.load_artifact("long_form_plan")

        gates = {}
        for phase in source_strategy.GATED_PHASES:
            allowed, reason = service.plan_gate(phase)
            gates[phase] = {"allowed": allowed, "reason": reason}

        jobs = store.list(limit=50)
        moment_views = []
        for raw_moment in ((hot["data"] or {}).get("hot_moments") or []):
            moment = dict(raw_moment)
            preview_path = moment.get("preview_path")
            if preview_path:
                try:
                    # Pipeline artifacts may need an absolute path internally,
                    # but the browser receives only an ownership-scoped media
                    # route. This also repairs legacy Windows absolute paths.
                    moment["preview_path"] = (
                        service.relative(preview_path)
                        if Path(str(preview_path)).is_absolute()
                        else str(preview_path).replace("\\", "/")
                    )
                except (StorageViolation, ValueError):
                    moment["preview_path"] = None
                    moment["preview_problem"] = "preview proje depolama alani disinda"
            moment_views.append(moment)
        candidate_request = dict((hot["data"] or {}).get("candidate_request") or {})
        produced_candidates = len(moment_views)
        requested_candidates = candidate_request.get("requested")
        if not isinstance(requested_candidates, int) or requested_candidates < 1:
            # Legacy manifests predate candidate_request. Recover the user's
            # actual top_n from the newest analysis job instead of misleadingly
            # presenting an old 1-of-5 result as "1 / 1 complete".
            latest_analysis = next(
                (job for job in jobs if job.get("job_type") == "video_analysis"), None)
            try:
                requested_candidates = int(
                    ((latest_analysis or {}).get("params") or {}).get("top_n"))
            except (TypeError, ValueError):
                requested_candidates = produced_candidates or None
        source_summary = dict((hot["data"] or {}).get("source") or {})
        candidate_summary = {
            "requested": requested_candidates,
            "produced": produced_candidates,
            "complete": (requested_candidates is not None
                         and produced_candidates == requested_candidates),
            "policy": candidate_request.get("policy"),
            "analysis_duration_sec": (candidate_request.get("analysis_duration_sec")
                                      or source_summary.get("clip_duration_sec")),
            "window_sec": (candidate_request.get("window_sec")
                           or (hot["data"] or {}).get("window_sec")),
            "stride_sec": (candidate_request.get("stride_sec")
                           or (hot["data"] or {}).get("stride_sec")),
        }
        view = {
            "owner": ownership.to_dict(),
            "project": {"data": project["data"], "problem": project["problem"]},
            "project_actor": service.project_actor(),
            "long_form": {"data": long_form["data"], "problem": long_form["problem"]},
            "archive": self.archive_status(),
            "storage_policy": self.storage_policy_view(),
            "source_library": self.source_library_view(),
            "phase0": {
                "assessment": assessment["data"], "assessment_problem": assessment["problem"],
                "plan": plan["data"], "plan_problem": plan["problem"],
                "panel": (source_strategy.format_assessment_panel(assessment["data"])
                          if assessment["data"] else None),
                "plan_panel": (source_strategy.format_plan_panel(plan["data"])
                               if plan["data"] else None),
                "modes": [
                    {"key": source_strategy.MODE_DIRECT_CLIP_SERIES,
                     "label": "Direct Clip Series",
                     "description": "Coklu hot moment -> coklu Short, orijinal ses korunur, "
                                    "script/TTS API cagrilmaz.",
                     "consumes_api": False},
                    {"key": source_strategy.MODE_NARRATED_THEORY,
                     "label": "Narrated Theory",
                     "description": "Momentler gorsel kanit; teori acisina gore Gemini script "
                                    "+ TTS uretilir, onay zinciri aynen isler.",
                     "consumes_api": True},
                ],
            },
            "phase_gates": gates,
                        "automation": self.automation(ownership).view(),
            "moments": {
                "data": moment_views if hot["data"] else None,
                "problem": hot["problem"],
                "selection": selection["data"], "selection_problem": selection["problem"],
                "candidate_summary": candidate_summary,
            },
            # The chain view, not the per-artifact view: the UI must show an
            # approval as invalid when anything UPSTREAM of it changed.
            "approvals": service.approval_chain(),
            "invalidated_approvals": service.invalidated_approvals(),
            "jobs": [_job_view(job) for job in jobs],
            "job_summary": _job_summary(jobs),
            "outputs": service.outputs(),
            # Per-Short state for the batch board. Derived on the server from the
            # same artifacts and approval chain the queue uses, so the board can
            # never offer an action the pipeline would refuse.
            "batch": service.batch_state(),
            "audit": AuditLog(self.storage, ownership).events(limit=40),
            "secrets": self.secrets.public_state(),
            "planned_not_implemented": [dict(item) for item in PLANNED_NOT_IMPLEMENTED],
            "not_uploaded": True,
            "upload_limits": {"max_bytes": self.config.max_upload_bytes,
                              "streamed_to_disk": True,
                              "supported_extensions": sorted(
                                  video_analysis.SUPPORTED_SOURCE_VIDEO_EXTENSIONS)},
            # The script a human is being asked to approve. Approving a hash for
            # content you cannot read is not a review, so the text itself has to
            # reach the page -- a curated slice of it, not the whole artifact.
            "script": _script_review_view(service),
            # The caption text of a clip Short, for the screen that lets a human
            # read it and fix misheard names before it is burned in.
            "transcript": service.transcript_view(),
            # The selectable narration styles, with the documented voice
            # characteristic behind each -- so the picker can explain itself
            # instead of listing opaque names.
            "narration_presets": narration_presets.preset_public_list(),
            "render_review": service.render_review_state(),
            "voice_review": service.voice_review_state(),
            # Gates plan approval, so the panel that answers it has to ship with
            # the plan screen -- a server gate the UI cannot satisfy is a wall.
            "copyright_review": service.copyright_review_state(),
            # The workflow map is derived here, from the SAME artifacts and
            # approval chain the pipeline itself uses, and shipped already
            # decided so the browser never re-implements a gate.
            "workflow": build_workflow_map(service, ownership, jobs=[_job_view(j) for j in jobs]),
            # Live phase progress, written by whoever is doing the work. Read
            # from disk every time, so a refresh, a second tab and a restarted
            # server all agree.
            "progress": load_progress(self.storage, ownership),
            "revisions": service.revisions(),
            "preflight": run_preflight(self, ownership),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        # Single choke point: no host path may reach the browser.
        return scrub_host_paths(view, self.storage, ownership)

    def shutdown(self) -> None:
        self.runner.shutdown()


def _summarize(result) -> dict:
    """Small, JSON-safe job result summary. Never the whole artifact, never a
    raw SDK object, never a credential."""
    if not isinstance(result, dict):
        return {}
    keys = ("status", "status_reason", "provider", "model", "recommended_mode",
            "pipeline_mode", "audio_path_relative", "latency_sec")
    summary = {k: result[k] for k in keys if k in result}
    if isinstance(result.get("hot_moments"), list):
        summary["moment_count"] = len(result["hot_moments"])
    output = result.get("output")
    if isinstance(output, dict):
        summary["validation_passed"] = (output.get("validation") or {}).get("passed")
    return summary


def _script_review_view(service) -> dict:
    """What a reviewer needs to actually READ the script, and nothing more.

    A curated slice rather than the raw artifact: the router's per-attempt
    diagnostics belong on the jobs screen, not in front of someone judging
    whether the writing is any good.
    """
    loaded = service.load_artifact("selected_script")
    data = loaded["data"]
    if not data:
        return {"data": None, "problem": loaded["problem"]}
    return {
        "problem": loaded["problem"],
        "data": {
            "hook": data.get("hook"),
            "script_text": data.get("script_text"),
            "estimated_duration_sec": data.get("estimated_duration_sec"),
            "character_count": len(data.get("script_text") or ""),
            "language": data.get("language"),
            "provider": data.get("provider"),
            "model": data.get("model"),
            "status": data.get("status"),
            "status_reason": data.get("status_reason"),
            "theory_angle": data.get("theory_angle"),
            "generated_at": data.get("generated_at"),
            "latency_sec": data.get("latency_sec"),
        },
    }


def _job_view(job: dict) -> dict:
    return {
        "job_id": job.get("job_id"),
        "job_type": job.get("job_type"),
        "status": job.get("status"),
        "artifact_status": job.get("artifact_status"),
        "attempts": job.get("attempts"),
        "max_attempts": job.get("max_attempts"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "finished_at": job.get("finished_at"),
        "error": job.get("error"),
        "error_kind": job.get("error_kind"),
        "recoverable": bool(job.get("recoverable")),
        "corrupt": bool(job.get("corrupt")),
        "result": job.get("result"),
        "can_retry": bool(job.get("recoverable")) and job.get("status") in (STATUS_FAILED, STATUS_CANCELLED)
        and int(job.get("attempts") or 0) < int(job.get("max_attempts") or 3),
        "can_cancel": job.get("status") in ACTIVE_STATUSES
        and not bool(job.get("cancel_requested_at")),
        "cancel_requested": bool(job.get("cancel_requested_at")),
    }


def _job_summary(jobs) -> dict:
    summary = {status: 0 for status in
               ("queued", "running", "review_required", "failed", "completed", "cancelled")}
    for job in jobs:
        if job.get("status") in summary:
            summary[job["status"]] += 1
    summary["active"] = sum(summary[s] for s in ACTIVE_STATUSES)
    return summary
