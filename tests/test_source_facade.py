"""Characterization tests for the source/media surface of `PipelineService`.

Written BEFORE the SourceService extraction: every assertion pins behaviour the
refactor must preserve -- the project `source` descriptor shape, stored paths,
duration authority, fingerprint identity, audit events and the exact exception
types the API maps to error codes.

No network and no real media: uploads are synthetic bytes, and the fingerprint
is a sampled digest over (name, size, head, tail) which needs no codec.
"""
from __future__ import annotations

import hashlib
import io
import json

import pytest

from creator_console.audit import AuditLog
from creator_console.ids import Ownership
from creator_console.pipeline_service import (
    ArtifactProblem,
    PipelineService,
    PipelineServiceError,
)
from creator_console.storage import LocalFilesystemStorage
from local_pipeline import render_utils, source_strategy

UPLOAD_BYTES = b"\x00\x01synthetic-source-bytes" * 512


@pytest.fixture
def ownership():
    return Ownership("usr_test", "wsp_test", "prj_test")


@pytest.fixture
def storage(tmp_path):
    return LocalFilesystemStorage(tmp_path / "console_root")


@pytest.fixture
def service(storage, ownership):
    return PipelineService(storage, ownership, None)


@pytest.fixture
def project(service):
    return service.create_project(title="Kaynak testi", created_by="tester")


@pytest.fixture
def attached(service, project):
    record = service.register_source_video(
        filename="my source.mp4", payload=UPLOAD_BYTES, registered_by="tester")
    return service, record


# ---------------------------------------------------------------------------
# Upload / attachment
# ---------------------------------------------------------------------------

class TestRegisterSourceVideo:
    def test_descriptor_shape_and_values(self, attached):
        _service, record = attached
        source = record["source"]
        # `safe_filename` sanitises the untrusted upload name: spaces become
        # underscores, so the DISPLAY name is already the sanitised one.
        assert source["display_name"] == "my_source.mp4"
        # `storage.resolve` sanitises each path segment too, which collapses the
        # `<id>__<display>` separator to a single underscore on disk.
        assert source["stored_path"].startswith("source_videos/src_")
        assert source["stored_path"].endswith("_my_source.mp4")
        assert source["size_bytes"] == len(UPLOAD_BYTES)
        assert source["container_extension"] == ".mp4"
        assert source["content_sha256"] == hashlib.sha256(UPLOAD_BYTES).hexdigest()
        assert len(source["fingerprint_sha256"]) == 64
        assert source["registered_by"] == "tester"
        assert source["registered_at"]

    def test_status_reason_updated(self, attached):
        _service, record = attached
        assert record["status_reason"] == (
            "kaynak video kaydedildi; Faz 0 degerlendirmesi bekleniyor")

    def test_bytes_land_in_namespace(self, attached, storage, ownership):
        _service, record = attached
        path = storage.resolve(ownership, record["source"]["stored_path"])
        assert path.read_bytes() == UPLOAD_BYTES

    def test_no_partial_file_left_behind(self, attached, storage, ownership):
        _service, _record = attached
        folder = storage.resolve(ownership, "source_videos")
        assert [p.name for p in folder.iterdir() if p.name.startswith(".")] == []

    def test_persisted_to_project_artifact(self, attached, storage, ownership):
        _service, record = attached
        on_disk = json.loads(
            storage.resolve(ownership, "project.json").read_text(encoding="utf-8"))
        assert on_disk["source"]["stored_path"] == record["source"]["stored_path"]

    def test_audits_source_registered(self, attached):
        service, _record = attached
        events = service.audit.events()
        assert events[0]["event_type"] == "source.registered"
        assert events[0]["actor"] == "tester"
        assert events[0]["detail"] == {"display_name": "my_source.mp4",
                                       "size_bytes": len(UPLOAD_BYTES)}

    def test_two_uploads_of_the_same_name_do_not_collide(self, service, project):
        first = service.register_source_video(
            filename="clip.mp4", payload=UPLOAD_BYTES, registered_by="tester")
        second = service.register_source_video(
            filename="clip.mp4", payload=UPLOAD_BYTES + b"more", registered_by="tester")
        assert (first["source"]["stored_path"]
                != second["source"]["stored_path"])

    def test_unsupported_extension_rejected(self, service, project):
        with pytest.raises(PipelineServiceError,
                           match="desteklenmeyen kaynak video uzantisi"):
            service.register_source_video(
                filename="payload.exe", payload=UPLOAD_BYTES, registered_by="t")

    def test_empty_upload_rejected(self, service, project):
        with pytest.raises(PipelineServiceError, match="bos dosya yuklenemez"):
            service.register_source_video(
                filename="a.mp4", payload=b"", registered_by="t")

    def test_path_traversal_filename_is_sanitised_not_obeyed(self, service, project):
        record = service.register_source_video(
            filename="../../escape.mp4", payload=UPLOAD_BYTES, registered_by="t")
        stored = record["source"]["stored_path"]
        assert ".." not in stored
        assert stored.startswith("source_videos/")

    def test_stream_upload_matches_buffered_upload(self, service, project):
        record = service.register_source_video_stream(
            filename="streamed.mp4", stream=io.BytesIO(UPLOAD_BYTES),
            content_length=len(UPLOAD_BYTES), registered_by="tester", track=False)
        assert record["source"]["content_sha256"] == (
            hashlib.sha256(UPLOAD_BYTES).hexdigest())
        assert record["source"]["size_bytes"] == len(UPLOAD_BYTES)

    def test_truncated_stream_raises_and_cleans_up(self, service, project,
                                                   storage, ownership):
        with pytest.raises(PipelineServiceError, match="yukleme yarida kesildi"):
            service.register_source_video_stream(
                filename="partial.mp4", stream=io.BytesIO(b"short"),
                content_length=len(UPLOAD_BYTES), registered_by="t", track=False)
        folder = storage.resolve(ownership, "source_videos")
        leftovers = [p.name for p in folder.iterdir()] if folder.is_dir() else []
        assert leftovers == [], leftovers

    def test_upload_without_a_project_bootstraps_one(self, service):
        record = service.register_source_video(
            filename="solo.mp4", payload=UPLOAD_BYTES, registered_by="tester")
        assert record["artifact_type"] == "console_project"
        assert record["title"] == "solo.mp4"
        assert record["source"]["display_name"] == "solo.mp4"
        types = [e["event_type"] for e in service.audit.events()]
        assert "project.created" in types
        assert "source.registered" in types


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------

class TestSourcePath:
    def test_returns_absolute_path_inside_namespace(self, attached, storage, ownership):
        service, record = attached
        path = service.source_path()
        assert path.is_absolute()
        assert path.is_file()
        assert path == storage.resolve(ownership, record["source"]["stored_path"])

    def test_missing_project_raises_artifact_problem(self, service):
        with pytest.raises(ArtifactProblem, match="bulunamadi"):
            service.source_path()

    def test_project_without_source_raises_service_error(self, service, project):
        with pytest.raises(PipelineServiceError,
                           match="kayitli bir kaynak video yok"):
            service.source_path()

    def test_manifest_present_but_file_deleted_raises_artifact_problem(
            self, attached, storage, ownership):
        service, record = attached
        storage.resolve(ownership, record["source"]["stored_path"]).unlink()
        with pytest.raises(ArtifactProblem, match="manifestte var ama diskte yok"):
            service.source_path()

    def test_corrupt_project_artifact_raises_artifact_problem(
            self, attached, storage, ownership):
        service, _record = attached
        storage.resolve(ownership, "project.json").write_text("{bad", encoding="utf-8")
        with pytest.raises(ArtifactProblem, match="gecerli JSON degil"):
            service.source_path()


# ---------------------------------------------------------------------------
# Duration authority
# ---------------------------------------------------------------------------

class TestSourceDuration:
    def test_duration_comes_from_source_assessment(self, attached, service):
        service, _record = attached
        service.write_artifact("source_assessment", {
            "artifact_type": "source_assessment", "status": "review_required",
            "source": {"duration_sec": 123.5}, "mode_scores": {},
        }, actor="tester")
        assert service.source_duration_sec() == 123.5

    def test_assessment_duration_is_authoritative_and_never_probes(
            self, attached, service, monkeypatch):
        """A recorded `duration_sec` is returned WITHOUT probing: the
        assessment artifact is the authority, so the fallback must not run."""
        service, _record = attached
        service.write_artifact("source_assessment", {
            "artifact_type": "source_assessment", "status": "review_required",
            "source": {"duration_sec": 123.5}, "mode_scores": {},
        }, actor="tester")
        monkeypatch.setattr(
            render_utils, "probe_media",
            lambda *a: pytest.fail("assessment is authoritative; no probe"))
        assert service.source_duration_sec() == 123.5

    def test_duration_falls_back_to_probe_without_assessment(
            self, attached, monkeypatch):
        """No assessment -> the attached file is probed through the canonical
        `probe_media(ffmpeg_bin, ffprobe_bin, path)` -- binaries first, path
        LAST -- and its measured duration is returned.

        Regression test for the Milestones 3-7 defect: the fallback called
        `probe_media(path, ffprobe)` -- wrong arity AND wrong order -- the
        TypeError was swallowed, and duration was always None."""
        service, _record = attached
        calls = []

        def fake_probe(ffmpeg_bin, ffprobe_bin, path):
            calls.append((ffmpeg_bin, ffprobe_bin, str(path)))
            return {"format": {"duration": "12.5"}, "streams": []}, "ffprobe"

        monkeypatch.setattr(render_utils, "probe_media", fake_probe)
        assert service.source_duration_sec() == 12.5
        (ffmpeg_bin, _ffprobe_bin, path), = calls
        assert path == str(service.source_path())
        assert ffmpeg_bin != path  # path is the THIRD argument, not the first

    def test_missing_field_falls_back_to_probe(self, attached, service,
                                               monkeypatch):
        """An assessment that exists but carries no `source.duration_sec`
        does not block the fallback -- the probe still answers."""
        service, _record = attached
        service.write_artifact("source_assessment", {
            "artifact_type": "source_assessment", "status": "review_required",
            "source": {}, "mode_scores": {},
        }, actor="tester")
        probed = []
        monkeypatch.setattr(
            render_utils, "probe_media",
            lambda ffmpeg, ffprobe, path: probed.append(path) or
            ({"format": {"duration": "9.0"}, "streams": []}, "ffprobe"))
        assert service.source_duration_sec() == 9.0
        assert len(probed) == 1

    def test_zero_duration_is_treated_as_absent(self, attached, service,
                                                monkeypatch):
        """`duration_sec: 0` is falsy -- treated as unrecorded, so the
        fallback probes and returns the real measurement."""
        service, _record = attached
        service.write_artifact("source_assessment", {
            "artifact_type": "source_assessment", "status": "review_required",
            "source": {"duration_sec": 0}, "mode_scores": {},
        }, actor="tester")
        monkeypatch.setattr(
            render_utils, "probe_media",
            lambda ffmpeg, ffprobe, path:
            ({"format": {"duration": "7.25"}, "streams": []}, "ffprobe"))
        assert service.source_duration_sec() == 7.25

    def test_underschema_assessment_falls_back_to_probe(
            self, attached, service, monkeypatch):
        """An assessment that fails artifact validation (missing required
        fields) cannot answer -- the probe supplies the real measurement
        rather than trusting a half-valid record."""
        service, _record = attached
        service.write_artifact("source_assessment", {
            "artifact_type": "source_assessment", "status": "ok",
            "source": {"duration_sec": 42.0},  # `mode_scores` missing
        }, actor="tester")
        monkeypatch.setattr(
            render_utils, "probe_media",
            lambda *a: ({"format": {"duration": "6.0"}, "streams": []}, "ffprobe"))
        assert service.source_duration_sec() == 6.0

    def test_corrupt_assessment_falls_back_to_probe(
            self, attached, service, monkeypatch):
        """Unparseable JSON on disk is the same 'cannot answer' case."""
        service, _record = attached
        path = service.storage.resolve(
            service.ownership, "results/source_assessment.json",
            create_parent=True)
        path.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(
            render_utils, "probe_media",
            lambda *a: ({"format": {"duration": "6.0"}, "streams": []}, "ffprobe"))
        assert service.source_duration_sec() == 6.0

    def test_probe_failure_reports_none_not_fabricated(self, attached,
                                                       monkeypatch):
        """An expected operational failure (RuntimeError = ffprobe AND the
        ffmpeg -i fallback both failed) reports None -- never an invented
        duration."""
        service, _record = attached
        monkeypatch.setattr(
            render_utils, "probe_media",
            lambda *a: (_ for _ in ()).throw(RuntimeError("medya okunamadi")))
        assert service.source_duration_sec() is None

    def test_probe_result_without_duration_reports_none(self, attached,
                                                        monkeypatch):
        service, _record = attached
        monkeypatch.setattr(
            render_utils, "probe_media",
            lambda *a: ({"format": {}, "streams": []}, "ffprobe"))
        assert service.source_duration_sec() is None

    def test_unexpected_probe_error_propagates(self, attached, monkeypatch):
        """A programming error (TypeError -- exactly what the arity defect
        produced) must NOT be swallowed into a silent None."""
        service, _record = attached
        monkeypatch.setattr(
            render_utils, "probe_media",
            lambda *a: (_ for _ in ()).throw(TypeError("bad call")))
        with pytest.raises(TypeError):
            service.source_duration_sec()

    def test_missing_source_reports_none(self, service):
        """No registered source: duration is simply unknown -- None, not an
        exception, matching the port contract."""
        assert service.source_duration_sec() is None

    def test_deleted_source_file_reports_none(self, attached):
        """Descriptor exists but the file is gone: duration is unknown."""
        service, _record = attached
        service.source_path().unlink()
        assert service.source_duration_sec() is None

    def test_no_media_binaries_reports_none(self, attached, monkeypatch):
        """With no ffmpeg/ffprobe at all, the REAL probe_media raises its
        documented RuntimeError -- reported as None, not invented."""
        service, _record = attached
        monkeypatch.setattr(render_utils, "resolve_ffmpeg_binaries",
                            lambda: (None, None))
        assert service.source_duration_sec() is None

    def test_duration_lookup_is_read_only(self, attached, service, monkeypatch):
        """A duration query must not persist artifacts or emit audit events."""
        service, _record = attached
        monkeypatch.setattr(
            render_utils, "probe_media",
            lambda *a: ({"format": {"duration": "5.0"}, "streams": []}, "probe"))
        root = service.storage.namespace_root(service.ownership)
        files_before = {p for p in root.rglob("*") if p.is_file()}
        events_before = len(service.audit.events())
        assert service.source_duration_sec() == 5.0
        files_after = {p for p in root.rglob("*") if p.is_file()}
        assert files_after == files_before
        assert len(service.audit.events()) == events_before


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

class TestSourceFingerprint:
    def test_matches_the_deterministic_sampled_digest(self, attached, storage,
                                                      ownership):
        service, record = attached
        path = storage.resolve(ownership, record["source"]["stored_path"])
        assert service.source_fingerprint() == (
            source_strategy.source_fingerprint_sha256(path))

    def test_none_without_a_project(self, service):
        assert service.source_fingerprint() is None

    def test_none_when_no_source_attached(self, service, project):
        assert service.source_fingerprint() is None

    def test_changes_when_the_source_is_replaced(self, service, project):
        first = service.register_source_video(
            filename="one.mp4", payload=UPLOAD_BYTES, registered_by="t")
        before = service.source_fingerprint()
        second = service.register_source_video(
            filename="two.mp4", payload=UPLOAD_BYTES + b"different",
            registered_by="t")
        assert second["source"]["stored_path"] != first["source"]["stored_path"]
        assert service.source_fingerprint() != before


# ---------------------------------------------------------------------------
# Binaries resolution stays reachable (used by verify_mkv_support.py)
# ---------------------------------------------------------------------------

class TestBinaries:
    def test_binaries_returns_the_injected_pair(self, storage, ownership):
        service = PipelineService(storage, ownership, None,
                                  ffmpeg_bin="/x/ffmpeg", ffprobe_bin="/x/ffprobe")
        assert service.binaries() == ("/x/ffmpeg", "/x/ffprobe")

    def test_binaries_result_is_cached(self, storage, ownership):
        service = PipelineService(storage, ownership, None,
                                  ffmpeg_bin="/x/ffmpeg", ffprobe_bin="/x/ffprobe")
        assert service.binaries() == service.binaries()

    def test_facade_and_source_service_share_one_resolver(self, storage, ownership):
        service = PipelineService(storage, ownership, None,
                                  ffmpeg_bin="/x/ffmpeg", ffprobe_bin="/x/ffprobe")
        assert service.source._binaries is service._binaries

    def test_resolution_is_lazy(self, storage, ownership, monkeypatch):
        """Constructing a service must not shell out looking for ffmpeg."""
        from application import media_binaries

        def explode():
            raise AssertionError("resolve_ffmpeg_binaries called during construction")

        monkeypatch.setattr(media_binaries.render_utils,
                            "resolve_ffmpeg_binaries", explode)
        PipelineService(storage, ownership, None)  # must not raise


# ---------------------------------------------------------------------------
# Façade parity: PipelineService delegates to SourceService
# ---------------------------------------------------------------------------

def _direct_source_service(storage, ownership):
    """A SourceService wired by hand -- NO PipelineService involved anywhere.

    Progress comes from the real local `ports.ProgressFactory`, constructed
    from storage + ownership alone: proof that the Milestone 3 façade-owned
    tracker callable is gone.
    """
    from application.media_binaries import MediaBinaries
    from application.services import SourceService
    from creator_console.ids import new_id, safe_filename
    from infrastructure.persistence.local import LocalArtifactRepository
    from infrastructure.progress.local import LocalProgressFactory

    storage.ensure_namespace(ownership)
    audit = AuditLog(storage, ownership)
    return SourceService(
        artifacts=LocalArtifactRepository(storage, ownership, audit),
        audit=audit,
        namespace=storage,
        ownership=ownership,
        binaries=MediaBinaries("/x/ffmpeg", "/x/ffprobe"),
        safe_filename=safe_filename,
        new_id=new_id,
        progress=LocalProgressFactory(storage, ownership),
    )


@pytest.fixture
def direct_source(storage):
    return _direct_source_service(
        storage, Ownership("usr_test", "wsp_test", "prj_direct_src"))


class TestFacadeDelegation:
    def test_facade_holds_a_source_service(self, service):
        from application.services import SourceService

        assert isinstance(service.source, SourceService)

    def test_source_service_is_not_the_facade(self, service):
        assert service.source is not service
        assert not isinstance(service.source, PipelineService)

    def test_delegates_source_path(self, attached, monkeypatch):
        service, _record = attached
        calls = []
        original = service.source.get_source_path
        monkeypatch.setattr(service.source, "get_source_path",
                            lambda: calls.append(1) or original())
        service.source_path()
        assert len(calls) == 1

    def test_delegates_duration(self, attached, monkeypatch):
        service, _record = attached
        calls = []
        monkeypatch.setattr(service.source, "get_duration",
                            lambda: calls.append(1) or 7.5)
        assert service.source_duration_sec() == 7.5
        assert len(calls) == 1

    def test_delegates_fingerprint(self, attached, monkeypatch):
        service, _record = attached
        monkeypatch.setattr(service.source, "get_fingerprint", lambda: "deadbeef")
        assert service.source_fingerprint() == "deadbeef"

    def test_delegates_upload(self, service, project, monkeypatch):
        seen = {}

        def spy(*, filename, payload, registered_by, track):
            seen.update(filename=filename, size=len(payload),
                        registered_by=registered_by, track=track)
            return {"ok": True}

        monkeypatch.setattr(service.source, "store_upload", spy)
        result = service.register_source_video(
            filename="x.mp4", payload=UPLOAD_BYTES, registered_by="tester")
        assert result == {"ok": True}
        assert seen == {"filename": "x.mp4", "size": len(UPLOAD_BYTES),
                        "registered_by": "tester", "track": False}

    def test_standalone_service_produces_the_same_descriptor(
            self, attached, direct_source):
        facade_service, facade_record = attached
        direct_record = direct_source.store_upload(
            filename="my source.mp4", payload=UPLOAD_BYTES,
            registered_by="tester", track=False)
        # `stored_path` carries a fresh source id, and the fingerprint digests
        # the basename (see `source_fingerprint_sha256`), so both legitimately
        # differ between two separate uploads of identical bytes.
        volatile = {"stored_path", "registered_at", "fingerprint_sha256"}
        facade_source = {k: v for k, v in facade_record["source"].items()
                         if k not in volatile}
        direct_descriptor = {k: v for k, v in direct_record["source"].items()
                             if k not in volatile}
        assert facade_source == direct_descriptor
        # ...but each fingerprint must match the canonical digest of its file.
        assert direct_record["source"]["fingerprint_sha256"] == (
            source_strategy.source_fingerprint_sha256(
                direct_source._namespace.resolve(
                    direct_source._ownership,
                    direct_record["source"]["stored_path"])))

    def test_standalone_service_raises_the_same_errors(self, service, direct_source):
        with pytest.raises(ArtifactProblem, match="bulunamadi"):
            service.source_path()
        with pytest.raises(ArtifactProblem, match="bulunamadi"):
            direct_source.get_source_path()

    def test_standalone_service_reports_the_same_duration_rule(self, direct_source):
        direct_source.store_upload(filename="a.mp4", payload=UPLOAD_BYTES,
                                   registered_by="t", track=False)
        assert direct_source.get_duration() is None
        direct_source._artifacts.write_artifact("source_assessment", {
            "artifact_type": "source_assessment", "status": "review_required",
            "source": {"duration_sec": 88.0}, "mode_scores": {},
        }, actor="t")
        assert direct_source.get_duration() == 88.0


# ---------------------------------------------------------------------------
# Fault injection must still reach the relocated source write path
# ---------------------------------------------------------------------------

class TestSourceWriteFaultCoverage:
    """Milestone 2 showed `faults.patch_atomic_write` patches a MANUAL list of
    modules that bind `atomic_write_json`. Source attachment now persists its
    descriptor from a different module, so these tests prove the injected
    faults still fire instead of silently losing coverage."""

    def test_disk_full_while_attaching_a_source_raises(self, service, project):
        from creator_console.faults import disk_full, patch_atomic_write

        with patch_atomic_write(exception=disk_full("project.json"),
                                match="project.json"):
            with pytest.raises(OSError):
                service.register_source_video(
                    filename="a.mp4", payload=UPLOAD_BYTES, registered_by="t")

    def test_permission_denied_while_attaching_a_source_raises(self, service, project):
        from creator_console.faults import patch_atomic_write, permission_denied

        with patch_atomic_write(exception=permission_denied("project.json"),
                                match="project.json"):
            with pytest.raises(PermissionError):
                service.register_source_video(
                    filename="b.mp4", payload=UPLOAD_BYTES, registered_by="t")

    def test_partial_write_is_surfaced_as_a_problem_on_the_next_read(
            self, service, project):
        from creator_console.faults import patch_atomic_write

        with patch_atomic_write(partial_bytes=20, match="project.json"):
            with pytest.raises(OSError):
                service.register_source_video(
                    filename="c.mp4", payload=UPLOAD_BYTES, registered_by="t")
        loaded = service.load_artifact("project")
        assert loaded["data"] is None
        assert loaded["problem"]


# ---------------------------------------------------------------------------
# Trend-ingest attachment (the rights hand-off)
# ---------------------------------------------------------------------------

class TestIngestedSourceAttachment:
    def test_attaches_downloaded_file_with_origin_and_licence(
            self, service, project, storage, ownership):
        stored = storage.resolve(ownership, "source_videos", "ingest", "dl.mp4",
                                 create_parent=True)
        stored.parent.mkdir(parents=True, exist_ok=True)
        stored.write_bytes(UPLOAD_BYTES)
        manifest = {
            "title": "Indirilen video",
            "filesize_bytes": len(UPLOAD_BYTES),
            "candidate_url": "https://www.youtube.com/watch?v=abc123XYZ_9",
            "rights": {"license_type": "fair_use_claim"},
        }
        record = service._register_ingested_source(
            stored, manifest, registered_by="ingest")
        source = record["source"]
        assert source["origin"] == "trend_ingest"
        assert source["license_type"] == "fair_use_claim"
        assert source["candidate_url"] == manifest["candidate_url"]
        # Never faked: an ingest is not read into memory to hash it.
        assert source["content_sha256"] is None
        assert len(source["fingerprint_sha256"]) == 64
        assert source["stored_path"] == "source_videos/ingest/dl.mp4"

    def test_attachment_alone_emits_no_source_registered_event(
            self, service, project, storage, ownership):
        """The ingest flow audits `source.registered` itself, with job context;
        the attach step must not double-record it."""
        stored = storage.resolve(ownership, "source_videos", "ingest", "dl2.mp4",
                                 create_parent=True)
        stored.parent.mkdir(parents=True, exist_ok=True)
        stored.write_bytes(UPLOAD_BYTES)
        before = [e["event_type"] for e in service.audit.events()]
        service._register_ingested_source(
            stored, {"filesize_bytes": len(UPLOAD_BYTES), "rights": {}},
            registered_by="ingest")
        after = [e["event_type"] for e in service.audit.events()]
        assert "source.registered" not in after[:len(after) - len(before)]
