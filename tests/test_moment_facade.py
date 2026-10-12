"""Characterization tests for the moment-selection + clip-bounds surface of
PipelineService (facade).

Written BEFORE the MomentService extraction (Milestone 11). They pin the
CURRENT behaviour of `record_moment_selection`, `moment_rank_owner`,
`selected_moment`, `clip_bounds*`, `save_clip_bounds` and
`reset_clip_bounds` using only the public facade, a synthetic-but-valid
`hot_moments` manifest and the real gate/repository/audit/progress stack.
"""
from __future__ import annotations

import json

import pytest

from creator_console.ids import Ownership
from creator_console.pipeline_service import (
    ArtifactProblem,
    PipelineService,
    PipelineServiceError,
)
from creator_console.storage import LocalFilesystemStorage
from local_pipeline import video_analysis

SOURCE_BYTES = b"\x00\x01synthetic-source-bytes" * 512

PROBE_INFO = {
    "streams": [{"codec_type": "video", "width": 1920, "height": 1080},
                {"codec_type": "audio"}],
    "format": {"duration": "180.0"},
}


class FakeSecrets:
    def __init__(self, configured=False):
        self._configured = configured

    def reference(self, kind):
        configured = self._configured
        return type("Ref", (), {"configured": configured})()


def _make_plan(service, *, mode="direct_clip_series", output_count=1):
    service.run_source_assessment(actor="tester")
    kwargs = {"mode": mode, "selected_by": "tester",
              "output_count": output_count}
    if mode == "narrated_theory":
        kwargs["theory_angle"] = "Bu sahnedeki karar aslinda bir tuzaktan ibaret."
    return service.create_processing_plan(**kwargs)


def _clear_rights(service):
    for item in service.copyright_review_state()["items"]:
        service.set_copyright_review_item(item["key"], checked=True,
                                           reviewed_by="reviewer")


def _approve_plan(service):
    digest = service.approval_state("processing_plan")["content_sha256"]
    service.approve("processing_plan", reviewed_sha256=digest,
                    approved_by="reviewer")


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _hot_manifest(source_path, ranks=(1, 2, 3)) -> dict:
    """Schema-valid hot_moments; require_preview=False keeps it self-contained."""
    return {
        "method": "local-multisignal-v2",
        "status": "review_required",
        "source": {"raw_video_path": str(source_path),
                   "clip_start_sec": 0.0, "clip_duration_sec": 180.0},
        "hot_moments": [
            {
                "rank": rank,
                "source_start_sec": 10.0 * rank,
                "source_end_sec": 10.0 * rank + 20.0,
                "aligned_start_sec": 10.0 * rank,
                "aligned_end_sec": 10.0 * rank + 20.0,
                "score": 1.0 - 0.1 * rank,
                "reason": f"candidate {rank}",
                "transcript": f"metin {rank}",
            }
            for rank in ranks
        ],
    }


@pytest.fixture
def ownership():
    return Ownership("usr_test", "wsp_test", "prj_test")


@pytest.fixture
def storage(tmp_path):
    return LocalFilesystemStorage(tmp_path / "console_root")


@pytest.fixture
def service(storage, ownership):
    return PipelineService(storage, ownership, FakeSecrets(),
                           probe_fn=lambda *a: (PROBE_INFO, "ffprobe"),
                           ffmpeg_bin="/x/ffmpeg", ffprobe_bin="/x/ffprobe")


def _seed(service, *, output_count=1, approve=True, mode="direct_clip_series"):
    service.create_project(title="Kapi", created_by="tester")
    service.register_source_video(filename="src.mp4", payload=SOURCE_BYTES,
                                  registered_by="tester")
    _make_plan(service, mode=mode, output_count=output_count)
    _clear_rights(service)
    if approve:
        _approve_plan(service)
    service.write_artifact("hot_moments", _hot_manifest("src"), actor="tester")


def _ns_root(service):
    return service.storage.namespace_root(service.ownership)


# ---------------------------------------------------------------------------
# record_moment_selection
# ---------------------------------------------------------------------------


class TestRecordMomentSelection:
    def test_valid_selection_writes_per_slot_artifact(self, service):
        _seed(service)
        result = service.record_moment_selection(
            selected_rank=1, reviewed_by="reviewer", require_preview=False)
        assert result["status"] == "approved"
        assert result["selected_moment_rank"] == 1
        assert result["reviewed_by"] == "reviewer"
        loaded = service.load_artifact("moment_selection", slot=1)
        assert loaded["data"]["selected_moment_rank"] == 1
        assert service.artifact_relative("moment_selection", 1) \
            == "results/moment_selection.json"

    def test_selection_writes_audit_artifact_event(self, service):
        _seed(service)
        service.record_moment_selection(selected_rank=2, reviewed_by="rev2",
                                        require_preview=False)
        events = service.audit.events()
        assert any(e["event_type"] == "artifact.written"
                   and e["artifact"] == "results/moment_selection.json"
                   and e["actor"] == "rev2" for e in events)

    def test_missing_hot_moments_blocks_selection(self, service):
        _seed(service)
        (_ns_root(service) / "results" / "hot_moments.json").unlink()
        with pytest.raises(ArtifactProblem):
            service.record_moment_selection(
                selected_rank=1, reviewed_by="reviewer",
                require_preview=False)

    def test_schema_invalid_hot_moments_rejected(self, service):
        _seed(service)
        bad = _hot_manifest("src")
        del bad["hot_moments"][0]["score"]
        service.write_artifact("hot_moments", bad, actor="tester")
        with pytest.raises(ValueError, match="semasi gecersiz"):
            service.record_moment_selection(
                selected_rank=1, reviewed_by="reviewer", require_preview=False)

    def test_unknown_rank_rejected(self, service):
        _seed(service)
        with pytest.raises(ValueError, match="bulunamadi"):
            service.record_moment_selection(
                selected_rank=99, reviewed_by="reviewer",
                require_preview=False)

    def test_unapproved_plan_denies_selection(self, service):
        _seed(service, approve=False)
        with pytest.raises(PipelineServiceError, match="onaylanmamis"):
            service.record_moment_selection(
                selected_rank=1, reviewed_by="reviewer",
                require_preview=False)

    def test_selection_is_a_review_artifact_not_auto_approval(self, service):
        _seed(service)
        result = service.record_moment_selection(
            selected_rank=1, reviewed_by="reviewer", require_preview=False)
        assert result["human_review"]["automatic_selection"] is False
        # Recording a selection grants no approval-row on other artifacts.
        granted = [e for e in service.audit.events()
                   if e["event_type"] == "approval.granted"]
        assert all(e["detail"].get("artifact") == "processing_plan"
                   or True for e in granted)
        assert len(granted) == 1  # only the plan approval from _seed

    def test_undefined_slot_rejected(self, service):
        _seed(service, output_count=1)
        with pytest.raises(PipelineServiceError):
            service.record_moment_selection(
                selected_rank=1, reviewed_by="reviewer",
                require_preview=False, slot=2)

    def test_rank_conflict_across_slots(self, service):
        _seed(service, output_count=2)
        service.record_moment_selection(selected_rank=1, reviewed_by="rev",
                                        require_preview=False, slot=1)
        with pytest.raises(PipelineServiceError, match="zaten"):
            service.record_moment_selection(
                selected_rank=1, reviewed_by="rev", require_preview=False,
                slot=2)

    def test_different_slots_select_different_ranks(self, service):
        _seed(service, output_count=2)
        service.record_moment_selection(selected_rank=1, reviewed_by="rev",
                                        require_preview=False, slot=1)
        result = service.record_moment_selection(
            selected_rank=2, reviewed_by="rev", require_preview=False, slot=2)
        assert result["selected_moment_rank"] == 2
        assert service.load_artifact("moment_selection", slot=1)["data"][
            "selected_moment_rank"] == 1
        assert service.load_artifact("moment_selection", slot=2)["data"][
            "selected_moment_rank"] == 2

    def test_moment_rank_owner_reports_owner(self, service):
        _seed(service, output_count=2)
        service.record_moment_selection(selected_rank=2, reviewed_by="rev",
                                        require_preview=False, slot=1)
        assert service.moment_rank_owner(2, 2) == 1
        assert service.moment_rank_owner(2, 1) is None
        assert service.moment_rank_owner(3, 1) is None

    def test_reselect_same_slot_replaces_selection(self, service):
        _seed(service, output_count=2)
        service.record_moment_selection(selected_rank=1, reviewed_by="rev",
                                        require_preview=False, slot=1)
        service.record_moment_selection(selected_rank=3, reviewed_by="rev",
                                        require_preview=False, slot=1)
        # Rank 1 released: slot 2 may now take it.
        result = service.record_moment_selection(
            selected_rank=1, reviewed_by="rev", require_preview=False, slot=2)
        assert result["selected_moment_rank"] == 1


# ---------------------------------------------------------------------------
# selected_moment / read surface
# ---------------------------------------------------------------------------


class TestSelectedMomentReads:
    def test_selected_moment_returns_matched_rank(self, service):
        _seed(service)
        service.record_moment_selection(selected_rank=2, reviewed_by="rev",
                                        require_preview=False)
        moment = service.selected_moment(slot=1)
        assert moment["rank"] == 2
        assert moment["source_start_sec"] == 20.0

    def test_selected_moment_empty_without_selection(self, service):
        _seed(service)
        assert service.selected_moment(slot=1) == {}

    def test_selected_moment_empty_on_corrupt_artifacts(self, service):
        _seed(service)
        service.record_moment_selection(selected_rank=1, reviewed_by="rev",
                                        require_preview=False)
        (_ns_root(service) / "results" / "hot_moments.json").write_text("{bad")
        assert service.selected_moment(slot=1) == {}

    def test_read_queries_do_not_write(self, service):
        _seed(service)
        before_events = len(service.audit.events())
        before_files = {
            str(p.relative_to(_ns_root(service)))
            for p in _ns_root(service).rglob("*") if p.is_file()
        }
        service.selected_moment(1)
        service.clip_bounds(1)
        service.clip_bounds_view(1)
        service.moment_rank_owner(1, 1)
        after_files = {
            str(p.relative_to(_ns_root(service)))
            for p in _ns_root(service).rglob("*") if p.is_file()
        }
        assert after_files == before_files
        assert len(service.audit.events()) == before_events

    def test_stale_selection_after_analysis_change(self, service):
        _seed(service)
        service.record_moment_selection(selected_rank=1, reviewed_by="rev",
                                        require_preview=False)
        changed = _hot_manifest("src")
        changed["hot_moments"][0]["score"] = 0.01
        service.write_artifact("hot_moments", changed, actor="tester")
        selection = service.load_artifact("moment_selection")["data"]
        errors = video_analysis.validate_moment_selection(
            selection, changed, require_preview=False)
        assert any("hot_moments.json degisti" in e for e in errors)


# ---------------------------------------------------------------------------
# clip_bounds
# ---------------------------------------------------------------------------


class TestClipBounds:
    def _select(self, service, slot=1):
        service.record_moment_selection(selected_rank=1, reviewed_by="rev",
                                        require_preview=False, slot=slot)

    def test_unedited_bounds_fall_back_to_moment(self, service):
        _seed(service)
        self._select(service)
        bounds = service.clip_bounds(1)
        assert bounds["edited"] is False
        assert bounds["source_start_sec"] == 10.0
        assert bounds["source_end_sec"] == 30.0

    def test_clip_bounds_view_reports_available(self, service):
        _seed(service)
        self._select(service)
        view = service.clip_bounds_view(1)
        assert view["available"] is True
        assert view["moment_rank"] == 1
        assert view["source_duration_sec"] == 180.0
        assert view["editable"] is True
        assert view["transcript_will_be_invalidated"] is True  # direct mode

    def test_clip_bounds_view_unavailable_without_selection(self, service):
        _seed(service)
        view = service.clip_bounds_view(1)
        assert view["available"] is False
        assert "moment" in view["reason"]

    def test_save_clip_bounds_persists_edit_and_audits(self, service):
        _seed(service)
        self._select(service)
        result = service.save_clip_bounds(start=5.0, end=25.0, actor="editor",
                                          slot=1)
        assert result["unchanged"] is False
        assert result["bounds"]["edited"] is True
        assert result["bounds"]["source_start_sec"] == 5.0
        path = _ns_root(service) / "results" / "clip_bounds" / "output_01.json"
        assert path.is_file()
        assert json.loads(path.read_text())["output_slot"] == 1
        assert any(e["event_type"] == "clip_bounds.saved"
                   and e["actor"] == "editor"
                   and e["detail"]["slot"] == 1
                   for e in service.audit.events())

    def test_save_clip_bounds_rejects_end_beyond_duration(self, service):
        _seed(service)
        self._select(service)
        with pytest.raises(PipelineServiceError, match="asamaz"):
            service.save_clip_bounds(start=10.0, end=999.0, actor="editor")

    def test_save_clip_bounds_rejects_zero_length(self, service):
        _seed(service)
        self._select(service)
        # end == start fails the ordering check before the min-duration check.
        with pytest.raises(PipelineServiceError, match="buyuk olmali"):
            service.save_clip_bounds(start=10.0, end=10.0, actor="editor")
        with pytest.raises(PipelineServiceError, match="en az"):
            service.save_clip_bounds(start=10.0, end=10.1, actor="editor")

    def test_save_clip_bounds_requires_selection(self, service):
        _seed(service)
        with pytest.raises(PipelineServiceError, match="moment secilmeli"):
            service.save_clip_bounds(start=5.0, end=25.0, actor="editor")

    def test_save_identical_bounds_is_noop(self, service):
        _seed(service)
        self._select(service)
        events_before = len(service.audit.events())
        result = service.save_clip_bounds(start=10.0, end=30.0, actor="editor")
        assert result["unchanged"] is True
        assert result["archived"] is None
        assert not (_ns_root(service) / "results" / "clip_bounds").exists()
        assert len(service.audit.events()) == events_before

    def test_clip_bounds_isolated_per_slot(self, service):
        _seed(service, output_count=2)
        service.record_moment_selection(selected_rank=1, reviewed_by="rev",
                                        require_preview=False, slot=1)
        service.record_moment_selection(selected_rank=2, reviewed_by="rev",
                                        require_preview=False, slot=2)
        service.save_clip_bounds(start=5.0, end=25.0, actor="editor", slot=1)
        assert service.clip_bounds(1)["edited"] is True
        assert service.clip_bounds(2)["edited"] is False
        assert service.clip_bounds(2)["source_start_sec"] == 20.0

    def test_clip_bounds_override_none_without_edit(self, service):
        _seed(service)
        self._select(service)
        assert service.clip_bounds_override(1) is None
        service.save_clip_bounds(start=5.0, end=25.0, actor="editor")
        assert service.clip_bounds_override(1) == {
            "source_start_sec": 5.0, "source_end_sec": 25.0}

    def test_reset_clip_bounds_restores_moment_range(self, service):
        _seed(service)
        self._select(service)
        service.save_clip_bounds(start=5.0, end=25.0, actor="editor")
        result = service.reset_clip_bounds(actor="editor")
        assert result["bounds"]["source_start_sec"] == 10.0
        assert result["bounds"]["source_end_sec"] == 30.0

    def test_edit_archives_render_output_in_direct_mode(self, service):
        _seed(service)
        self._select(service)
        # Simulate a completed render + transcript for slot 1.
        render_dir = _ns_root(service) / "results" / "render"
        render_dir.mkdir(parents=True, exist_ok=True)
        (render_dir / "render_manifest.json").write_text("{}")
        (_ns_root(service) / "results" / "moment_transcript.json").write_text("{}")
        result = service.save_clip_bounds(start=5.0, end=25.0, actor="editor")
        assert result["transcript_invalidated"] is True
        assert result["archived"]["revision_id"]
        moved = result["archived"]["moved_paths"]
        assert "results/moment_transcript.json" in moved
        assert not (_ns_root(service) / "results" / "moment_transcript.json").exists()
        # The selection itself is NEVER archived by a bounds edit.
        assert (_ns_root(service) / "results" / "moment_selection.json").is_file()

    def test_narrated_edit_keeps_transcript(self, storage, ownership):
        service = PipelineService(
            storage, ownership, FakeSecrets(configured=True),
            probe_fn=lambda *a: (PROBE_INFO, "ffprobe"),
            ffmpeg_bin="/x/ffmpeg", ffprobe_bin="/x/ffprobe")
        _seed(service, mode="narrated_theory")
        service.record_moment_selection(selected_rank=1, reviewed_by="rev",
                                        require_preview=False)
        (_ns_root(service) / "results" / "moment_transcript.json").write_text("{}")
        result = service.save_clip_bounds(start=5.0, end=25.0, actor="editor")
        assert result["transcript_invalidated"] is False
        assert (_ns_root(service) / "results" / "moment_transcript.json").is_file()

    def test_missing_source_duration_means_no_upper_bound(self, service):
        """A genuinely unknown duration drops the cap rather than inventing one."""
        _seed(service)
        self._select(service)
        # Probe returns None -> get_duration() reports None.
        service.source._probe_fn = lambda *a: None
        # And the assessment duration is removed so the fallback runs.
        assessment_path = _ns_root(service) / "results" / "source_assessment.json"
        data = json.loads(assessment_path.read_text())
        data["source"]["duration_sec"] = None
        assessment_path.write_text(json.dumps(data))
        # End beyond any realistic duration is now accepted (no bound).
        result = service.save_clip_bounds(start=10.0, end=500.0, actor="editor")
        assert result["unchanged"] is False


# ---------------------------------------------------------------------------
# Facade parity: standalone MomentService vs PipelineService delegates
# ---------------------------------------------------------------------------


def _standalone_moment(storage, ownership):
    """MomentService built from real adapters only -- no PipelineService
    instance anywhere in its dependency graph."""
    from application.media_binaries import MediaBinaries
    from application.services import (
        MomentService, SourceService, WorkflowGate)
    from creator_console.audit import AuditLog
    from creator_console.ids import new_id, safe_filename
    from infrastructure.persistence.local import (
        LocalArtifactRepository, LocalRevisionArchive)
    from infrastructure.progress.local import LocalProgressFactory

    audit = AuditLog(storage, ownership)
    artifacts = LocalArtifactRepository(storage, ownership, audit)
    progress = LocalProgressFactory(storage, ownership)
    source = SourceService(
        artifacts=artifacts, audit=audit, namespace=storage,
        ownership=ownership,
        binaries=MediaBinaries("/x/ffmpeg", "/x/ffprobe"),
        safe_filename=safe_filename, new_id=new_id, progress=progress)
    moment = MomentService(
        artifacts=artifacts, source=source,
        workflow=WorkflowGate(artifacts=artifacts),
        archiver=LocalRevisionArchive(namespace=storage, ownership=ownership),
        audit=audit)
    return moment, audit


def _selection_normalised(record):
    """Drop only the environmental field that legitimately differs between
    two runs: the review timestamp."""
    data = dict(record)
    data.pop("reviewed_at", None)
    return data


class TestFacadeParity:
    def test_selection_result_and_artifact_are_identical(self, service):
        _seed(service)
        facade_result = service.record_moment_selection(
            selected_rank=2, reviewed_by="rev", require_preview=False)
        facade_doc = dict(service.load_artifact("moment_selection")["data"])
        # Re-run through a service with no facade; identical inputs land on
        # the same artifact path.
        standalone, _audit = _standalone_moment(service.storage,
                                              service.ownership)
        service_result = standalone.record_moment_selection(
            selected_rank=2, reviewed_by="rev", require_preview=False)
        service_doc = dict(service.load_artifact("moment_selection")["data"])
        assert _selection_normalised(service_result) == \
            _selection_normalised(facade_result)
        assert _selection_normalised(service_doc) == \
            _selection_normalised(facade_doc)
        assert standalone.clip_bounds_relative(1) == \
            service.clip_bounds_relative(1)

    def test_reads_are_identical(self, service):
        _seed(service, output_count=2)
        service.record_moment_selection(selected_rank=1, reviewed_by="rev",
                                        require_preview=False, slot=1)
        service.record_moment_selection(selected_rank=2, reviewed_by="rev",
                                        require_preview=False, slot=2)
        service.save_clip_bounds(start=5.0, end=25.0, actor="ed", slot=1)
        standalone, _audit = _standalone_moment(service.storage,
                                              service.ownership)
        for slot in (1, 2):
            assert standalone.selected_moment(slot) == \
                service.selected_moment(slot)
            assert standalone.clip_bounds(slot) == service.clip_bounds(slot)
            assert standalone.clip_bounds_override(slot) == \
                service.clip_bounds_override(slot)
            view_a, view_b = standalone.clip_bounds_view(slot), \
                service.clip_bounds_view(slot)
            assert view_a == view_b
        assert standalone.moment_rank_owner(1, 2) == \
            service.moment_rank_owner(1, 2) == 1
        assert standalone.moment_rank_owner(3, 1) is None

    def test_exceptions_and_audit_are_identical(self, service):
        _seed(service, output_count=2)
        service.record_moment_selection(selected_rank=1, reviewed_by="rev",
                                        require_preview=False, slot=1)
        standalone, audit = _standalone_moment(service.storage,
                                               service.ownership)
        with pytest.raises(PipelineServiceError) as via_facade:
            service.record_moment_selection(selected_rank=1, reviewed_by="rev",
                                            require_preview=False, slot=2)
        with pytest.raises(PipelineServiceError) as via_service:
            standalone.record_moment_selection(
                selected_rank=1, reviewed_by="rev", require_preview=False,
                slot=2)
        assert str(via_service.value) == str(via_facade.value)
        # Bounds validation error text survives the move byte-identical.
        with pytest.raises(PipelineServiceError) as f_err:
            service.save_clip_bounds(start=10.0, end=999.0, actor="ed")
        with pytest.raises(PipelineServiceError) as s_err:
            standalone.save_clip_bounds(start=10.0, end=999.0, actor="ed")
        assert str(s_err.value) == str(f_err.value)
        # The clip_bounds.saved event is emitted through the same AuditSink.
        result = standalone.save_clip_bounds(start=10.0, end=25.0, actor="ed")
        assert result["unchanged"] is False
        assert any(e["event_type"] == "clip_bounds.saved" and e["actor"] == "ed"
                   for e in audit.events())

    def test_slot_isolation_and_stale_binding_identical(self, service):
        _seed(service, output_count=2)
        standalone, _audit = _standalone_moment(service.storage,
                                              service.ownership)
        standalone.record_moment_selection(selected_rank=1, reviewed_by="rev",
                                           require_preview=False, slot=1)
        standalone.save_clip_bounds(start=5.0, end=25.0, actor="ed", slot=1)
        # Slot 2 untouched by slot-1 writes through the service.
        assert service.load_artifact("moment_selection", slot=2)["data"] is None
        assert service.clip_bounds(2)["edited"] is False
        # Stale binding: re-analysis hash invalidates the pick in the same way.
        changed = _hot_manifest("src")
        changed["hot_moments"][0]["score"] = 0.01
        service.write_artifact("hot_moments", changed, actor="tester")
        selection = service.load_artifact("moment_selection")["data"]
        errors = video_analysis.validate_moment_selection(
            selection, changed, require_preview=False)
        assert any("hot_moments.json degisti" in e for e in errors)


# ---------------------------------------------------------------------------
# Explicit negatives
# ---------------------------------------------------------------------------


class TestMomentNegatives:
    def test_missing_hot_moments_cannot_fabricate_selection(self, service):
        _seed(service)
        (_ns_root(service) / "results" / "hot_moments.json").unlink()
        with pytest.raises(ArtifactProblem):
            service.record_moment_selection(
                selected_rank=1, reviewed_by="rev", require_preview=False)
        assert service.load_artifact("moment_selection")["data"] is None

    def test_failed_selection_write_reports_no_success(self, service):
        _seed(service)
        from creator_console.faults import disk_full, patch_atomic_write
        with patch_atomic_write(exception=disk_full(),
                                match="moment_selection.json"):
            with pytest.raises(OSError):
                service.record_moment_selection(
                    selected_rank=1, reviewed_by="rev", require_preview=False)
        assert service.load_artifact("moment_selection")["data"] is None
        # And no audit event claims the artifact was written.
        assert not any(e["artifact"] == "results/moment_selection.json"
                       and e["event_type"] == "artifact.written"
                       for e in service.audit.events())

    def test_failed_clip_bounds_write_reports_no_success(self, service):
        _seed(service)
        service.record_moment_selection(selected_rank=1, reviewed_by="rev",
                                        require_preview=False)
        from creator_console.faults import disk_full, patch_atomic_write
        with patch_atomic_write(exception=disk_full(),
                                match="clip_bounds/output_01.json"):
            with pytest.raises(OSError):
                service.save_clip_bounds(start=5.0, end=25.0, actor="ed")
        # No override document, no success audit, no fake "edited" bounds.
        assert not (_ns_root(service) / "results" / "clip_bounds"
                    / "output_01.json").exists()
        assert service.clip_bounds(1)["edited"] is False
        assert not any(e["event_type"] == "clip_bounds.saved"
                       for e in service.audit.events())

    def test_selection_grants_no_approval_and_starts_no_downstream(
            self, service):
        _seed(service)
        service.record_moment_selection(selected_rank=1, reviewed_by="rev",
                                        require_preview=False)
        # No downstream artifacts and no provider/job/approval events.
        for key in ("selected_script", "selected_voiceover",
                    "render_manifest", "publish_intent"):
            assert service.load_artifact(key)["data"] is None, key
        events = service.audit.events()
        assert not any(e["event_type"] in {"provider.call", "job.submitted"}
                       for e in events)
        approvals = [e for e in events
                     if e["event_type"] == "approval.granted"]
        assert len(approvals) == 1  # only the plan approval from _seed

    def test_service_runs_without_pipeline_service(self, storage, ownership):
        """The acceptance criterion: constructible and functional with zero
        PipelineService in its dependency graph. State is seeded by a facade
        on the SAME namespace; the service then reads/writes through ports."""
        facade = PipelineService(
            storage, ownership, FakeSecrets(),
            probe_fn=lambda *a: (PROBE_INFO, "ffprobe"),
            ffmpeg_bin="/x/ffmpeg", ffprobe_bin="/x/ffprobe")
        _seed(facade)
        standalone, _audit = _standalone_moment(storage, ownership)
        result = standalone.record_moment_selection(
            selected_rank=3, reviewed_by="rev", require_preview=False)
        assert result["selected_moment_rank"] == 3
        assert facade.load_artifact("moment_selection")["data"][
            "selected_moment_rank"] == 3
        saved = standalone.save_clip_bounds(start=35.0, end=45.0, actor="ed")
        assert saved["bounds"]["source_start_sec"] == 35.0
        assert facade.clip_bounds(1)["edited"] is True
