"""Characterization tests for the EDL/editor surface of `PipelineService`.

Written BEFORE the EditorService extraction: every assertion here pins
behaviour the refactor must preserve byte-for-byte -- returned data shapes,
artifact contents and paths, audit event names/ordering/actors, and the exact
exception types the API layer maps to error codes.

No network, no ffmpeg execution: `render_edl_preview` is intentionally not
covered here (it is render execution, out of the editor boundary).
"""
from __future__ import annotations

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
from local_pipeline import render_utils, scene_editor


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
def built(service):
    """A project with one EDL covering a 60s source."""
    artifact = service.build_edl(
        source_path="source.mp4", source_duration=60.0,
        title="Deneme", actor="tester")
    return service, artifact


def _audit_types(service):
    return [e["event_type"] for e in service.audit.events()]


class TestBuildEdl:
    def test_returns_artifact_envelope(self, built):
        _service, artifact = built
        assert artifact["artifact_type"] == "edl"
        assert artifact["status"] == "draft"
        assert artifact["title"] == "Deneme"
        assert artifact["width"] == 1080 and artifact["height"] == 1920
        assert len(artifact["clips"]) == 1
        clip = artifact["clips"][0]
        assert clip["clip_id"] == "clip_001"
        assert clip["source_start_sec"] == 0.0
        assert clip["source_end_sec"] == 60.0

    def test_writes_canonical_relative_path(self, built, storage, ownership):
        service, _artifact = built
        path = storage.resolve(ownership, "results/edl/edl.json")
        assert path.is_file()
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["artifact_type"] == "edl"

    def test_audits_created_after_artifact_write(self, built):
        service, _artifact = built
        events = service.audit.events()  # newest first
        assert events[0]["event_type"] == "edl.created"
        assert events[0]["actor"] == "tester"
        assert events[0]["artifact"] == "results/edl/edl.json"
        assert events[1]["event_type"] == "artifact.written"


class TestLoadEdl:
    def test_round_trip(self, built):
        service, _artifact = built
        edl = service.load_edl()
        assert isinstance(edl, scene_editor.EDL)
        assert edl.title == "Deneme"
        assert edl.clips[0].source_end_sec == 60.0

    def test_missing_edl_reports_problem(self, service):
        with pytest.raises(ArtifactProblem, match="bulunamadi"):
            service.load_edl()

    def test_corrupt_edl_reports_problem_not_a_default(self, built, storage, ownership):
        service, _artifact = built
        path = storage.resolve(ownership, "results/edl/edl.json")
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ArtifactProblem, match="gecerli JSON degil"):
            service.load_edl()

    def test_wrong_artifact_type_is_a_value_error(self, built, storage, ownership):
        service, _artifact = built
        path = storage.resolve(ownership, "results/edl/edl.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["artifact_type"] = "something_else"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError, match="artefakt turu 'edl' degil"):
            service.load_edl()


class TestUpdateEdlClip:
    def test_source_bounds_update_persists_and_audits(self, built):
        service, _artifact = built
        artifact = service.update_edl_clip(0, start=10.0, end=40.0, actor="editor")
        clip = artifact["clips"][0]
        assert clip["source_start_sec"] == 10.0
        assert clip["source_end_sec"] == 40.0
        events = service.audit.events()
        assert events[0]["event_type"] == "edl.updated"
        assert events[0]["actor"] == "editor"
        assert events[0]["detail"] == {"clip_index": 0}

    def test_invalid_index_raises_service_error(self, built):
        service, _artifact = built
        with pytest.raises(PipelineServiceError, match="gecersiz klip indeksi"):
            service.update_edl_clip(7, start=0.0, end=1.0)
        with pytest.raises(PipelineServiceError, match="gecersiz klip indeksi"):
            service.update_edl_clip(-1, start=0.0, end=1.0)

    def test_invalid_bounds_raise_value_error(self, built):
        service, _artifact = built
        with pytest.raises(ValueError, match="baslangictan buyuk olmali"):
            service.update_edl_clip(0, start=50.0, end=10.0)

    def test_crop_zoom_caption_and_broll(self, built):
        service, _artifact = built
        artifact = service.update_edl_clip(
            0,
            crop={"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8},
            zoom={"scale": 1.5, "anchor_x": 0.5, "anchor_y": 0.4},
            caption_style={"preset": "classic_white", "font_size": 40},
            add_b_roll={"media_path": "b.mp4", "start_sec": 1.0,
                        "duration_sec": 3.0},
        )
        clip = artifact["clips"][0]
        assert clip["crop"] == {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8}
        assert clip["zoom"]["scale"] == 1.5
        assert clip["caption_style"]["preset"] == "classic_white"
        assert clip["caption_style"]["font_size"] == 40
        assert len(clip["b_roll"]) == 1
        assert clip["b_roll"][0]["media_path"] == "b.mp4"

    def test_b_roll_replace(self, built):
        service, _artifact = built
        service.update_edl_clip(0, add_b_roll={
            "media_path": "first.mp4", "start_sec": 0.0, "duration_sec": 2.0})
        artifact = service.update_edl_clip(0, b_roll=[
            {"media_path": "second.mp4", "start_sec": 5.0, "duration_sec": 4.0}])
        b_roll = artifact["clips"][0]["b_roll"]
        assert len(b_roll) == 1
        assert b_roll[0]["media_path"] == "second.mp4"


class TestMusic:
    def test_set_music_persists_and_audits(self, built):
        service, _artifact = built
        artifact = service.set_edl_music("music/track.mp3", actor="editor")
        assert artifact["music_track"] == "music/track.mp3"
        events = service.audit.events()
        assert events[0]["event_type"] == "edl.music_set"
        assert events[0]["detail"] == {"music_path": "music/track.mp3"}


class TestPreviewCommand:
    def test_build_preview_returns_command_without_executing(self, built):
        service, _artifact = built
        command = service.build_edl_preview("prev.mp4")
        assert isinstance(command, list)
        assert command[0] == "ffmpeg"
        assert "-filter_complex" in command
        assert command[-1].endswith("prev.mp4")

    def test_build_preview_rejects_invalid_edl(self, built, storage, ownership):
        service, _artifact = built
        path = storage.resolve(ownership, "results/edl/edl.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["render_mode"] = "not_a_mode"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(PipelineServiceError, match="EDL gecerli degil"):
            service.build_edl_preview()


class TestAddClip:
    def test_add_clip_appends_and_audits(self, built):
        service, _artifact = built
        artifact = service.add_edl_clip(
            source_path="other.mp4", source_start_sec=5.0,
            source_end_sec=25.0, actor="editor")
        assert len(artifact["clips"]) == 2
        assert artifact["clips"][1]["clip_id"] == "clip_002"
        # `add_clip_to_edl` appends as-is; timeline placement is a separate edit.
        assert artifact["clips"][1]["timeline_start_sec"] == 0.0
        events = service.audit.events()
        assert events[0]["event_type"] == "edl.clip_added"
        assert events[0]["detail"] == {"clip_id": "clip_002"}


class TestRevisionFlow:
    def test_request_revision_bumps_version_and_records_actor(self, built):
        service, artifact = built
        assert artifact["version"] == 1
        updated = service.request_edl_revision(
            notes="ilk klip cok uzun", requested_by="reviewer")
        assert updated["version"] == 2
        assert updated["revision_notes"] == "ilk klip cok uzun"
        assert updated["revision_requested_by"] == "reviewer"
        events = service.audit.events()
        assert events[0]["event_type"] == "edl.revision_requested"
        assert events[0]["actor"] == "reviewer"

    def test_resolve_revision_clears_state_and_bumps_version(self, built):
        service, _artifact = built
        service.request_edl_revision(notes="not", requested_by="reviewer")
        resolved = service.resolve_edl_revision(resolved_by="editor")
        assert resolved["version"] == 3
        assert resolved["revision_notes"] == ""
        assert resolved["revision_requested_by"] == ""
        events = service.audit.events()
        assert events[0]["event_type"] == "edl.revision_resolved"
        assert events[0]["actor"] == "editor"


# ---------------------------------------------------------------------------
# Facade parity: PipelineService delegates to EditorService over real adapters
# ---------------------------------------------------------------------------

class FakeSourceQuery:
    """A `ports.SourceQuery` double: the only source knowledge the editor gets."""

    def __init__(self, path="direct-source.mp4", duration=60.0):
        self._path = path
        self._duration = duration
        self.path_calls = 0
        self.duration_calls = 0

    def get_source_path(self):
        self.path_calls += 1
        return self._path

    def get_duration(self):
        self.duration_calls += 1
        return self._duration

    def get_fingerprint(self):
        return None


@pytest.fixture
def fake_source():
    return FakeSourceQuery()


@pytest.fixture
def direct_editor(storage, fake_source):
    """An EditorService wired by hand -- NO PipelineService involved anywhere."""
    from application.services import EditorService
    from infrastructure.persistence.local import LocalArtifactRepository

    ownership = Ownership("usr_test", "wsp_test", "prj_direct")
    storage.ensure_namespace(ownership)
    audit = AuditLog(storage, ownership)
    return EditorService(
        artifacts=LocalArtifactRepository(storage, ownership, audit),
        audit=audit,
        namespace=storage,
        ownership=ownership,
        source=fake_source,
    )


class TestEditorSourceDependency:
    """Milestone 3: the editor's source knowledge is a port, not the façade."""

    def test_fake_source_query_satisfies_the_port(self, fake_source):
        from application.ports import SourceQuery

        assert isinstance(fake_source, SourceQuery)

    def test_source_service_satisfies_the_port(self, service):
        from application.ports import SourceQuery

        assert isinstance(service.source, SourceQuery)

    def test_editor_takes_no_facade_callables(self):
        import inspect

        from application.services import EditorService

        params = set(inspect.signature(EditorService.__init__).parameters)
        assert "source_path" not in params
        assert "source_duration" not in params
        assert "source" in params

    def test_editor_holds_the_source_port_not_the_facade(self, service):
        from creator_console.pipeline_service import PipelineService

        assert not isinstance(service.editor._source, PipelineService)
        assert service.editor._source is service.source

    def test_create_edl_default_source_comes_from_the_port(self, direct_editor,
                                                           fake_source):
        artifact = direct_editor.create_edl(source_duration=42.0, title="Port")
        assert fake_source.path_calls == 1
        assert artifact["clips"][0]["source_path"] == "direct-source.mp4"

    def test_bounds_validation_uses_the_port_duration(self, direct_editor,
                                                      fake_source):
        direct_editor.create_edl(source_duration=60.0)
        direct_editor.update_clip(0, start=0.0, end=50.0)
        assert fake_source.duration_calls == 1

    def test_bounds_beyond_the_port_duration_are_rejected(self, storage):
        from application.services import EditorService
        from infrastructure.persistence.local import LocalArtifactRepository

        ownership = Ownership("usr_test", "wsp_test", "prj_short")
        storage.ensure_namespace(ownership)
        audit = AuditLog(storage, ownership)
        editor = EditorService(
            artifacts=LocalArtifactRepository(storage, ownership, audit),
            audit=audit, namespace=storage, ownership=ownership,
            source=FakeSourceQuery(duration=10.0))
        editor.create_edl(source_duration=10.0)
        with pytest.raises(ValueError, match="kaynak suresini asamaz"):
            editor.update_clip(0, start=0.0, end=30.0)

    def test_unknown_duration_does_not_block_a_clip_update(self, storage):
        """`None` means unknown, not zero: the clip must still be editable."""
        from application.services import EditorService
        from infrastructure.persistence.local import LocalArtifactRepository

        ownership = Ownership("usr_test", "wsp_test", "prj_nodur")
        storage.ensure_namespace(ownership)
        audit = AuditLog(storage, ownership)
        editor = EditorService(
            artifacts=LocalArtifactRepository(storage, ownership, audit),
            audit=audit, namespace=storage, ownership=ownership,
            source=FakeSourceQuery(duration=None))
        editor.create_edl(source_duration=60.0)
        artifact = editor.update_clip(0, start=1.0, end=59.0)
        assert artifact["clips"][0]["source_end_sec"] == 59.0


# ---------------------------------------------------------------------------
# Milestone 8: the repaired probe fallback flows END TO END into validation
# ---------------------------------------------------------------------------

def _seed_analysis(service, *, moment_start=10.0, moment_end=20.0):
    """The minimum plan + moment selection `save_clip_bounds`/`clip_bounds_view`
    need: one output slot, one analysed moment, one selection. The source
    ASSESSMENT is deliberately absent -- the probe fallback supplies duration."""
    service.write_artifact("processing_plan", {
        "artifact_type": "processing_plan", "status": "approved",
        "outputs": [{"index": 1, "moment_rank": 1}],
    }, actor="t")
    service.write_artifact("hot_moments", {
        "artifact_type": "hot_moments", "status": "ok",
        "hot_moments": [{"rank": 1, "source_start_sec": moment_start,
                         "source_end_sec": moment_end}],
        "source": {"raw_video_path": str(service.source_path())},
    }, actor="t")
    service.write_artifact("moment_selection", {
        "artifact_type": "moment_selection", "status": "ok",
        "selected_moment_rank": 1,
    }, actor="t", slot=1)


@pytest.fixture
def probed(service, monkeypatch):
    """A real façade: project + attached source, NO assessment, and the
    repaired fallback probing 30s from the file."""
    service.create_project(title="t", created_by="t")
    service.register_source_video(filename="clip.mp4",
                                  payload=b"\x00\x01fake-mp4" * 2048,
                                  registered_by="t")
    monkeypatch.setattr(
        render_utils, "probe_media",
        lambda ffmpeg, ffprobe, path:
        ({"format": {"duration": "30.0"}, "streams": []}, "ffprobe"))
    return service


class TestProbedDurationEndToEnd:
    """Before the repair, every path below silently saw duration=None -- the
    wrong-arity probe call died in a swallowed TypeError -- so the upper bound
    was never enforced. Now the probe answers and the bounds gate applies."""

    def test_probed_duration_flows_into_edl_clip_validation(self, probed):
        probed.build_edl(source_duration=30.0, title="t", actor="t")
        with pytest.raises(ValueError, match="kaynak suresini asamaz"):
            probed.update_edl_clip(0, start=0.0, end=45.0)
        artifact = probed.update_edl_clip(0, start=0.0, end=29.0)
        assert artifact["clips"][0]["source_end_sec"] == 29.0

    def test_clip_bounds_view_reports_the_probed_duration(self, probed):
        _seed_analysis(probed)
        view = probed.clip_bounds_view(slot=1)
        assert view["available"] is True
        assert view["source_duration_sec"] == 30.0

    def test_save_clip_bounds_rejects_beyond_the_probed_duration(self, probed):
        _seed_analysis(probed)
        with pytest.raises(PipelineServiceError, match="kaynak suresini asamaz"):
            probed.save_clip_bounds(start=0.0, end=45.0, actor="t")

    def test_save_clip_bounds_within_the_probed_duration_succeeds(self, probed):
        _seed_analysis(probed)
        result = probed.save_clip_bounds(start=2.0, end=25.0, actor="t")
        assert result["bounds"]["source_end_sec"] == 25.0

    def test_failed_probe_keeps_duration_unbounded(self, service, monkeypatch):
        """A genuine probe failure reports None -- 'unknown', never zero --
        and the old no-upper-bound semantics are preserved for it."""
        service.create_project(title="t", created_by="t")
        service.register_source_video(filename="clip.mp4",
                                      payload=b"\x00\x01fake-mp4" * 2048,
                                      registered_by="t")
        monkeypatch.setattr(
            render_utils, "probe_media",
            lambda *a: (_ for _ in ()).throw(RuntimeError("medya okunamadi")))
        _seed_analysis(service)
        assert service.clip_bounds_view(slot=1)["source_duration_sec"] is None
        result = service.save_clip_bounds(start=0.0, end=500.0, actor="t")
        assert result["bounds"]["source_end_sec"] == 500.0


class TestFacadeDelegation:
    def test_facade_holds_an_editor_service(self, service):
        from application.services import EditorService

        assert isinstance(service.editor, EditorService)

    def test_facade_uses_a_real_artifact_repository(self, service):
        from infrastructure.persistence.local import LocalArtifactRepository

        assert isinstance(service._artifacts, LocalArtifactRepository)

    def test_direct_service_runs_the_full_lifecycle(self, direct_editor, storage):
        """The whole EDL flow works with no facade instance at all -- proof the
        dependency arrow does not point back at PipelineService."""
        artifact = direct_editor.create_edl(
            source_duration=60.0, title="Direkt", actor="editor")
        assert artifact["artifact_type"] == "edl"
        direct_editor.update_clip(0, start=5.0, end=30.0)
        direct_editor.add_clip(source_path="b.mp4", source_end_sec=10.0)
        direct_editor.set_music("m.mp3")
        direct_editor.request_revision(notes="kisalt", requested_by="rev")
        resolved = direct_editor.resolve_revision(resolved_by="ed")
        assert resolved["version"] == 3
        assert len(resolved["clips"]) == 2
        assert resolved["music_track"] == "m.mp3"
        command = direct_editor.build_preview_command()
        assert command[0] == "ffmpeg"

    def test_facade_and_direct_service_write_identical_artifacts(
            self, service, direct_editor, storage, ownership):
        facade_artifact = service.build_edl(
            source_path="s.mp4", source_duration=60.0, title="Ayni",
            actor="editor")
        direct_artifact = direct_editor.create_edl(
            source_path="s.mp4", source_duration=60.0, title="Ayni",
            actor="editor")
        # `created_at` is a timestamp; everything else must match exactly.
        facade_artifact = {k: v for k, v in facade_artifact.items()
                           if k != "created_at"}
        direct_artifact = {k: v for k, v in direct_artifact.items()
                           if k != "created_at"}
        assert facade_artifact == direct_artifact

        # The on-disk bytes are identical too, minus the timestamp.
        facade_path = storage.resolve(ownership, "results/edl/edl.json")
        direct_path = storage.resolve(
            direct_editor._ownership, "results/edl/edl.json")
        facade_disk = json.loads(facade_path.read_text(encoding="utf-8"))
        direct_disk = json.loads(direct_path.read_text(encoding="utf-8"))
        facade_disk.pop("created_at")
        direct_disk.pop("created_at")
        assert facade_disk == direct_disk

    def test_facade_and_direct_service_raise_the_same_errors(
            self, service, direct_editor):
        with pytest.raises(ArtifactProblem):
            service.load_edl()
        with pytest.raises(ArtifactProblem):
            direct_editor.load_edl()

    def test_facade_delegates_mutations_and_audit(
            self, service, direct_editor, monkeypatch):
        calls = []
        original = service.editor.update_clip

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)

        monkeypatch.setattr(service.editor, "update_clip", spy)
        service.build_edl(source_path="s.mp4", source_duration=60.0)
        service.update_edl_clip(0, start=1.0, end=9.0, actor="editor")
        assert len(calls) == 1
        assert calls[0][0] == (0,)
        assert calls[0][1]["start"] == 1.0 and calls[0][1]["actor"] == "editor"
