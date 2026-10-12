"""Characterization tests for the transcript surface of PipelineService.

Written BEFORE the TranscriptService extraction (Milestone 12). They pin the
CURRENT behaviour of `run_transcript`, `transcript_segments`,
`transcript_view`, `save_transcript_edits`, `set_transcript_exclusions`,
`restore_transcript_from_asr` and `set_transcript_corrections` using the
public facade, a synthetic-but-valid `moment_transcript` document produced by
the canonical builder, and the real gate/repository/audit/progress stack.

The ASR engine (`render_utils.run_moment_transcription`) is stubbed for the
orchestration tests -- generation plumbing is what the facade owns; the
engine itself is exercised by the real-ASR integration check.
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
from creator_console.progress import OperationCancelled
from creator_console.storage import LocalFilesystemStorage
from local_pipeline import render_utils, transcript_edit, video_analysis

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


def _hot_manifest(source_path, ranks=(1, 2, 3)) -> dict:
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


def _ns_root(service):
    return service.storage.namespace_root(service.ownership)


def _seed(service, *, output_count=1, approve=True, mode="direct_clip_series"):
    service.create_project(title="Kapi", created_by="tester")
    service.register_source_video(filename="src.mp4", payload=SOURCE_BYTES,
                                  registered_by="tester")
    _make_plan(service, mode=mode, output_count=output_count)
    _clear_rights(service)
    if approve:
        _approve_plan(service)
    service.write_artifact("hot_moments", _hot_manifest("src"), actor="tester")


def _select(service, rank=1, slot=1):
    return service.record_moment_selection(
        selected_rank=rank, reviewed_by="rev", require_preview=False,
        slot=slot)


def _words():
    return [
        {"text": "Merhaba", "start": 10.0, "end": 10.4, "probability": 0.9},
        {"text": "dunya", "start": 10.4, "end": 10.8, "probability": 0.4},
        {"text": "nasil", "start": 11.0, "end": 11.3, "probability": 0.95},
        {"text": "gidiyor", "start": 11.3, "end": 11.8, "probability": 0.9},
    ]


def _cues():
    return [
        {"id": "seg_0001", "text": "Merhaba dunya", "start": 10.0,
         "end": 10.8},
        {"id": "seg_0002", "text": "nasil gidiyor", "start": 11.0,
         "end": 11.8},
    ]


def _transcript_doc(*, rank=1, words=None, cues=None, corrections=None,
                    revisions=None, edited_segment_ids=None):
    return render_utils.build_moment_transcript_artifact(
        words if words is not None else _words(),
        cues if cues is not None else _cues(),
        source="src", language="tr", moment_rank=rank,
        moment_selection_sha256="selsha", whisper_model_size="small",
        initial_prompt_used=False, corrections=corrections,
        revisions=revisions, edited_segment_ids=edited_segment_ids)


def _write_transcript(service, slot=1, **kwargs):
    doc = _transcript_doc(**kwargs)
    service.write_artifact("moment_transcript", doc, actor="tester", slot=slot)
    return doc


def _engine_stub(result=None, error=None, validate=False):
    """Replace the canonical ASR engine for orchestration tests."""
    calls = []

    def stub(hot, selection, runtime_dir, **kwargs):
        calls.append({"hot": hot, "selection": selection,
                      "runtime_dir": str(runtime_dir), "kwargs": kwargs})
        if validate:
            errors = video_analysis.validate_moment_selection(
                selection, hot, require_preview=False)
            if errors:
                raise ValueError(f"Moment secimi gecersiz: {errors}")
        if error is not None:
            raise error
        return result if result is not None else _transcript_doc(
            rank=selection.get("selected_moment_rank") or 1)

    return stub, calls


# ---------------------------------------------------------------------------
# run_transcript
# ---------------------------------------------------------------------------


class TestRunTranscript:
    def test_unapproved_plan_denies_transcript(self, service):
        _seed(service, approve=False)
        _select(service) if False else None  # selection would fail anyway
        with pytest.raises(PipelineServiceError):
            service.run_transcript()

    def test_narrated_mode_denies_transcript(self, storage, ownership):
        service = PipelineService(
            storage, ownership, FakeSecrets(configured=True),
            probe_fn=lambda *a: (PROBE_INFO, "ffprobe"),
            ffmpeg_bin="/x/ffmpeg", ffprobe_bin="/x/ffprobe")
        _seed(service, mode="narrated_theory")
        _select(service)
        with pytest.raises(PipelineServiceError):
            service.run_transcript()

    def test_missing_selection_blocks_transcript(self, service,
                                                 monkeypatch):
        _seed(service)
        stub, calls = _engine_stub()
        monkeypatch.setattr(render_utils, "run_moment_transcription", stub)
        with pytest.raises(ArtifactProblem):
            service.run_transcript()
        assert calls == []

    def test_success_writes_artifact_progress_and_audit(self, service,
                                                        monkeypatch):
        _seed(service)
        _select(service)
        stub, calls = _engine_stub()
        monkeypatch.setattr(render_utils, "run_moment_transcription", stub)
        result = service.run_transcript(actor="op")
        assert result["artifact_type"] == "moment_transcript"
        assert calls and calls[0]["kwargs"]["progress"] is not None
        loaded = service.load_artifact("moment_transcript", slot=1)
        assert loaded["data"]["has_speech"] is True
        assert service.artifact_relative("moment_transcript", 1) == \
            "results/moment_transcript.json"
        progress = json.loads(
            (_ns_root(service) / "progress" / "transcript.json")
            .read_text())
        assert progress["state"] == "completed"
        assert any(e["event_type"] == "artifact.written"
                   and e["artifact"] == "results/moment_transcript.json"
                   and e["actor"] == "op" for e in service.audit.events())

    def test_engine_failure_fails_tracker_and_writes_nothing(self, service,
                                                             monkeypatch):
        _seed(service)
        _select(service)
        stub, _calls = _engine_stub(error=RuntimeError("asr boom"))
        monkeypatch.setattr(render_utils, "run_moment_transcription", stub)
        with pytest.raises(RuntimeError, match="asr boom"):
            service.run_transcript()
        assert service.load_artifact("moment_transcript")["data"] is None
        progress = json.loads(
            (_ns_root(service) / "progress" / "transcript.json")
            .read_text())
        assert progress["state"] == "failed"

    def test_cancellation_propagates_and_fails_tracker(self, service,
                                                       monkeypatch):
        _seed(service)
        _select(service)
        stub, _calls = _engine_stub(
            error=OperationCancelled("is kullanici tarafindan durduruldu"))
        monkeypatch.setattr(render_utils, "run_moment_transcription", stub)
        with pytest.raises(OperationCancelled):
            service.run_transcript()
        assert service.load_artifact("moment_transcript")["data"] is None
        progress = json.loads(
            (_ns_root(service) / "progress" / "transcript.json")
            .read_text())
        assert progress["state"] == "failed"

    def test_slot2_writes_per_slot_path(self, service, monkeypatch):
        _seed(service, output_count=2)
        _select(service, rank=1, slot=1)
        _select(service, rank=2, slot=2)
        stub, _calls = _engine_stub()
        monkeypatch.setattr(render_utils, "run_moment_transcription", stub)
        service.run_transcript(slot=2)
        assert service.artifact_relative("moment_transcript", 2) == \
            "results/render/output_02/moment_transcript.json"
        assert service.load_artifact("moment_transcript", slot=2)["data"]
        assert service.load_artifact("moment_transcript", slot=1)["data"] \
            is None

    def test_rerun_carries_word_corrections(self, service, monkeypatch):
        _seed(service)
        _select(service)
        _write_transcript(service)
        service.set_transcript_corrections(
            corrections=[{"from": "dunya", "to": "Dunya"}], edited_by="ed")
        stub, _calls = _engine_stub()
        monkeypatch.setattr(render_utils, "run_moment_transcription", stub)
        result = service.run_transcript()
        assert result["corrections"][0]["from"] == "dunya"
        assert result["corrections"][0]["to"] == "Dunya"
        assert "Dunya" in result["text"]

    def test_rerun_archives_manually_edited_transcript(self, service,
                                                       monkeypatch):
        _seed(service)
        _select(service)
        _write_transcript(service)
        segments = service.transcript_segments()
        service.save_transcript_edits(
            segments=[{"id": segments[0]["id"], "text": "Merhaba insanlar"}],
            edited_by="ed")
        stub, _calls = _engine_stub()
        monkeypatch.setattr(render_utils, "run_moment_transcription", stub)
        result = service.run_transcript()
        replaced = result.get("replaced_manual_edit")
        assert replaced and replaced["revision_id"]
        # `.revisions/` is namespace-normalised to `revisions/` on disk.
        revisions_root = _ns_root(service) / "revisions" \
            / replaced["revision_id"]
        assert (revisions_root / "results" / "moment_transcript.json") \
            .is_file()
        assert any(e["event_type"] == "transcript.manual_edits_archived"
                   for e in service.audit.events())

    def test_stale_selection_cannot_produce_transcript(self, service,
                                                       monkeypatch):
        _seed(service)
        _select(service)
        changed = _hot_manifest("src")
        changed["hot_moments"][0]["score"] = 0.01
        service.write_artifact("hot_moments", changed, actor="tester")
        stub, calls = _engine_stub(validate=True)
        monkeypatch.setattr(render_utils, "run_moment_transcription", stub)
        with pytest.raises(ValueError, match="gecersiz"):
            service.run_transcript()
        assert service.load_artifact("moment_transcript")["data"] is None


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


class TestTranscriptReads:
    def test_transcript_segments_returns_rows(self, service):
        _seed(service)
        _write_transcript(service)
        segments = service.transcript_segments()
        assert [s["id"] for s in segments] == ["seg_0001", "seg_0002"]
        assert segments[0]["text"] == "Merhaba dunya"
        assert segments[0]["low_confidence_count"] == 1
        assert segments[0]["low_confidence_words"][0]["text"] == "dunya"

    def test_transcript_view_shape(self, service):
        _seed(service)
        _select(service)
        _write_transcript(service)
        view = service.transcript_view(1)
        assert view["available"] is True
        assert view["phase_allowed"] is True
        assert view["moment_rank"] == 1
        assert view["language"] == "tr"
        assert len(view["segments"]) == 2
        assert view["full_text"]
        assert view["limits"]["max_segment_chars"] > 0
        assert view["approval"]["kind"] == "transcript"
        assert view["approval"]["approved"] is False

    def test_transcript_view_unavailable_without_artifact(self, service):
        _seed(service)
        view = service.transcript_view(1)
        assert view["available"] is False
        assert view["segments"] == []

    def test_transcript_segments_requires_artifact(self, service):
        _seed(service)
        with pytest.raises(ArtifactProblem):
            service.transcript_segments(1)

    def test_read_queries_do_not_write(self, service):
        _seed(service)
        _select(service)
        _write_transcript(service)
        before_events = len(service.audit.events())
        before_files = {
            str(p.relative_to(_ns_root(service)))
            for p in _ns_root(service).rglob("*") if p.is_file()}
        service.transcript_view(1)
        service.transcript_segments(1)
        after_files = {
            str(p.relative_to(_ns_root(service)))
            for p in _ns_root(service).rglob("*") if p.is_file()}
        assert after_files == before_files
        assert len(service.audit.events()) == before_events


# ---------------------------------------------------------------------------
# edits
# ---------------------------------------------------------------------------


class TestTranscriptEdits:
    def _ready(self, service):
        _seed(service)
        _select(service)
        _write_transcript(service)

    def test_segment_edit_writes_revision(self, service):
        self._ready(service)
        segments = service.transcript_segments()
        view = service.save_transcript_edits(
            segments=[{"id": segments[0]["id"], "text": "Selam dunya"}],
            edited_by="ed")
        doc = service.load_artifact("moment_transcript")["data"]
        assert doc["revision"] == 1
        assert doc["edited_segment_ids"] == ["seg_0001"]
        assert doc["revisions"][0]["kind"] == "segment_edit"
        assert "Selam" in doc["text"]
        assert view["available"] is True
        assert any(e["event_type"] == "artifact.written"
                   and e["detail"].get("edit_kind") == "segment"
                   for e in service.audit.events())

    def test_full_text_edit(self, service):
        self._ready(service)
        view = service.save_transcript_edits(
            full_text="Merhaba insanlar\nnasil gidiyor", edited_by="ed")
        doc = service.load_artifact("moment_transcript")["data"]
        assert doc["revisions"][0]["kind"] == "full_text_edit"
        assert "insanlar" in doc["text"]

    def test_exactly_one_of_segments_or_full_text(self, service):
        self._ready(service)
        with pytest.raises(PipelineServiceError, match="ikisi birden degil"):
            service.save_transcript_edits(edited_by="ed")
        with pytest.raises(PipelineServiceError, match="ikisi birden degil"):
            service.save_transcript_edits(
                segments=[], full_text="x", edited_by="ed")

    def test_missing_segment_id_rejected(self, service):
        self._ready(service)
        with pytest.raises(PipelineServiceError, match="segment id"):
            service.save_transcript_edits(
                segments=[{"text": "no id"}], edited_by="ed")

    def test_unknown_segment_id_raises(self, service):
        self._ready(service)
        with pytest.raises(Exception):
            service.save_transcript_edits(
                segments=[{"id": "seg_9999", "text": "yok"}], edited_by="ed")

    def test_unchanged_edit_is_noop(self, service):
        self._ready(service)
        segments = service.transcript_segments()
        before = service.load_artifact("moment_transcript")["data"]
        events_before = len(service.audit.events())
        view = service.save_transcript_edits(
            segments=[{"id": segments[0]["id"],
                       "text": segments[0]["text"]}],
            edited_by="ed")
        doc = service.load_artifact("moment_transcript")["data"]
        assert doc.get("revision", 0) == before.get("revision", 0)
        assert not doc.get("revisions")
        assert len(service.audit.events()) == events_before
        assert view["available"] is True

    def test_exclusion_marks_segment(self, service):
        self._ready(service)
        view = service.set_transcript_exclusions(
            segment_ids=["seg_0001"], excluded=True, actor="ed")
        doc = service.load_artifact("moment_transcript")["data"]
        assert doc["cues"][0].get("excluded_from_subtitles") is True
        assert doc["revisions"][0]["kind"] == "subtitle_exclusion"
        assert view["excluded_cue_count"] == 1

    def test_restore_asr_reverts_edit(self, service):
        self._ready(service)
        segments = service.transcript_segments()
        service.save_transcript_edits(
            segments=[{"id": segments[0]["id"], "text": "Selam dunya"}],
            edited_by="ed")
        view = service.restore_transcript_from_asr(actor="ed")
        doc = service.load_artifact("moment_transcript")["data"]
        assert "Merhaba dunya" in doc["text"]
        assert doc["revisions"][-1]["kind"] == "restore_asr"
        assert doc["edited_segment_ids"] == []

    def test_set_corrections_rebuilds_text(self, service):
        self._ready(service)
        artifact = service.set_transcript_corrections(
            corrections=[{"from": "dunya", "to": "Dunya"}], edited_by="ed")
        assert artifact["corrections"][0]["from"] == "dunya"
        assert artifact["corrections"][0]["to"] == "Dunya"
        assert "Dunya" in artifact["text"]
        assert artifact["revisions"][-1]["kind"] == "corrections"

    def test_corrections_limit_enforced(self, service):
        self._ready(service)
        with pytest.raises(PipelineServiceError, match="100"):
            service.set_transcript_corrections(
                corrections=[{"from": f"a{i}", "to": "b"}
                             for i in range(101)],
                edited_by="ed")

    def test_edit_drops_transcript_approval(self, service):
        self._ready(service)
        digest = service.approval_state("transcript")["content_sha256"]
        service.approve("transcript", reviewed_sha256=digest,
                        approved_by="rev")
        assert service.approval_state("transcript")["valid"] is True
        segments = service.transcript_segments()
        service.save_transcript_edits(
            segments=[{"id": segments[0]["id"], "text": "Selam dunya"}],
            edited_by="ed")
        assert service.approval_state("transcript")["valid"] is False
        assert service.approval_state("transcript")["approved"] is False

    def test_edit_slot1_leaves_slot2_untouched(self, service):
        _seed(service, output_count=2)
        _select(service, rank=1, slot=1)
        _select(service, rank=2, slot=2)
        _write_transcript(service, slot=1)
        _write_transcript(service, slot=2, rank=2)
        segments = service.transcript_segments(1)
        service.save_transcript_edits(
            segments=[{"id": segments[0]["id"], "text": "Selam dunya"}],
            edited_by="ed", slot=1)
        doc2 = service.load_artifact("moment_transcript", slot=2)["data"]
        assert not doc2.get("revisions")
        assert "Merhaba dunya" in doc2["text"]

    def test_edit_never_grants_approval_or_downstream(self, service):
        self._ready(service)
        segments = service.transcript_segments()
        service.save_transcript_edits(
            segments=[{"id": segments[0]["id"], "text": "Selam dunya"}],
            edited_by="ed")
        for key in ("selected_script", "selected_voiceover",
                    "render_manifest", "publish_intent"):
            assert service.load_artifact(key)["data"] is None, key
        events_before = len(service.audit.events())
        segments = service.transcript_segments()
        service.save_transcript_edits(
            segments=[{"id": segments[0]["id"], "text": "Yine selam"}],
            edited_by="ed")
        events_now = service.audit.events()
        new_events = events_now[:len(events_now) - events_before]
        assert not any(e["event_type"] in {"provider.call", "job.submitted",
                                           "approval.granted"}
                       for e in new_events)


# ---------------------------------------------------------------------------
# parity: standalone TranscriptService vs the facade delegates
# ---------------------------------------------------------------------------

from creator_console.audit import AuditLog  # noqa: E402
from creator_console.ids import new_id, safe_filename  # noqa: E402
from application.media_binaries import MediaBinaries  # noqa: E402
from application.services import (ApprovalService, SourceService,  # noqa: E402
                                  TranscriptService, WorkflowGate)
from infrastructure.persistence.local import (  # noqa: E402
    LocalArtifactRepository, LocalRevisionArchive)
from infrastructure.progress.local import LocalProgressFactory  # noqa: E402
from infrastructure.review.local import LocalReviewGate  # noqa: E402


def _standalone(storage, ownership):
    """A TranscriptService built from real adapters -- no facade anywhere."""
    audit = AuditLog(storage, ownership)
    artifacts = LocalArtifactRepository(storage, ownership, audit)
    progress = LocalProgressFactory(storage, ownership)
    source = SourceService(
        artifacts=artifacts, audit=audit, namespace=storage,
        ownership=ownership,
        binaries=MediaBinaries("/x/ffmpeg", "/x/ffprobe"),
        safe_filename=safe_filename, new_id=new_id, progress=progress)
    workflow = WorkflowGate(artifacts=artifacts)
    approval = ApprovalService(
        artifacts=artifacts, audit=audit, ownership=ownership,
        review_gate=LocalReviewGate(artifacts=artifacts, ownership=ownership,
                                    source=source))
    return TranscriptService(
        artifacts=artifacts, source=source, workflow=workflow,
        approvals=approval,
        archiver=LocalRevisionArchive(namespace=storage, ownership=ownership),
        progress=progress, namespace=storage, ownership=ownership,
        audit=audit)


def _ready_pair(tmp_path):
    """Two identically-seeded namespaces: one facade-driven, one standalone."""
    storage_a = LocalFilesystemStorage(tmp_path / "a" / "console_root")
    storage_b = LocalFilesystemStorage(tmp_path / "b" / "console_root")
    own = Ownership("usr_test", "wsp_test", "prj_test")
    facade = PipelineService(storage_a, own, FakeSecrets(),
                             probe_fn=lambda *a: (PROBE_INFO, "ffprobe"),
                             ffmpeg_bin="/x/ffmpeg", ffprobe_bin="/x/ffprobe")
    seeder = PipelineService(storage_b, own, FakeSecrets(),
                             probe_fn=lambda *a: (PROBE_INFO, "ffprobe"),
                             ffmpeg_bin="/x/ffmpeg", ffprobe_bin="/x/ffprobe")
    _seed(facade)
    _select(facade)
    _write_transcript(facade)
    _seed(seeder)
    _select(seeder)
    _write_transcript(seeder)
    return facade, seeder, _standalone(storage_b, own)


def _normalise(value):
    """Strip the only legitimately-nondeterministic field: revision stamps."""
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items()
                if k not in {"at", "recorded_at", "suggested_at",
                             "captured_at", "created_at"}}
    if isinstance(value, list):
        return [_normalise(v) for v in value]
    return value


class TestTranscriptParity:
    def test_view_and_segments_identical(self, tmp_path):
        facade, _seeder, standalone = _ready_pair(tmp_path)
        for slot in (1, 2):
            assert facade.transcript_view(slot) == standalone.transcript_view(slot)
            assert facade.transcript_segments(1) == \
                standalone.transcript_segments(1)
            break

    def test_segment_edit_parity(self, tmp_path):
        facade, _seeder, standalone = _ready_pair(tmp_path)
        seg_id = facade.transcript_segments(1)[0]["id"]
        facade_view = facade.save_transcript_edits(
            segments=[{"id": seg_id, "text": "Selam dunya"}], edited_by="ed")
        solo_view = standalone.save_transcript_edits(
            segments=[{"id": seg_id, "text": "Selam dunya"}], edited_by="ed")
        assert _normalise(facade_view) == _normalise(solo_view)
        facade_doc = facade.load_artifact("moment_transcript")["data"]
        solo_doc = standalone._artifacts.load_artifact(
            "moment_transcript")["data"]
        assert _normalise(facade_doc) == _normalise(solo_doc)
        facade_events = [(e["event_type"], e["actor"], e["artifact"],
                          e["detail"]) for e in facade.audit.events()
                         if e["event_type"] == "artifact.written"]
        solo_events = [(e["event_type"], e["actor"], e["artifact"],
                        e["detail"]) for e in standalone._audit.events()
                       if e["event_type"] == "artifact.written"]
        assert facade_events[-2:] == solo_events[-2:]

    def test_corrections_parity(self, tmp_path):
        facade, _seeder, standalone = _ready_pair(tmp_path)
        corrections = [{"from": "dunya", "to": "Dunya"}]
        facade_art = facade.set_transcript_corrections(
            corrections=corrections, edited_by="ed")
        solo_art = standalone.set_transcript_corrections(
            corrections=corrections, edited_by="ed")
        assert _normalise(facade_art) == _normalise(solo_art)

    def test_exclusion_and_restore_parity(self, tmp_path):
        facade, _seeder, standalone = _ready_pair(tmp_path)
        facade_v = facade.set_transcript_exclusions(
            segment_ids=["seg_0001"], excluded=True, actor="ed")
        solo_v = standalone.set_transcript_exclusions(
            segment_ids=["seg_0001"], excluded=True, actor="ed")
        assert _normalise(facade_v) == _normalise(solo_v)
        facade_v = facade.restore_transcript_from_asr(actor="ed")
        solo_v = standalone.restore_transcript_from_asr(actor="ed")
        assert _normalise(facade_v) == _normalise(solo_v)

    def test_run_transcript_parity(self, tmp_path, monkeypatch):
        facade, _seeder, standalone = _ready_pair(tmp_path)
        stub, _calls = _engine_stub()
        monkeypatch.setattr(render_utils, "run_moment_transcription", stub)
        facade_art = facade.run_transcript(actor="op")
        solo_art = standalone.run_transcript(actor="op")
        assert _normalise(facade_art) == _normalise(solo_art)
        assert _normalise(
            facade.load_artifact("moment_transcript")["data"]) == _normalise(
            standalone._artifacts.load_artifact("moment_transcript")["data"])

    def test_exception_parity(self, tmp_path):
        facade, _seeder, standalone = _ready_pair(tmp_path)
        for call in (
                lambda s: s.save_transcript_edits(edited_by="ed"),
                lambda s: s.save_transcript_edits(
                    segments=[{"text": "x"}], edited_by="ed"),
                lambda s: s.set_transcript_corrections(
                    corrections=[{"from": "a" * 81, "to": "b"}],
                    edited_by="ed")):
            with pytest.raises(Exception) as facade_exc:
                call(facade)
            with pytest.raises(Exception) as solo_exc:
                call(standalone)
            assert type(facade_exc.value) is type(solo_exc.value)
            assert str(facade_exc.value) == str(solo_exc.value)


# ---------------------------------------------------------------------------
# explicit negatives
# ---------------------------------------------------------------------------


class TestTranscriptNegatives:
    def test_standalone_service_needs_no_facade(self, tmp_path):
        storage = LocalFilesystemStorage(tmp_path / "console_root")
        own = Ownership("usr_test", "wsp_test", "prj_test")
        standalone = _standalone(storage, own)
        view = standalone.transcript_view(1)
        assert view["available"] is False
        assert view["phase_allowed"] is False

    def test_failed_write_reports_no_success(self, service, monkeypatch):
        from creator_console.faults import disk_full, patch_atomic_write
        _seed(service)
        _select(service)
        stub, _calls = _engine_stub()
        monkeypatch.setattr(render_utils, "run_moment_transcription", stub)
        events_before = len(service.audit.events())
        with patch_atomic_write(exception=disk_full("moment_transcript.json"),
                                match="moment_transcript.json"):
            with pytest.raises(OSError):
                service.run_transcript(actor="op")
        assert service.load_artifact("moment_transcript")["data"] is None
        # `events()` is newest-first: the operation's events are at the FRONT.
        events_now = service.audit.events()
        new_events = events_now[:len(events_now) - events_before]
        assert not any(e["event_type"] == "artifact.written"
                       and "moment_transcript" in str(e.get("artifact"))
                       for e in new_events)
        assert service.approval_state("transcript")["approved"] is False
        progress = json.loads(
            (_ns_root(service) / "progress" / "transcript.json").read_text())
        assert progress["state"] == "failed"

    def test_permission_denied_write_reports_no_success(self, service,
                                                        monkeypatch):
        from creator_console.faults import (patch_atomic_write,
                                            permission_denied)
        _seed(service)
        _select(service)
        stub, _calls = _engine_stub()
        monkeypatch.setattr(render_utils, "run_moment_transcription", stub)
        with patch_atomic_write(
                exception=permission_denied("moment_transcript.json"),
                match="moment_transcript.json"):
            with pytest.raises(PermissionError):
                service.run_transcript(actor="op")
        assert service.load_artifact("moment_transcript")["data"] is None
        assert service.approval_state("transcript")["approved"] is False

    def test_partial_write_never_reports_success(self, service, monkeypatch):
        from creator_console.faults import InterruptedWrite, patch_atomic_write
        _seed(service)
        _select(service)
        stub, _calls = _engine_stub()
        monkeypatch.setattr(render_utils, "run_moment_transcription", stub)
        with patch_atomic_write(exception=InterruptedWrite("kesildi"),
                                partial_bytes=48,
                                match="moment_transcript.json"):
            with pytest.raises(InterruptedWrite):
                service.run_transcript(actor="op")
        loaded = service.load_artifact("moment_transcript")
        # The fragment must be REJECTED by the reader, never parsed as content.
        assert loaded["data"] is None
        assert loaded["problem"]
        assert service.approval_state("transcript")["approved"] is False

    def test_failed_edit_write_reports_no_success(self, service, monkeypatch):
        from creator_console.faults import disk_full, patch_atomic_write
        _seed(service)
        _select(service)
        _write_transcript(service)
        segments = service.transcript_segments()
        with patch_atomic_write(exception=disk_full("moment_transcript.json"),
                                match="moment_transcript.json"):
            with pytest.raises(OSError):
                service.save_transcript_edits(
                    segments=[{"id": segments[0]["id"], "text": "Selam"}],
                    edited_by="ed")
        doc = service.load_artifact("moment_transcript")["data"]
        assert not doc.get("revisions")
        assert "Selam" not in doc["text"]

    def test_missing_selection_fabricates_nothing(self, service, monkeypatch):
        _seed(service)
        stub, calls = _engine_stub()
        monkeypatch.setattr(render_utils, "run_moment_transcription", stub)
        with pytest.raises(ArtifactProblem):
            service.run_transcript()
        assert calls == []
        assert service.load_artifact("moment_transcript")["data"] is None

    def test_run_transcript_never_triggers_downstream(self, service,
                                                      monkeypatch):
        _seed(service)
        _select(service)
        stub, _calls = _engine_stub()
        monkeypatch.setattr(render_utils, "run_moment_transcription", stub)
        events_before = len(service.audit.events())
        service.run_transcript()
        for key in ("selected_script", "selected_voiceover",
                    "render_manifest", "publish_intent"):
            assert service.load_artifact(key)["data"] is None, key
        # `events()` is newest-first: the operation's events are at the FRONT.
        events_now = service.audit.events()
        new_events = events_now[:len(events_now) - events_before]
        assert not any(e["event_type"] in
                       {"provider.call", "job.submitted", "approval.granted"}
                       for e in new_events)

    def test_transcript_generation_grants_no_approval(self, service,
                                                      monkeypatch):
        _seed(service)
        _select(service)
        stub, _calls = _engine_stub()
        monkeypatch.setattr(render_utils, "run_moment_transcription", stub)
        service.run_transcript()
        state = service.approval_state("transcript")
        assert state["approved"] is False
        assert service.load_artifact("moment_transcript")["data"]["status"] \
            == "review_required"

    def test_slot_edit_isolation_for_exclusions_and_corrections(self, service):
        _seed(service, output_count=2)
        _select(service, rank=1, slot=1)
        _select(service, rank=2, slot=2)
        _write_transcript(service, slot=1)
        _write_transcript(service, slot=2, rank=2)
        service.set_transcript_exclusions(
            segment_ids=["seg_0001"], excluded=True, actor="ed", slot=1)
        service.set_transcript_corrections(
            corrections=[{"from": "dunya", "to": "Dunya"}],
            edited_by="ed", slot=1)
        doc2 = service.load_artifact("moment_transcript", slot=2)["data"]
        assert not doc2.get("revisions")
        assert not doc2["cues"][0].get("excluded_from_subtitles")
        assert "Dunya" not in doc2["text"]
