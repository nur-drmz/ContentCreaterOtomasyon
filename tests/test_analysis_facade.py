"""Characterization tests for source assessment + processing plan on the façade.

Written BEFORE the AnalysisService extraction. Every assertion pins behaviour
the refactor must preserve: artifact contents and paths, the advisory-only
recommendation promise, approval invalidation on plan edit, audit events and
ordering, progress stages, cancellation, and the exact exception types.

No real media and no provider calls: `source_strategy.assess_source` takes an
injectable `probe_fn`, and assessment itself never contacts a provider -- it
only reads a `configured` boolean from the secret resolver.
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
from creator_console.progress import OperationCancelled, load_progress
from creator_console.storage import LocalFilesystemStorage
from local_pipeline import render_utils, source_strategy

SOURCE_BYTES = b"\x00\x01synthetic-source" * 1024

#: What a healthy ffprobe would report for a 180s 1080p clip with audio.
PROBE_INFO = {
    "streams": [
        {"codec_type": "video", "width": 1920, "height": 1080},
        {"codec_type": "audio"},
    ],
    "format": {"duration": "180.0"},
}


def fake_probe(info=PROBE_INFO, backend="ffprobe", raise_exc=None):
    def _probe(ffmpeg_bin, ffprobe_bin, path):
        if raise_exc is not None:
            raise raise_exc
        return info, backend

    return _probe


class FakeSecretReference:
    def __init__(self, configured):
        self.configured = configured


class FakeSecrets:
    """Only `reference(kind).configured` is consulted by assessment."""

    def __init__(self, configured=False):
        self._configured = configured
        self.kinds = []

    def reference(self, kind):
        self.kinds.append(kind)
        return FakeSecretReference(self._configured)


@pytest.fixture
def ownership():
    return Ownership("usr_test", "wsp_test", "prj_test")


@pytest.fixture
def storage(tmp_path):
    return LocalFilesystemStorage(tmp_path / "console_root")


@pytest.fixture
def secrets():
    return FakeSecrets(configured=False)


@pytest.fixture
def service(storage, ownership, secrets):
    return PipelineService(storage, ownership, secrets,
                           probe_fn=fake_probe(),
                           ffmpeg_bin="/x/ffmpeg", ffprobe_bin="/x/ffprobe")


@pytest.fixture
def with_source(service):
    service.create_project(title="Analiz", created_by="tester")
    service.register_source_video(filename="src.mp4", payload=SOURCE_BYTES,
                                  registered_by="tester")
    return service


@pytest.fixture
def assessed(with_source):
    assessment = with_source.run_source_assessment(actor="tester")
    return with_source, assessment


# ---------------------------------------------------------------------------
# Source assessment
# ---------------------------------------------------------------------------

class TestRunSourceAssessment:
    def test_artifact_envelope(self, assessed):
        _service, assessment = assessed
        assert assessment["artifact_type"] == "source_assessment"
        assert assessment["schema_version"] == 1
        assert assessment["status"] == "review_required"
        assert assessment["assessed_at"]

    def test_source_block_from_the_probe(self, assessed):
        _service, assessment = assessed
        source = assessment["source"]
        assert source["duration_sec"] == 180.0
        assert source["width"] == 1920 and source["height"] == 1080
        assert source["has_video"] is True and source["has_audio"] is True
        assert source["probe_ok"] is True
        assert source["probe_backend"] == "ffprobe"
        assert source["size_bytes"] == len(SOURCE_BYTES)
        assert len(source["fingerprint_sha256"]) == 64

    def test_recommendation_is_advisory_only(self, assessed):
        """Phase 0 may never select or approve a mode."""
        _service, assessment = assessed
        assert assessment["recommendation_is_advisory"] is True
        assert assessment["selected_mode"] is None
        assert assessment["auto_selected"] is False
        assert assessment["recommended_mode"] in source_strategy.PROCESSING_MODES

    def test_both_modes_are_scored(self, assessed):
        _service, assessment = assessed
        assert set(assessment["mode_scores"]) == set(source_strategy.PROCESSING_MODES)

    def test_written_to_the_canonical_relative_path(self, assessed, storage, ownership):
        _service, _assessment = assessed
        path = storage.resolve(ownership, "results/source_assessment.json")
        assert path.is_file()
        assert json.loads(path.read_text(encoding="utf-8"))["artifact_type"] == (
            "source_assessment")

    def test_audit_event_and_detail(self, assessed):
        service, assessment = assessed
        events = service.audit.events()
        assert events[0]["event_type"] == "assessment.created"
        assert events[0]["actor"] == "tester"
        assert events[0]["artifact"] == "results/source_assessment.json"
        assert events[0]["detail"] == {
            "recommended_mode": assessment["recommended_mode"],
            "auto_selected": assessment["auto_selected"]}
        # The artifact is durable before the event describing it.
        assert events[1]["event_type"] == "artifact.written"

    def test_consults_the_google_credential_flag(self, with_source, secrets):
        with_source.run_source_assessment(actor="tester")
        assert "google_api" in secrets.kinds

    def test_no_credential_value_reaches_the_artifact(self, storage, ownership):
        """Only the `configured` boolean may influence the assessment.

        Credential ALIAS names legitimately appear in blocker text ("gemini
        kimlik bilgisi yok (geminikey / google_api_key)"), so the assertion is
        about the secret VALUE, which must never be reachable.
        """
        secret_value = "sk-live-NEVER-PERSIST-THIS"

        class LeakyReference:
            configured = True
            value = secret_value

        class LeakySecrets:
            def reference(self, kind):
                return LeakyReference()

        service = PipelineService(storage, ownership, LeakySecrets(),
                                  probe_fn=fake_probe(),
                                  ffmpeg_bin="/x/ffmpeg", ffprobe_bin="/x/ffprobe")
        service.create_project(title="Leak", created_by="tester")
        service.register_source_video(filename="src.mp4", payload=SOURCE_BYTES,
                                      registered_by="tester")
        assessment = service.run_source_assessment(actor="tester")
        assert secret_value not in json.dumps(assessment)
        audit_blob = json.dumps(service.audit.events())
        assert secret_value not in audit_blob

    def test_progress_stages_are_reported(self, with_source, storage, ownership):
        with_source.run_source_assessment(actor="tester")
        progress = load_progress(storage, ownership)["source_assessment"]
        assert progress["state"] == "completed"
        assert [s["key"] for s in progress["stages"]] == ["probe", "scoring", "write"]

    def test_hot_moments_are_consumed_when_present(self, with_source):
        with_source.write_artifact("hot_moments", {
            "status": "review_required",
            "hot_moments": [
                {"rank": 1, "score": 80, "transcript_text": "bir cumle" * 20,
                 "start_sec": 0.0, "end_sec": 30.0},
            ],
        }, actor="tester")
        assessment = with_source.run_source_assessment(actor="tester")
        assert assessment["moment_signals"]["available"] is True
        assert assessment["moment_signals"]["moment_count"] == 1

    def test_absent_hot_moments_are_not_fabricated(self, assessed):
        _service, assessment = assessed
        assert assessment["moment_signals"]["available"] is False
        assert assessment["moment_signals"]["moment_count"] == 0


class TestAssessmentFailureModes:
    def test_missing_project_raises_artifact_problem(self, service):
        with pytest.raises(ArtifactProblem, match="bulunamadi"):
            service.run_source_assessment(actor="tester")

    def test_project_without_a_source_raises_service_error(self, service):
        service.create_project(title="Bos", created_by="tester")
        with pytest.raises(PipelineServiceError, match="kayitli bir kaynak video yok"):
            service.run_source_assessment(actor="tester")

    def test_missing_source_file_raises_artifact_problem(self, with_source,
                                                         storage, ownership):
        descriptor = with_source.load_artifact("project")["data"]["source"]
        storage.resolve(ownership, descriptor["stored_path"]).unlink()
        with pytest.raises(ArtifactProblem, match="manifestte var ama diskte yok"):
            with_source.run_source_assessment(actor="tester")

    def test_failure_is_recorded_on_the_progress_record(self, service, storage,
                                                        ownership):
        service.create_project(title="Bos", created_by="tester")
        with pytest.raises(PipelineServiceError):
            service.run_source_assessment(actor="tester")
        progress = load_progress(storage, ownership)["source_assessment"]
        assert progress["state"] == "failed"
        assert "PipelineServiceError" in progress["error"]

    def test_probe_failure_is_not_fatal_and_is_reported(self, storage, ownership,
                                                        secrets):
        """A probe failure must produce `probe_ok=False`, never a guess."""
        service = PipelineService(
            storage, ownership, secrets,
            probe_fn=fake_probe(raise_exc=RuntimeError("ffprobe patladi")),
            ffmpeg_bin="/x/ffmpeg", ffprobe_bin="/x/ffprobe")
        service.create_project(title="Probe", created_by="tester")
        service.register_source_video(filename="src.mp4", payload=SOURCE_BYTES,
                                      registered_by="tester")
        assessment = service.run_source_assessment(actor="tester")
        source = assessment["source"]
        assert source["probe_ok"] is False
        assert "ffprobe patladi" in source["probe_error"]
        assert source["duration_sec"] is None

    def test_cancellation_propagates_as_operation_cancelled(self, with_source):
        with_source.bind_cancel_check(lambda: True)
        with pytest.raises(OperationCancelled):
            with_source.run_source_assessment(actor="tester")


# ---------------------------------------------------------------------------
# Processing plan
# ---------------------------------------------------------------------------

class TestCreateProcessingPlan:
    def test_plan_envelope_and_decision(self, assessed):
        service, _assessment = assessed
        plan = service.create_processing_plan(
            mode="direct_clip_series", selected_by="tester", output_count=1)
        assert plan["artifact_type"] == "processing_plan"
        assert plan["decision"]["mode"] == "direct_clip_series"
        assert plan["decision"]["output_count"] == 1
        assert plan["selected_by"] == "tester"

    def test_plan_starts_unapproved(self, assessed):
        service, _assessment = assessed
        plan = service.create_processing_plan(
            mode="direct_clip_series", selected_by="tester", output_count=1)
        assert plan["human_approval"]["approved"] is False
        assert plan["human_approval"]["approved_content_sha256"] is None
        assert plan["not_uploaded"] is True

    def test_output_paths_are_namespace_relative(self, assessed):
        """An artifact must never carry the operator's absolute layout."""
        service, _assessment = assessed
        plan = service.create_processing_plan(
            mode="direct_clip_series", selected_by="tester", output_count=2)
        for slot in plan["outputs"]:
            for field in ("output_dir", "render_manifest_path", "final_video_path"):
                assert not str(slot[field]).startswith("/")
                assert str(slot[field]).startswith("results/render")

    def test_plan_binds_the_assessment_digest(self, assessed):
        service, assessment = assessed
        plan = service.create_processing_plan(
            mode="direct_clip_series", selected_by="tester", output_count=1)
        assert plan["source_assessment_sha256"] == (
            source_strategy.assessment_content_sha256(assessment))

    def test_written_path_and_audit(self, assessed, storage, ownership):
        service, _assessment = assessed
        plan = service.create_processing_plan(
            mode="direct_clip_series", selected_by="tester", output_count=1)
        assert storage.resolve(ownership, "results/processing_plan.json").is_file()
        events = service.audit.events()
        assert events[0]["event_type"] == "plan.created"
        assert events[0]["detail"] == {
            "mode": "direct_clip_series",
            "output_count": 1,
            "followed_recommendation": plan["followed_recommendation"]}

    def test_requires_an_assessment(self, with_source):
        with pytest.raises(ArtifactProblem, match="bulunamadi"):
            with_source.create_processing_plan(
                mode="direct_clip_series", selected_by="tester", output_count=1)

    def test_corrupt_assessment_raises_artifact_problem(self, assessed, storage,
                                                        ownership):
        service, _assessment = assessed
        storage.resolve(ownership, "results/source_assessment.json").write_text(
            "{broken", encoding="utf-8")
        with pytest.raises(ArtifactProblem, match="gecerli JSON degil"):
            service.create_processing_plan(
                mode="direct_clip_series", selected_by="tester", output_count=1)

    def test_unknown_mode_is_rejected(self, assessed):
        service, _assessment = assessed
        with pytest.raises(ValueError):
            service.create_processing_plan(
                mode="telepathy", selected_by="tester", output_count=1)


class TestUpdateProcessingPlan:
    @pytest.fixture
    def planned(self, assessed):
        service, _assessment = assessed
        plan = service.create_processing_plan(
            mode="direct_clip_series", selected_by="tester", output_count=1,
            notes="ilk")
        return service, plan

    def test_revision_increments_and_records_editor(self, planned):
        service, plan = planned
        assert int(plan.get("revision") or 1) == 1
        rebuilt = service.update_processing_plan(updated_by="editor",
                                                 output_count=2)
        assert rebuilt["revision"] == 2
        assert rebuilt["updated_by"] == "editor"
        assert rebuilt["decision"]["output_count"] == 2

    def test_unspecified_fields_are_preserved(self, planned):
        service, _plan = planned
        rebuilt = service.update_processing_plan(updated_by="editor",
                                                 output_count=2)
        assert rebuilt["notes"] == "ilk"
        assert rebuilt["decision"]["mode"] == "direct_clip_series"

    def test_blank_editor_is_rejected(self, planned):
        service, _plan = planned
        with pytest.raises(PipelineServiceError, match="bos olamaz"):
            service.update_processing_plan(updated_by="   ")

    def test_non_dict_render_config_is_rejected(self, planned):
        service, _plan = planned
        with pytest.raises(PipelineServiceError, match="JSON nesnesi olmali"):
            service.update_processing_plan(updated_by="editor",
                                           render_config="nope")

    def test_missing_plan_is_rejected(self, assessed):
        service, _assessment = assessed
        with pytest.raises(ArtifactProblem, match="bulunamadi"):
            service.update_processing_plan(updated_by="editor")

    def test_edit_resets_human_approval(self, planned):
        service, _plan = planned
        rebuilt = service.update_processing_plan(updated_by="editor",
                                                 output_count=2)
        assert rebuilt["human_approval"]["approved"] is False
        assert rebuilt["human_approval"]["approved_content_sha256"] is None

    def test_plan_updated_audit_detail(self, planned):
        service, _plan = planned
        service.update_processing_plan(updated_by="editor", output_count=3)
        events = service.audit.events()
        assert events[0]["event_type"] == "plan.updated"
        detail = events[0]["detail"]
        assert detail["previous_mode"] == "direct_clip_series"
        assert detail["mode"] == "direct_clip_series"
        assert detail["decision_changed"] is True
        assert detail["revision"] == 2

    def test_editing_an_approved_plan_records_invalidation(self, planned):
        """The approval must not survive an edit, and the fact must be audited."""
        service, plan = planned
        # Plan approval is gated behind the copyright checklist. Satisfy the
        # real gate rather than bypassing it -- the gate itself is unchanged.
        for item in service.copyright_review_state()["items"]:
            service.set_copyright_review_item(
                item["key"], checked=True, reviewed_by="reviewer")
        review = service.describe_plan_review()["review"]
        approved = service.approve(
            "processing_plan", reviewed_sha256=review["content_sha256"],
            approved_by="reviewer", notes="ok")
        assert approved["human_approval"]["approved"] is True

        service.update_processing_plan(updated_by="editor", output_count=2)
        types = [e["event_type"] for e in service.audit.events()]
        assert "approval.invalidated" in types
        invalidated = next(e for e in service.audit.events()
                           if e["event_type"] == "approval.invalidated")
        assert invalidated["detail"]["kind"] == "processing_plan"
        assert invalidated["detail"]["reason"] == "plan edited"
        assert invalidated["detail"]["previous_approved_content_sha256"]
        # And the persisted plan is genuinely unapproved again.
        assert service.load_artifact("processing_plan")["data"][
            "human_approval"]["approved"] is False

    def test_unapproved_edit_records_no_invalidation(self, planned):
        service, _plan = planned
        service.update_processing_plan(updated_by="editor", output_count=2)
        types = [e["event_type"] for e in service.audit.events()]
        assert "approval.invalidated" not in types


class TestDescribePlanReview:
    def test_absent_plan_reports_not_present(self, assessed):
        service, _assessment = assessed
        assert service.describe_plan_review() == {"present": False}

    def test_present_plan_reports_review_and_staleness(self, assessed):
        service, _assessment = assessed
        service.create_processing_plan(
            mode="direct_clip_series", selected_by="tester", output_count=1)
        described = service.describe_plan_review()
        assert described["present"] is True
        assert described["review"]["content_sha256"]
        assert described["staleness"] == []
        assert described["plan"]["artifact_type"] == "processing_plan"

    def test_corrupt_plan_raises_artifact_problem(self, assessed, storage, ownership):
        service, _assessment = assessed
        service.create_processing_plan(
            mode="direct_clip_series", selected_by="tester", output_count=1)
        storage.resolve(ownership, "results/processing_plan.json").write_text(
            "{broken", encoding="utf-8")
        with pytest.raises(ArtifactProblem, match="gecerli JSON degil"):
            service.describe_plan_review()

    def test_replacing_the_source_makes_the_plan_stale(self, assessed):
        """The check that catches 'approved yesterday, then source swapped'."""
        service, _assessment = assessed
        service.create_processing_plan(
            mode="direct_clip_series", selected_by="tester", output_count=1)
        service.register_source_video(filename="other.mp4",
                                      payload=SOURCE_BYTES + b"different",
                                      registered_by="tester")
        service.run_source_assessment(actor="tester")
        assert service.describe_plan_review()["staleness"] != []


# ---------------------------------------------------------------------------
# Duration authority: the known defect, pinned (see §10 / ANALYSIS_SERVICE.md)
# ---------------------------------------------------------------------------

class TestDurationAuthority:
    def test_assessment_becomes_the_duration_authority(self, assessed):
        service, assessment = assessed
        assert assessment["source"]["duration_sec"] == 180.0
        assert service.source_duration_sec() == 180.0

    def test_duration_authority_does_not_cycle_back_into_analysis(self, with_source):
        """Assessment must not ask SourceService for a duration that only
        exists after that same assessment finishes."""
        calls = []
        original = with_source.source.get_duration

        def spy():
            calls.append(1)
            return original()

        with_source.source.get_duration = spy
        with_source.run_source_assessment(actor="tester")
        assert calls == []

    def test_without_an_assessment_the_probe_fallback_runs(
            self, with_source, monkeypatch):
        """Repaired in Milestone 8: with no assessment the lookup probes the
        attached file and returns what it measures -- replacing the old
        behaviour where a wrong-arity call died in a swallowed TypeError and
        every assessment-less project reported None."""
        calls = []
        monkeypatch.setattr(
            render_utils, "probe_media",
            lambda ffmpeg, ffprobe, path: calls.append(path) or
            ({"format": {"duration": "11.0"}, "streams": []}, "ffprobe"))
        assert with_source.source_duration_sec() == 11.0
        assert len(calls) == 1

    def test_the_fallback_probe_call_uses_the_canonical_signature(
            self, with_source, monkeypatch):
        """Regression pin for the Milestones 3-7 root cause: the call site must
        pass (ffmpeg_bin, ffprobe_bin, path) -- binaries first, path last --
        matching `probe_media(ffmpeg_bin, ffprobe_bin, path)`."""
        captured = {}

        def spy(ffmpeg_bin, ffprobe_bin, path):
            captured.update(ffmpeg_bin=ffmpeg_bin, ffprobe_bin=ffprobe_bin,
                            path=path)
            return {"format": {"duration": "1.0"}, "streams": []}, "ffprobe"

        monkeypatch.setattr(render_utils, "probe_media", spy)
        with_source.source_duration_sec()
        assert captured["path"] == with_source.source_path()
        assert captured["ffmpeg_bin"] != captured["path"]


# ---------------------------------------------------------------------------
# Fault injection must still reach assessment + plan persistence
# ---------------------------------------------------------------------------

class TestAnalysisWriteFaultCoverage:
    """Milestone 2 showed `faults.patch_atomic_write` patches a MANUAL list of
    modules binding `atomic_write_json`. Assessment and plan writes now happen
    from `AnalysisService`, so these prove the injected faults still fire."""

    def test_disk_full_writing_the_assessment_raises(self, with_source):
        from creator_console.faults import disk_full, patch_atomic_write

        with patch_atomic_write(exception=disk_full("source_assessment.json"),
                                match="source_assessment.json"):
            with pytest.raises(OSError):
                with_source.run_source_assessment(actor="tester")

    def test_permission_denied_writing_the_assessment_raises(self, with_source):
        from creator_console.faults import patch_atomic_write, permission_denied

        with patch_atomic_write(exception=permission_denied("source_assessment.json"),
                                match="source_assessment.json"):
            with pytest.raises(PermissionError):
                with_source.run_source_assessment(actor="tester")

    def test_partial_assessment_write_is_surfaced_on_the_next_read(self, with_source):
        from creator_console.faults import patch_atomic_write

        with patch_atomic_write(partial_bytes=25, match="source_assessment.json"):
            with pytest.raises(OSError):
                with_source.run_source_assessment(actor="tester")
        loaded = with_source.load_artifact("source_assessment")
        assert loaded["data"] is None
        assert loaded["problem"]

    def test_disk_full_writing_the_plan_raises(self, assessed):
        from creator_console.faults import disk_full, patch_atomic_write

        service, _assessment = assessed
        with patch_atomic_write(exception=disk_full("processing_plan.json"),
                                match="processing_plan.json"):
            with pytest.raises(OSError):
                service.create_processing_plan(
                    mode="direct_clip_series", selected_by="tester",
                    output_count=1)


# ---------------------------------------------------------------------------
# Façade parity + no-façade construction
# ---------------------------------------------------------------------------

def _direct_analysis_service(storage, ownership, secrets, source):
    """An AnalysisService wired by hand -- NO PipelineService anywhere."""
    from application.media_binaries import MediaBinaries
    from application.services import AnalysisService, ApprovalService
    from infrastructure.persistence.local import LocalArtifactRepository
    from infrastructure.progress.local import LocalProgressFactory
    from infrastructure.review.local import LocalReviewGate

    storage.ensure_namespace(ownership)
    audit = AuditLog(storage, ownership)
    artifacts = LocalArtifactRepository(storage, ownership, audit)
    return AnalysisService(
        artifacts=artifacts,
        audit=audit,
        namespace=storage,
        ownership=ownership,
        source=source,
        binaries=MediaBinaries("/x/ffmpeg", "/x/ffprobe"),
        progress=LocalProgressFactory(storage, ownership),
        secrets=secrets,
        approvals=ApprovalService(
            artifacts=artifacts, audit=audit, ownership=ownership,
            review_gate=LocalReviewGate(artifacts, ownership, source)),
        probe_fn=fake_probe(),
    )


class TestFacadeDelegation:
    def test_facade_holds_an_analysis_service(self, service):
        from application.services import AnalysisService

        assert isinstance(service.analysis, AnalysisService)

    def test_analysis_service_is_not_the_facade(self, service):
        assert service.analysis is not service
        assert not isinstance(service.analysis, PipelineService)

    def test_analysis_shares_the_source_port_and_binaries(self, service):
        assert service.analysis._source is service.source
        assert service.analysis._binaries is service._binaries
        assert service.analysis._progress is service._progress

    def test_delegates_assessment(self, with_source, monkeypatch):
        seen = {}
        monkeypatch.setattr(with_source.analysis, "assess_source",
                            lambda **kw: seen.update(kw) or {"ok": True})
        result = with_source.run_source_assessment(job_id="job_1", actor="tester")
        assert result == {"ok": True}
        assert seen == {"job_id": "job_1", "actor": "tester"}

    def test_delegates_plan_creation(self, assessed, monkeypatch):
        service, _assessment = assessed
        seen = {}
        monkeypatch.setattr(service.analysis, "create_processing_plan",
                            lambda **kw: seen.update(kw) or {"ok": True})
        service.create_processing_plan(mode="direct_clip_series",
                                       selected_by="tester", output_count=2)
        assert seen["mode"] == "direct_clip_series"
        assert seen["output_count"] == 2

    def test_delegates_plan_update_and_review(self, assessed, monkeypatch):
        service, _assessment = assessed
        monkeypatch.setattr(service.analysis, "update_processing_plan",
                            lambda **kw: {"updated": kw["updated_by"]})
        monkeypatch.setattr(service.analysis, "describe_plan_review",
                            lambda: {"present": "delegated"})
        assert service.update_processing_plan(updated_by="ed") == {"updated": "ed"}
        assert service.describe_plan_review() == {"present": "delegated"}

    def test_standalone_service_produces_an_identical_assessment(
            self, with_source, storage, secrets):
        """Same artifact from a hand-wired service as from the façade."""
        facade_assessment = with_source.run_source_assessment(actor="tester")
        direct = _direct_analysis_service(
            storage, with_source.ownership, secrets, with_source.source)
        direct_assessment = direct.assess_source(actor="tester")
        volatile = {"assessed_at"}
        assert ({k: v for k, v in facade_assessment.items() if k not in volatile}
                == {k: v for k, v in direct_assessment.items() if k not in volatile})

    def test_standalone_service_builds_an_equivalent_plan(self, with_source,
                                                          storage, secrets):
        direct = _direct_analysis_service(
            storage, with_source.ownership, secrets, with_source.source)
        direct.assess_source(actor="tester")
        plan = direct.create_processing_plan(
            mode="direct_clip_series", selected_by="tester", output_count=1)
        assert plan["artifact_type"] == "processing_plan"
        assert plan["human_approval"]["approved"] is False
        for slot in plan["outputs"]:
            assert str(slot["output_dir"]).startswith("results/render")

    def test_standalone_service_raises_the_same_errors(self, service, storage,
                                                       secrets):
        direct = _direct_analysis_service(
            storage, service.ownership, secrets, service.source)
        with pytest.raises(ArtifactProblem):
            service.create_processing_plan(mode="direct_clip_series",
                                            selected_by="t", output_count=1)
        with pytest.raises(ArtifactProblem):
            direct.create_processing_plan(mode="direct_clip_series",
                                           selected_by="t", output_count=1)

    def test_queries_report_artifact_trustworthiness(self, assessed):
        service, _assessment = assessed
        record = service.analysis.get_assessment()
        assert record["present"] is True
        assert record["problem"] is None
        assert record["relative_path"] == "results/source_assessment.json"
        assert service.analysis.get_processing_plan()["present"] is False


class TestProgressDependency:
    def test_facade_tracker_comes_from_the_progress_factory(self, service):
        from infrastructure.progress.local import LocalProgressFactory

        assert isinstance(service._progress, LocalProgressFactory)

    def test_progress_factory_satisfies_the_port(self, service):
        from application.ports import ProgressFactory

        assert isinstance(service._progress, ProgressFactory)

    def test_source_service_takes_no_facade_tracker_callable(self):
        import inspect

        from application.services import SourceService

        params = set(inspect.signature(SourceService.__init__).parameters)
        assert "tracker_factory" not in params
        assert "progress" in params

    def test_analysis_service_takes_no_facade_callables(self):
        import inspect

        from application.services import AnalysisService

        params = set(inspect.signature(AnalysisService.__init__).parameters)
        assert not (params & {"tracker_factory", "facade", "pipeline_service",
                              "source_path", "source_duration"})
        assert {"source", "progress", "binaries"} <= params

    def test_bind_cancel_check_reaches_service_built_trackers(self, with_source):
        """Cancellation must not depend on which layer created the tracker."""
        with_source.bind_cancel_check(lambda: True)
        assert with_source._progress.cancel_check is not None
        with pytest.raises(OperationCancelled):
            with_source.run_source_assessment(actor="tester")

    def test_progress_factory_needs_only_storage_and_ownership(self, storage,
                                                               ownership):
        from application.ports import ProgressFactory
        from infrastructure.progress.local import LocalProgressFactory

        factory = LocalProgressFactory(storage, ownership)
        assert isinstance(factory, ProgressFactory)
        assert factory.create("upload", track=False) is not None
