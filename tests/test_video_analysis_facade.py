"""Characterization tests for PipelineService.run_video_analysis (facade).

These tests pin the CURRENT behaviour of the video-analysis orchestration
before it is extracted into `application.services.video_analysis_service`.
They call the facade public surface and replace only the deterministic
`local_pipeline.video_analysis` engine functions with deterministic doubles --
everything else (gate, source resolution, artifact persistence, progress,
audit) is exercised for real.
"""
from __future__ import annotations

import pytest

from creator_console.ids import Ownership
from creator_console.pipeline_service import (
    ArtifactProblem,
    PipelineService,
    PipelineServiceError,
)
from creator_console.progress import OperationCancelled, load_progress
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
# Deterministic doubles for the canonical engine
# ---------------------------------------------------------------------------


def _fake_manifest(source_path, scope="local") -> dict:
    return {
        "method": "local-multisignal-v2",
        "status": "review_required",
        "source": {
            "raw_video_path": str(source_path),
            "clip_start_sec": 0.0,
            "clip_duration_sec": 180.0,
        },
        "hot_moments": [
            {
                "rank": 1,
                "source_start_sec": 10.0,
                "source_end_sec": 40.0,
                "aligned_start_sec": 10.0,
                "aligned_end_sec": 40.0,
                "score": 0.9,
                "reason": "fake",
                "transcript": "",
            }
        ],
    }


class _AnalysisSpy:
    """Deterministic stand-in for local_pipeline.video_analysis.run_video_analysis."""

    def __init__(self, manifest=None, exc=None):
        self.manifest = manifest
        self.exc = exc
        self.calls = []

    def __call__(self, ffmpeg_bin, raw_video_path, runtime_dir, *,
                 clip_start_sec, clip_duration_sec, whisper_model_size,
                 scene_detector, top_n, preview_dir, progress,
                 adaptive_candidate_windows):
        self.calls.append({
            "ffmpeg_bin": ffmpeg_bin,
            "source": raw_video_path,
            "runtime_dir": runtime_dir,
            "clip_start_sec": clip_start_sec,
            "clip_duration_sec": clip_duration_sec,
            "whisper_model_size": whisper_model_size,
            "scene_detector": scene_detector,
            "top_n": top_n,
            "preview_dir": preview_dir,
            "progress": progress,
            "adaptive_candidate_windows": adaptive_candidate_windows,
        })
        if self.exc is not None:
            raise self.exc
        return dict(self.manifest or _fake_manifest(raw_video_path))


class _FullSourceSpy:
    """Deterministic stand-in for run_full_source_analysis."""

    def __init__(self, manifest=None, exc=None):
        self.manifest = manifest
        self.exc = exc
        self.calls = []

    def __call__(self, ffmpeg_bin, raw_video_path, runtime_dir, *,
                 source_duration_sec, whisper_model_size, scene_detector,
                 top_n, preview_dir, progress):
        self.calls.append({
            "ffmpeg_bin": ffmpeg_bin,
            "source": raw_video_path,
            "runtime_dir": runtime_dir,
            "source_duration_sec": source_duration_sec,
            "whisper_model_size": whisper_model_size,
            "scene_detector": scene_detector,
            "top_n": top_n,
            "preview_dir": preview_dir,
            "progress": progress,
        })
        if self.exc is not None:
            raise self.exc
        manifest = dict(self.manifest or _fake_manifest(
            raw_video_path, scope="full_source"))
        manifest["analysis_scope"] = "full_source"
        manifest["full_source_scan"] = {"regions_scanned": 3}
        return manifest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


def _with_source(service):
    service.create_project(title="Kapi", created_by="tester")
    service.register_source_video(filename="src.mp4", payload=SOURCE_BYTES,
                                  registered_by="tester")


def _approved(service):
    _with_source(service)
    _make_plan(service)
    _clear_rights(service)
    _approve_plan(service)
    return service


def _run(service, **kwargs):
    defaults = dict(job_id="j1", actor="tester")
    defaults.update(kwargs)
    return service.run_video_analysis(**defaults)


def _progress(service):
    return load_progress(service.storage, service.ownership)


def _ns_root(service):
    return service.storage.namespace_root(service.ownership)


# ---------------------------------------------------------------------------
# Gate & prerequisites
# ---------------------------------------------------------------------------


class TestGateAndPrerequisites:
    def test_no_plan_denies_before_any_work(self, service, monkeypatch):
        _with_source(service)
        spy = _AnalysisSpy()
        monkeypatch.setattr(video_analysis, "run_video_analysis", spy)
        with pytest.raises(PipelineServiceError):
            _run(service)
        assert spy.calls == []
        # The gate fires before the tracker is created: no progress record.
        assert "video_analysis" not in _progress(service)

    def test_unapproved_plan_denies(self, service, monkeypatch):
        _with_source(service)
        _make_plan(service)
        _clear_rights(service)
        spy = _AnalysisSpy()
        monkeypatch.setattr(video_analysis, "run_video_analysis", spy)
        with pytest.raises(PipelineServiceError, match="onaylanmamis"):
            _run(service)
        assert spy.calls == []

    def test_full_source_requires_known_duration(self, service, monkeypatch):
        """Approved plan + missing assessment -> full_source refuses."""
        _approved(service)
        (_ns_root(service) / "results" / "source_assessment.json").unlink()
        spy = _FullSourceSpy()
        monkeypatch.setattr(video_analysis, "run_full_source_analysis", spy)
        with pytest.raises(PipelineServiceError, match="suresi bilinmiyor"):
            _run(service, analysis_scope="full_source")
        assert spy.calls == []
        assert _progress(service)["video_analysis"]["state"] == "failed"

    def test_window_analysis_proceeds_without_assessment(self, service,
                                                         monkeypatch):
        """Window scope does not need the source duration."""
        _approved(service)
        (_ns_root(service) / "results" / "source_assessment.json").unlink()
        spy = _AnalysisSpy()
        monkeypatch.setattr(video_analysis, "run_video_analysis", spy)
        result = _run(service)
        assert result["status"] == "review_required"
        assert spy.calls[0]["clip_start_sec"] == 0.0
        assert spy.calls[0]["clip_duration_sec"] == 240.0

    def test_unknown_scope_raises_service_error(self, service, monkeypatch):
        _approved(service)
        spy = _AnalysisSpy()
        monkeypatch.setattr(video_analysis, "run_video_analysis", spy)
        with pytest.raises(PipelineServiceError, match="bilinmeyen analiz kapsami"):
            _run(service, analysis_scope="weird")
        assert spy.calls == []

    def test_focus_region_without_duration_raises_value_error(self, service):
        _approved(service)
        (_ns_root(service) / "results" / "source_assessment.json").unlink()
        with pytest.raises(ValueError, match="sayisal olmali"):
            _run(service, focus_region="end", clip_duration_min=5)

    def test_direct_mode_allows_analysis(self, service, monkeypatch):
        _with_source(service)
        _make_plan(service, mode="direct_clip_series")
        _clear_rights(service)
        _approve_plan(service)
        allowed, _ = service.plan_gate("video_analysis")
        assert allowed
        spy = _AnalysisSpy()
        monkeypatch.setattr(video_analysis, "run_video_analysis", spy)
        result = _run(service)
        assert result["status"] == "review_required"

    def test_narrated_mode_allows_analysis(self, storage, ownership,
                                           monkeypatch):
        # Narrated plans require a configured Gemini credential; fake the
        # capability check only -- no key value exists anywhere.
        service = PipelineService(
            storage, ownership, FakeSecrets(configured=True),
            probe_fn=lambda *a: (PROBE_INFO, "ffprobe"),
            ffmpeg_bin="/x/ffmpeg", ffprobe_bin="/x/ffprobe")
        _with_source(service)
        _make_plan(service, mode="narrated_theory")
        _clear_rights(service)
        _approve_plan(service)
        spy = _AnalysisSpy()
        monkeypatch.setattr(video_analysis, "run_video_analysis", spy)
        result = _run(service)
        assert result["status"] == "review_required"


# ---------------------------------------------------------------------------
# Window-scope analysis
# ---------------------------------------------------------------------------


class TestWindowAnalysis:
    def test_success_writes_hot_moments_artifact(self, service, monkeypatch):
        _approved(service)
        spy = _AnalysisSpy()
        monkeypatch.setattr(video_analysis, "run_video_analysis", spy)
        result = _run(service)
        artifact = service.load_artifact("hot_moments", required=True)
        assert artifact["problem"] is None
        assert artifact["data"] == result
        assert service.artifact_relative("hot_moments") == "results/hot_moments.json"
        assert (_ns_root(service) / "results" / "hot_moments.json").is_file()

    def test_facade_marks_window_scope_and_focus_metadata(self, service,
                                                          monkeypatch):
        _approved(service)
        spy = _AnalysisSpy()
        monkeypatch.setattr(video_analysis, "run_video_analysis", spy)
        result = _run(service, focus_region="middle", clip_duration_min=2)
        assert result["analysis_scope"] == "window"
        assert result["candidate_request"] == {
            "focus_region": "middle",
            "clip_duration_min": 2.0,
            "custom_start_min": None,
            "resolved_start_sec": 30.0,
            "resolved_duration_sec": 120.0,
        }

    def test_focus_region_resolves_analysis_range(self, service, monkeypatch):
        _approved(service)
        spy = _AnalysisSpy()
        monkeypatch.setattr(video_analysis, "run_video_analysis", spy)
        _run(service, focus_region="end", clip_duration_min=2)
        call = spy.calls[0]
        # 180s source, last 2 minutes: start=60, duration=120.
        assert call["clip_start_sec"] == 60.0
        assert call["clip_duration_sec"] == 120.0

    def test_binaries_source_and_dirs_passed_to_engine(self, service,
                                                       monkeypatch):
        _approved(service)
        spy = _AnalysisSpy()
        monkeypatch.setattr(video_analysis, "run_video_analysis", spy)
        _run(service, whisper_model_size="base", scene_detector="adaptive",
             top_n=7)
        call = spy.calls[0]
        assert call["ffmpeg_bin"] == "/x/ffmpeg"
        assert call["source"] == service.source_path()
        assert call["source"].parent.name == "source_videos"
        assert call["runtime_dir"].name == "runtime"
        assert call["preview_dir"].name == "moment_previews"
        assert call["whisper_model_size"] == "base"
        assert call["scene_detector"] == "adaptive"
        assert call["top_n"] == 7
        assert call["adaptive_candidate_windows"] is True
        assert type(call["progress"]).__name__ == "ProgressTracker"

    def test_progress_activity_and_terminal_state(self, service, monkeypatch):
        _approved(service)
        spy = _AnalysisSpy()
        monkeypatch.setattr(video_analysis, "run_video_analysis", spy)
        _run(service)
        rec = _progress(service)["video_analysis"]
        assert rec["state"] == "completed"
        assert rec["activity"] == "video_analysis"
        assert rec["job_id"] == "j1"

    def test_engine_failure_propagates_and_marks_failed(self, service,
                                                        monkeypatch):
        _approved(service)
        spy = _AnalysisSpy(exc=RuntimeError("engine exploded"))
        monkeypatch.setattr(video_analysis, "run_video_analysis", spy)
        with pytest.raises(RuntimeError, match="engine exploded"):
            _run(service)
        rec = _progress(service)["video_analysis"]
        assert rec["state"] == "failed"
        assert "engine exploded" in rec["error"]
        assert not (_ns_root(service) / "results" / "hot_moments.json").exists()

    def test_cancellation_propagates_and_fails_tracker(self, service,
                                                       monkeypatch):
        _approved(service)
        spy = _AnalysisSpy(exc=OperationCancelled("stop"))
        monkeypatch.setattr(video_analysis, "run_video_analysis", spy)
        with pytest.raises(OperationCancelled):
            _run(service)
        assert _progress(service)["video_analysis"]["state"] == "failed"
        assert not (_ns_root(service) / "results" / "hot_moments.json").exists()

    def test_missing_source_cannot_fabricate_moments(self, service,
                                                     monkeypatch):
        _approved(service)
        # Manifest still references the source but the bytes are gone.
        source = service.source_path()
        source.unlink()
        spy = _AnalysisSpy()
        monkeypatch.setattr(video_analysis, "run_video_analysis", spy)
        with pytest.raises(ArtifactProblem, match="diskte yok"):
            _run(service)
        assert spy.calls == []
        assert not (_ns_root(service) / "results" / "hot_moments.json").exists()

    def test_repeated_analysis_overwrites_artifact(self, service, monkeypatch):
        _approved(service)
        monkeypatch.setattr(video_analysis, "run_video_analysis", _AnalysisSpy())
        _run(service)
        second = _fake_manifest("b")
        second["hot_moments"] = []
        monkeypatch.setattr(video_analysis, "run_video_analysis",
                            _AnalysisSpy(manifest=second))
        result = _run(service)
        assert result["hot_moments"] == []
        assert service.load_artifact("hot_moments")["data"]["hot_moments"] == []

    def test_audit_records_artifact_written(self, service, monkeypatch):
        _approved(service)
        monkeypatch.setattr(video_analysis, "run_video_analysis", _AnalysisSpy())
        _run(service)
        events = service.audit.events()
        assert any(e["event_type"] == "artifact.written"
                   and e["artifact"] == "results/hot_moments.json"
                   and e["job_id"] == "j1"
                   for e in events)


# ---------------------------------------------------------------------------
# Full-source analysis
# ---------------------------------------------------------------------------


class TestFullSourceAnalysis:
    def test_success_writes_manifest_and_reports_regions(self, service,
                                                         monkeypatch):
        _approved(service)
        spy = _FullSourceSpy()
        monkeypatch.setattr(video_analysis, "run_full_source_analysis", spy)
        result = _run(service, analysis_scope="full_source")
        assert result["analysis_scope"] == "full_source"
        assert service.load_artifact("hot_moments")["data"] == result
        call = spy.calls[0]
        assert call["source_duration_sec"] == 180.0
        assert call["ffmpeg_bin"] == "/x/ffmpeg"
        assert call["source"] == service.source_path()
        rec = _progress(service)["video_analysis"]
        assert rec["state"] == "completed"
        assert "3 bolge" in rec["message"]

    def test_failure_propagates_and_no_artifact(self, service, monkeypatch):
        _approved(service)
        spy = _FullSourceSpy(exc=RuntimeError("scan died"))
        monkeypatch.setattr(video_analysis, "run_full_source_analysis", spy)
        with pytest.raises(RuntimeError, match="scan died"):
            _run(service, analysis_scope="full_source")
        assert not (_ns_root(service) / "results" / "hot_moments.json").exists()
        assert _progress(service)["video_analysis"]["state"] == "failed"

    def test_progress_stage_labels(self, service, monkeypatch):
        _approved(service)
        spy = _FullSourceSpy()
        monkeypatch.setattr(video_analysis, "run_full_source_analysis", spy)
        _run(service, analysis_scope="full_source")
        rec = _progress(service)["video_analysis"]
        keys = [s["key"] for s in rec["stages"]]
        assert keys == ["full_scan", "region_analysis", "scoring", "previews"]


# ---------------------------------------------------------------------------
# Isolation / no-unrelated-state
# ---------------------------------------------------------------------------


class TestAnalysisIsolation:
    def test_analysis_writes_only_hot_moments(self, service, monkeypatch):
        _approved(service)
        before = {
            str(p.relative_to(_ns_root(service)))
            for p in _ns_root(service).rglob("*") if p.is_file()
        }
        monkeypatch.setattr(video_analysis, "run_video_analysis", _AnalysisSpy())
        _run(service)
        after = {
            str(p.relative_to(_ns_root(service)))
            for p in _ns_root(service).rglob("*") if p.is_file()
        }
        added = after - before
        assert "results/hot_moments.json" in added
        # progress + audit are expected side effects; nothing else may appear
        assert all(
            f == "results/hot_moments.json" or f.startswith("progress/")
            or f == "audit_events.jsonl"
            for f in added
        )

    def test_analysis_grants_no_approval(self, service, monkeypatch):
        _approved(service)
        granted_before = [
            e for e in service.audit.events()
            if e["event_type"] == "approval.granted"
        ]
        monkeypatch.setattr(video_analysis, "run_video_analysis", _AnalysisSpy())
        _run(service)
        granted_after = [
            e for e in service.audit.events()
            if e["event_type"] == "approval.granted"
        ]
        assert granted_after == granted_before

    def test_stale_selection_invalidated_by_rerun(self, service, monkeypatch):
        """A moment selection built on manifest A must not silently validate
        against manifest B produced by a fresh analysis run."""
        _approved(service)
        monkeypatch.setattr(video_analysis, "run_video_analysis", _AnalysisSpy())
        first = _run(service)
        selection = video_analysis.build_moment_selection(
            first, 1, "reviewer", require_preview=False)
        assert video_analysis.validate_moment_selection(
            selection, first, require_preview=False) == []
        different = _fake_manifest("c")
        different["hot_moments"] = [dict(first["hot_moments"][0], score=0.1)]
        monkeypatch.setattr(video_analysis, "run_video_analysis",
                            _AnalysisSpy(manifest=different))
        second = _run(service)
        errors = video_analysis.validate_moment_selection(
            selection, second, require_preview=False)
        assert any("hot_moments.json degisti" in e for e in errors)


# ---------------------------------------------------------------------------
# Post-extraction parity: facade delegate == standalone service
# ---------------------------------------------------------------------------

from application.services import (  # noqa: E402
    SourceService, VideoAnalysisService, WorkflowGate)
from infrastructure.persistence.local import (  # noqa: E402
    LocalArtifactRepository)
from infrastructure.progress.local import (  # noqa: E402
    LocalProgressFactory)
from application.media_binaries import MediaBinaries  # noqa: E402
from creator_console.ids import new_id, safe_filename  # noqa: E402
from creator_console.audit import AuditLog  # noqa: E402


def _standalone_service(storage, ownership):
    """A VideoAnalysisService built WITHOUT PipelineService.

    Only real ports/adapters -- the same objects the composition root would
    inject -- so the comparison exercises the service, not a fake.
    """
    audit = AuditLog(storage, ownership)
    artifacts = LocalArtifactRepository(storage, ownership, audit)
    progress = LocalProgressFactory(storage, ownership)
    source = SourceService(
        artifacts=artifacts, audit=audit, namespace=storage,
        ownership=ownership, binaries=MediaBinaries("/x/ffmpeg", "/x/ffprobe"),
        safe_filename=safe_filename, new_id=new_id, progress=progress)
    return VideoAnalysisService(
        artifacts=artifacts, namespace=storage, ownership=ownership,
        source=source, binaries=MediaBinaries("/x/ffmpeg", "/x/ffprobe"),
        progress=progress, workflow=WorkflowGate(artifacts=artifacts))


class TestServiceParity:
    def test_standalone_service_produces_identical_result(self, service,
                                                          storage, ownership,
                                                          monkeypatch):
        _approved(service)
        # A fixed manifest (constant source path) keeps the two namespaces'
        # outputs byte-identical; the real difference under test is WHO runs
        # the orchestration, not which project it ran in.
        monkeypatch.setattr(
            video_analysis, "run_video_analysis",
            _AnalysisSpy(manifest=_fake_manifest("shared.mp4")))
        facade_result = _run(service)
        facade_artifact = service.load_artifact("hot_moments")["data"]
        facade_progress = dict(_progress(service)["video_analysis"])
        for volatile in ("started_at", "updated_at", "finished_at",
                         "elapsed_sec"):
            facade_progress.pop(volatile, None)

        # Independent service on a SECOND namespace of the same storage --
        # no PipelineService instance exists in its construction at all.
        ownership2 = Ownership("usr_test", "wsp_test", "prj_test2")
        service2 = PipelineService(
            storage, ownership2, FakeSecrets(),
            probe_fn=lambda *a: (PROBE_INFO, "ffprobe"),
            ffmpeg_bin="/x/ffmpeg", ffprobe_bin="/x/ffprobe")
        _approved(service2)
        direct = _standalone_service(storage, ownership2)

        direct_result = direct.run_analysis(job_id="j1", actor="tester")
        assert direct_result == facade_result
        # Same artifact, same path, same content in the second namespace.
        written = service2.load_artifact("hot_moments")
        assert written["data"] == direct_result == facade_artifact
        # Same progress record modulo timestamps.
        direct_progress = dict(load_progress(storage, ownership2)
                               ["video_analysis"])
        for volatile in ("started_at", "updated_at", "finished_at",
                         "elapsed_sec"):
            direct_progress.pop(volatile, None)
        assert direct_progress == facade_progress

    def test_error_types_match_facade_semantics(self, service, storage,
                                                ownership, monkeypatch):
        _approved(service)
        direct = _standalone_service(storage, ownership)
        # Gate denial: same exception TYPE the facade's require_phase raises.
        (_ns_root(service) / "results" / "processing_plan.json").unlink()
        with pytest.raises(PipelineServiceError):
            direct.run_analysis()

    def test_service_needs_no_facade(self, storage, ownership):
        """Constructor signature takes ports only -- no facade slot."""
        import inspect
        params = set(inspect.signature(
            VideoAnalysisService.__init__).parameters)
        assert params == {
            "self", "artifacts", "namespace", "ownership", "source",
            "binaries", "progress", "workflow",
        }


# ---------------------------------------------------------------------------
# Negative boundary tests
# ---------------------------------------------------------------------------


class TestNegativeBoundaries:
    def test_service_cannot_bypass_workflow_gate(self, storage, ownership,
                                               monkeypatch):
        """A denying gate stops the run before the engine is reached --
        even when every other port is real and the project is valid."""
        spy = _AnalysisSpy()
        monkeypatch.setattr(video_analysis, "run_video_analysis", spy)

        class _DenyAll:
            def require_phase(self, phase):
                raise PipelineServiceError(f"{phase} denied by test gate")

        service = _standalone_service(storage, ownership)
        service._workflow = _DenyAll()
        with pytest.raises(PipelineServiceError, match="denied by test gate"):
            service.run_analysis()
        assert spy.calls == []

    def test_analysis_cannot_publish(self, service, monkeypatch):
        """No publish/upload surface on the service; no provider audit call."""
        _approved(service)
        monkeypatch.setattr(video_analysis, "run_video_analysis",
                            _AnalysisSpy())
        for forbidden in ("publish", "upload", "set_visibility"):
            assert not hasattr(service.video_analysis, forbidden)
        _run(service)
        events = service.audit.events()
        assert not [e for e in events
                    if e["event_type"] in ("provider.call", "publish.uploaded")]

    def test_invalid_timestamps_rejected_at_manifest_build(self):
        """Timestamp-order validation is canonical: a non-positive window can
        never become a hot_moments artifact, window or full-source."""
        moment = {"rank": 1, "aligned_start_sec": 10.0,
                  "aligned_end_sec": 5.0, "source_start_sec": 10.0,
                  "source_end_sec": 40.0}
        with pytest.raises(ValueError, match="pozitif degil"):
            video_analysis.build_hot_moments_manifest(
                [moment], "", 60.0, 30.0, {}, None, None, 0.0, 60.0)

    def test_schema_invalid_hot_moments_rejected_at_selection(self, service):
        """A manifest missing a required moment field fails the canonical
        schema check inside `build_moment_selection` -- it cannot seed a
        selection."""
        _approved(service)
        bad = _fake_manifest("x")
        del bad["hot_moments"][0]["score"]
        service.write_artifact("hot_moments", bad, actor="tester")
        assert video_analysis.validate_hot_moments_schema(bad)
        with pytest.raises(ValueError, match="semasi gecersiz"):
            service.record_moment_selection(
                selected_rank=1, reviewed_by="reviewer",
                require_preview=False)
