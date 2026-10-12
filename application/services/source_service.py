"""SourceService: the application service for a project's source media.

WHAT THIS OWNS
--------------
The source media attached to one project, and its descriptor:

  - resolving the attached source to a local path other layers can read,
  - reporting duration / fingerprint / descriptor,
  - attaching an owned upload (streamed to the namespace),
  - attaching an already-downloaded file (the trend-ingest hand-off).

It is the ONLY writer of the `source` block of the `project` artifact, so there
is a single place where "which file is this project cut from" is decided.

WHAT IT DELIBERATELY DOES NOT OWN
---------------------------------
- **HTTP**: request streaming, `Content-Length`, headers and disconnect
  semantics stay in the transport layer. This service starts from an already
  valid byte stream plus its expected length.
- **Deterministic media logic**: fingerprinting (`source_strategy`) and probing
  (`render_utils`) stay in `local_pipeline`.
- **Source assessment**: `run_source_assessment` stays on the façade. It is
  assessment/planning workflow (it consumes `hot_moments`, reads a provider
  credential and feeds the processing plan), not source ownership -- a later
  Analysis/Planning service is its real home.
- **Trend research and rights**: candidate discovery, probing, the rights
  decision and the download itself stay in the façade's ingest flow. This
  service only performs the final ATTACH step, after the rights gate has
  already passed. It can never turn a third-party URL into an approved source
  on its own.
- **Video analysis / moment selection**: not source concerns.
- **Source-library root policy**: which server directory may be imported from
  is operator configuration, enforced in `ConsoleApp.import_source_from_library`
  before a stream ever reaches this service.
- **Path safety rules**: delegated to the namespace store, not reimplemented.
"""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

from application import project_record
from application.artifact_catalog import SOURCE_DIRNAME
from application.errors import ArtifactProblem, PipelineServiceError
from application.time_utils import now_iso
from local_pipeline import render_utils, source_strategy, video_analysis

#: Upload chunk size. Large local sources are streamed to disk rather than
#: buffered, so the guard protects disk capacity, not process memory.
_UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024

#: Kept on the exact same allow-list the analysis resolver uses.
SUPPORTED_SOURCE_VIDEO_EXTENSIONS = frozenset(
    video_analysis.SUPPORTED_SOURCE_VIDEO_EXTENSIONS
)


class SourceService:
    """Source media lifecycle and queries for ONE project namespace.

    Satisfies `ports.SourceQuery` structurally, which is the narrow contract
    `EditorService` depends on.
    """

    def __init__(self, *, artifacts, audit, namespace, ownership, binaries,
                 safe_filename, new_id, progress):
        #: `ports.ArtifactRepository` -- the `project` artifact holds the descriptor.
        self._artifacts = artifacts
        #: `ports.AuditSink`.
        self._audit = audit
        #: `ports.ProjectNamespaceStore` -- owns every path-safety rule.
        self._namespace = namespace
        self._ownership = ownership
        #: `application.media_binaries.MediaBinaries`.
        self._binaries = binaries
        #: Identity/sanitisation helpers from the console's id module.
        self._safe_filename = safe_filename
        self._new_id = new_id
        #: `ports.ProgressFactory`. Independently constructed at the
        #: composition root -- Milestone 3's façade-owned tracker callable was
        #: removed in Milestone 4. The service never learns how progress is
        #: persisted or who owns the cancel signal.
        self._progress = progress

    # ------------------------------------------------------------- queries
    def get_descriptor(self) -> dict | None:
        """The `source` block of the project artifact, or None when unattached."""
        project = self._artifacts.load_artifact("project")["data"] or {}
        return project.get("source") or None

    def get_source_path(self) -> Path:
        """Local path of the attached source.

        Raises rather than returning a missing path: every downstream stage
        (ffmpeg, analysis, render) would otherwise fail far from the cause.
        """
        project = self._artifacts.load_artifact("project", required=True)
        if project["problem"]:
            raise ArtifactProblem(project["problem"])
        source = (project["data"] or {}).get("source")
        if not source or not source.get("stored_path"):
            raise PipelineServiceError("bu projede kayitli bir kaynak video yok")
        path = self._namespace.resolve(self._ownership, source["stored_path"])
        if not path.is_file():
            raise ArtifactProblem(
                f"kaynak video manifestte var ama diskte yok: {source['stored_path']}"
            )
        return path

    def get_duration(self) -> float | None:
        """How long the source video is, in seconds, or None.

        Authoritative value: `source_assessment.source.duration_sec` -- what
        ffprobe actually measured in Phase 0. `hot_moments.source` does NOT
        carry a duration (it records paths and the analysis window), so reading
        it there silently yielded `None`, and a timeline whose scale is `None`
        collapses to zero width.

        When the assessment cannot answer (absent, or no recorded duration),
        the attached source is probed directly through
        `render_utils.probe_media` -- the same canonical call Phase 0 uses, so
        the fallback measures the same file the same way. Expected operational
        failures (no source, missing file, unreadable media, no media binary)
        report `None`: the duration is simply unknown and is never invented.
        Unexpected programming errors propagate instead of being disguised as
        "no duration".
        """
        assessment = self._artifacts.load_artifact("source_assessment")
        duration = ((assessment["data"] or {}).get("source") or {}).get("duration_sec")
        if duration:
            return float(duration)
        try:
            ffmpeg_bin, ffprobe_bin = self._binaries.resolve()
            info, _backend = render_utils.probe_media(
                ffmpeg_bin, ffprobe_bin, self.get_source_path())
        except (PipelineServiceError, ArtifactProblem):
            return None  # no project, no registered source, or file gone
        except RuntimeError:
            return None  # probe_media's documented failure: ffprobe AND the
                         # ffmpeg -i fallback both failed, or no ffmpeg exists
        except OSError:
            return None  # a resolved binary cannot be spawned or the file
                         # cannot be read at all
        raw = ((info or {}).get("format") or {}).get("duration")
        try:
            return round(float(raw), 3) if raw else None
        except (TypeError, ValueError):
            return None

    def get_fingerprint(self) -> str | None:
        """The digest approvals about the source material are bound to.

        Deliberately the SOURCE fingerprint, not a render: the question is about
        the material that was taken, so swapping the source has to reset the
        answers even if every later artifact still looks fine.
        """
        return (self.get_descriptor() or {}).get("fingerprint_sha256") or None

    # ---------------------------------------------------------- attachment
    def store_upload(self, *, filename: str, payload: bytes, registered_by: str,
                     track: bool = False) -> dict:
        """Store an uploaded source video inside the project namespace.

        The display filename is sanitized (never trusted as a path) and the
        stored name is prefixed with a generated id, so two uploads called
        `video.mp4` cannot collide or overwrite each other.
        """
        return self._tracked(
            track, filename=filename, stream=io.BytesIO(payload),
            content_length=len(payload), registered_by=registered_by)

    def store_upload_stream(self, *, filename: str, stream, content_length: int,
                            registered_by: str, track: bool = True) -> dict:
        """Stream a large local upload to disk without buffering it in RAM."""
        return self._tracked(
            track, filename=filename, stream=stream,
            content_length=content_length, registered_by=registered_by)

    def _tracked(self, track, *, filename, stream, content_length,
                 registered_by) -> dict:
        tracker = self._progress.create("upload", track=bool(track))
        tracker.start("Yukleme basladi")
        try:
            return self._store(tracker, filename=filename, stream=stream,
                               content_length=content_length,
                               registered_by=registered_by)
        except Exception as exc:  # noqa: BLE001
            tracker.fail(f"{type(exc).__name__}: {exc}")
            raise

    def _store(self, tracker, *, filename, stream, content_length,
               registered_by) -> dict:
        try:
            expected = int(content_length)
        except (TypeError, ValueError) as exc:
            raise PipelineServiceError("yukleme boyutu gecersiz") from exc
        if expected <= 0:
            raise PipelineServiceError("bos dosya yuklenemez")
        display = self._safe_filename(filename, default="source")
        extension = Path(display).suffix.lower()
        if extension not in SUPPORTED_SOURCE_VIDEO_EXTENSIONS:
            supported = ", ".join(sorted(SUPPORTED_SOURCE_VIDEO_EXTENSIONS))
            raise PipelineServiceError(
                f"desteklenmeyen kaynak video uzantisi: {extension or '(yok)'}; "
                f"desteklenenler: {supported}"
            )
        stored_name = f"{self._new_id('source')}__{display}"
        path = self._namespace.resolve(self._ownership, SOURCE_DIRNAME, stored_name,
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
                    chunk = stream.read(min(_UPLOAD_CHUNK_BYTES, expected - received))
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

        record = self._project_record(title=display, created_by=registered_by)
        record["source"] = {
            "display_name": display,
            "stored_path": self._relative(path),
            "size_bytes": received,
            "container_extension": extension,
            "content_sha256": digest.hexdigest(),
            "fingerprint_sha256": source_strategy.source_fingerprint_sha256(path),
            "registered_at": now_iso(),
            "registered_by": str(registered_by)[:120],
        }
        record["status_reason"] = "kaynak video kaydedildi; Faz 0 degerlendirmesi bekleniyor"
        self._artifacts.write_artifact("project", record, actor=registered_by)
        self._audit.record("source.registered", actor=registered_by,
                           detail={"display_name": display, "size_bytes": received})
        tracker.finish(message=f"{display} ({received} bayt) kaydedildi")
        return record

    def attach_downloaded_source(self, stored: Path, manifest: dict, *,
                                 registered_by: str) -> dict:
        """Point the project at an already-downloaded file (trend ingest).

        Called ONLY after the ingest flow has validated the rights decision and
        written the ingest manifest; this method performs no rights check of its
        own and must never be used to attach an unvetted third-party file.

        Deliberately does NOT read the file into memory (an ingest can be
        gigabytes); the fingerprint uses the same sampled digest Phase 0 relies
        on, and `content_sha256` is left None rather than faked.
        """
        record = self._project_record(
            title=manifest.get("title") or "Indirilen kaynak",
            created_by=registered_by)
        record["source"] = {
            "display_name": self._safe_filename(stored.name, default="source.mp4"),
            "stored_path": self._relative(stored),
            "size_bytes": manifest.get("filesize_bytes"),
            "content_sha256": None,
            "fingerprint_sha256": source_strategy.source_fingerprint_sha256(stored),
            "registered_at": now_iso(),
            "registered_by": str(registered_by)[:120],
            "origin": "trend_ingest",
            "candidate_url": manifest.get("candidate_url"),
            "license_type": (manifest.get("rights") or {}).get("license_type"),
        }
        record["status_reason"] = (
            "kaynak indirildi ve projeye baglandi; Faz 0 degerlendirmesi bekleniyor")
        self._artifacts.write_artifact("project", record, actor=registered_by)
        return record

    # ------------------------------------------------------------ internals
    def _relative(self, path) -> str:
        return self._namespace.relative_to_namespace(self._ownership, path)

    def _project_record(self, *, title: str, created_by: str) -> dict:
        """The project artifact to attach a source to, bootstrapping if absent.

        An upload may arrive before a project record exists (direct service use,
        notebooks). Creating it here preserves the previous behaviour exactly;
        the shared factory keeps the record shape and the `project.created`
        audit event identical to `PipelineService.create_project`.
        """
        existing = self._artifacts.load_artifact("project")["data"]
        if existing:
            return existing
        return project_record.create_project(
            artifacts=self._artifacts, audit=self._audit,
            namespace=self._namespace, ownership=self._ownership,
            title=title, created_by=created_by)
