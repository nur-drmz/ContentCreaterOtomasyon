"""Characterization tests for the script surface of PipelineService.

Written BEFORE the ScriptService extraction (Milestone 13). They pin the
CURRENT behaviour of `run_script_generation` and `update_script_text` using
the public facade, a real moment selection bound to a real preview file
(script generation validates with `require_preview=True`), the real
gate/repository/audit/progress stack and the canonical provider doubles from
`creator_console.fixtures` — which also count provider calls, so the
one-paid-call cost contract is asserted rather than assumed.

No network: every client is a fixture double injected through the existing
`*_client_factory` seams.
"""
from __future__ import annotations

import json

import pytest

from creator_console import fixtures
from creator_console.ids import Ownership
from creator_console.pipeline_service import (
    ArtifactProblem,
    PipelineService,
    PipelineServiceError,
)
from creator_console.progress import OperationCancelled
from creator_console.storage import LocalFilesystemStorage
from local_pipeline import script_generation, video_analysis

SOURCE_BYTES = b"\x00\x01synthetic-source-bytes" * 512
PREVIEW_BYTES = b"\x00\x00preview-bytes" * 64

PROBE_INFO = {
    "streams": [{"codec_type": "video", "width": 1920, "height": 1080},
                {"codec_type": "audio"}],
    "format": {"duration": "180.0"},
}

THEORY_ANGLE = "Bu sahnedeki karar aslinda bir tuzaktan ibaret."


class FakeSecrets:
    """`configured` drives the facade's own credential branch."""

    def __init__(self, configured=True):
        self._configured = configured

    def reference(self, kind):
        configured = self._configured
        return type("Ref", (), {"configured": configured})()

    def credential(self, kind):
        configured = self._configured
        return type("Cred", (), {"configured": configured,
                                 "value": "k" * 20 if configured else None})()


@pytest.fixture
def ownership():
    return Ownership("usr_test", "wsp_test", "prj_test")


@pytest.fixture
def storage(tmp_path):
    return LocalFilesystemStorage(tmp_path / "console_root")


def _service(storage, ownership, *, secrets=None, google=True,
             openai=False, anthropic=False):
    kwargs = {}
    if google:
        kwargs["google_client_factory"] = lambda: fixtures.fake_google_client()
    if openai:
        kwargs["openai_client_factory"] = fixtures.fake_openai_client
    if anthropic:
        kwargs["anthropic_client_factory"] = fixtures.fake_anthropic_client
    return PipelineService(
        storage, ownership, secrets or FakeSecrets(),
        probe_fn=lambda *a: (PROBE_INFO, "ffprobe"),
        ffmpeg_bin="/x/ffmpeg", ffprobe_bin="/x/ffprobe", **kwargs)


@pytest.fixture
def service(storage, ownership):
    return _service(storage, ownership)


def _ns_root(service):
    return service.storage.namespace_root(service.ownership)


def _hot_manifest(service, ranks=(1, 2, 3)) -> dict:
    """A schema-valid manifest whose selected moment has a REAL preview file."""
    previews = _ns_root(service) / "results" / "moment_previews"
    previews.mkdir(parents=True, exist_ok=True)
    moments = []
    for rank in ranks:
        preview = previews / f"moment_{rank:02d}.mp4"
        preview.write_bytes(PREVIEW_BYTES + bytes([rank]))
        moments.append({
            "rank": rank,
            "source_start_sec": 10.0 * rank,
            "source_end_sec": 10.0 * rank + 20.0,
            "aligned_start_sec": 10.0 * rank,
            "aligned_end_sec": 10.0 * rank + 20.0,
            "score": 1.0 - 0.1 * rank,
            "reason": f"candidate {rank}",
            "transcript": f"metin {rank}",
            "preview_path": str(preview),
        })
    return {
        "method": "local-multisignal-v2",
        "status": "review_required",
        "source": {"raw_video_path": "src", "clip_start_sec": 0.0,
                   "clip_duration_sec": 180.0},
        "hot_moments": moments,
    }


def _make_plan(service, *, mode="narrated_theory", output_count=1,
               content_language=None, target_duration_sec=None,
               theory_angle=THEORY_ANGLE, theory_angle_auto=False):
    service.run_source_assessment(actor="tester")
    kwargs = {"mode": mode, "selected_by": "tester", "output_count": output_count}
    if mode == "narrated_theory":
        kwargs["theory_angle"] = theory_angle
        kwargs["theory_angle_auto"] = theory_angle_auto
    if content_language is not None:
        kwargs["content_language"] = content_language
    if target_duration_sec is not None:
        kwargs["target_duration_sec"] = target_duration_sec
    return service.create_processing_plan(**kwargs)


def _clear_rights(service):
    for item in service.copyright_review_state()["items"]:
        service.set_copyright_review_item(item["key"], checked=True,
                                           reviewed_by="reviewer")


def _approve_plan(service):
    digest = service.approval_state("processing_plan")["content_sha256"]
    service.approve("processing_plan", reviewed_sha256=digest,
                    approved_by="reviewer")


def _seed(service, *, approve=True, mode="narrated_theory", **plan_kwargs):
    service.create_project(title="Kapi", created_by="tester")
    service.register_source_video(filename="src.mp4", payload=SOURCE_BYTES,
                                  registered_by="tester")
    _make_plan(service, mode=mode, **plan_kwargs)
    _clear_rights(service)
    if approve:
        _approve_plan(service)
    service.write_artifact("hot_moments", _hot_manifest(service), actor="tester")


def _select(service, rank=1, slot=1):
    """A selection bound to a real preview file (require_preview=True)."""
    return service.record_moment_selection(
        selected_rank=rank, reviewed_by="rev", slot=slot)


def _ready(service, **kwargs):
    _seed(service, **kwargs)
    _select(service)


def _progress(service, activity="script_generation"):
    return json.loads(
        (_ns_root(service) / "progress" / f"{activity}.json").read_text())


# ---------------------------------------------------------------------------
# run_script_generation
# ---------------------------------------------------------------------------


class TestRunScriptGeneration:
    def test_narrated_success_writes_artifact(self, service):
        _ready(service)
        fixtures.PROVIDER_CALLS.clear()
        result = service.run_script_generation(actor="op")
        assert result["status"] == "review_required"
        assert result["script_text"]
        assert result["provider"] == "google"
        assert result["human_approval"]["approved"] is False
        stored = service.load_artifact("selected_script")["data"]
        assert stored["script_text"] == result["script_text"]
        assert service.artifact_relative("selected_script") == \
            "results/selected_script.json"

    def test_exactly_one_paid_call_per_request(self, service):
        _ready(service)
        fixtures.PROVIDER_CALLS.clear()
        service.run_script_generation()
        generate = [c for c in fixtures.PROVIDER_CALLS
                    if c[0] == "google.generate_content"]
        # The cost contract: fallback is disabled, so one request is one call.
        assert len(generate) == 1, fixtures.PROVIDER_CALLS

    def test_direct_mode_denies_script_generation(self, storage, ownership):
        service = _service(storage, ownership)
        _seed(service, mode="direct_clip_series")
        _select(service)
        with pytest.raises(PipelineServiceError):
            service.run_script_generation()
        assert service.load_artifact("selected_script")["data"] is None

    def test_unapproved_plan_denies_script_generation(self, service):
        _seed(service, approve=False)
        with pytest.raises(PipelineServiceError):
            service.run_script_generation()

    def test_missing_hot_moments_fabricates_nothing(self, service):
        _seed(service)
        _select(service)
        (_ns_root(service) / "results" / "hot_moments.json").unlink()
        with pytest.raises(ArtifactProblem):
            service.run_script_generation()
        assert service.load_artifact("selected_script")["data"] is None

    def test_missing_moment_selection_fabricates_nothing(self, service):
        _seed(service)
        with pytest.raises(ArtifactProblem):
            service.run_script_generation()
        assert service.load_artifact("selected_script")["data"] is None

    def test_stale_selection_is_rejected(self, service):
        _ready(service)
        changed = _hot_manifest(service)
        changed["hot_moments"][0]["score"] = 0.01
        service.write_artifact("hot_moments", changed, actor="tester")
        with pytest.raises(PipelineServiceError, match="gecersiz/bayat"):
            service.run_script_generation()
        assert service.load_artifact("selected_script")["data"] is None

    def test_missing_preview_file_is_rejected(self, service):
        _ready(service)
        selection = service.load_artifact("moment_selection")["data"]
        from pathlib import Path
        Path(selection["selected_moment"]["preview_path"]).unlink()
        with pytest.raises(PipelineServiceError, match="gecersiz/bayat"):
            service.run_script_generation()

    def test_plan_decision_feeds_language_and_angle(self, service):
        _ready(service, content_language="en-US")
        result = service.run_script_generation()
        assert result["content_language"] == "en-US"
        assert result["theory_angle"] == THEORY_ANGLE
        assert result["theory_angle_auto"] is False

    def test_theory_angle_auto_is_recorded(self, storage, ownership):
        service = _service(storage, ownership)
        _seed(service, theory_angle="", theory_angle_auto=True)
        _select(service)
        result = service.run_script_generation()
        assert result["theory_angle_auto"] is True

    def test_no_google_credential_yields_prompt_only_package(self, storage,
                                                             ownership):
        # The narrated plan itself requires a credential, so the plan is made
        # with one and the generation runs through a client-less service over
        # the same namespace.
        planner = _service(storage, ownership)
        _ready(planner)
        service = _service(storage, ownership, google=False,
                           secrets=FakeSecrets(configured=False))
        result = service.run_script_generation()
        # No client -> a prompt package, never a fabricated script.
        assert result["script_text"] is None
        assert result["prompt_package"]["user_prompt"]
        assert result["status"] == "review_required"
        # Not reviewable: there is no script content to approve.
        assert service.approval_state("script")["reviewable"] is not True

    def test_provider_exception_produces_no_script(self, storage, ownership,
                                                   monkeypatch):
        service = _service(storage, ownership)
        _ready(service)

        def exploding(*_a, **_k):
            raise RuntimeError("provider boom")

        monkeypatch.setattr(script_generation, "call_google_script_routed",
                            exploding)
        with pytest.raises(RuntimeError, match="provider boom"):
            service.run_script_generation()
        assert service.load_artifact("selected_script")["data"] is None
        assert _progress(service)["state"] == "failed"

    def test_malformed_provider_response_yields_prompt_only(self, storage,
                                                            ownership,
                                                            monkeypatch):
        service = _service(storage, ownership)
        _ready(service)
        monkeypatch.setattr(
            script_generation, "call_google_script_routed",
            lambda *a, **k: {"candidate": "google-route",
                             "error": "JSON ayristirilamadi",
                             "latency_sec": 0.1, "google_route": {}})
        result = service.run_script_generation()
        assert result["script_text"] is None
        assert result["provider_attempts"][0]["ok"] is False
        assert "basarisiz" in result["status_reason"]

    def test_safety_refusal_cannot_create_a_valid_script(self, storage,
                                                         ownership,
                                                         monkeypatch):
        service = _service(storage, ownership)
        _ready(service)
        monkeypatch.setattr(
            script_generation, "call_google_script_routed",
            lambda *a, **k: {"candidate": "google-route",
                             "error": "guvenlik reddi",
                             "latency_sec": 0.1,
                             "google_route": {"error_kind": "safety_refusal"}})
        result = service.run_script_generation()
        assert result["script_text"] is None
        state = service.approval_state("script")
        assert state["approved"] is False
        assert state.get("reviewable") is not True

    def test_progress_stages_and_provider_audit(self, service):
        _ready(service)
        result = service.run_script_generation(actor="op")
        progress = _progress(service)
        assert progress["state"] == "completed"
        assert [s["key"] for s in progress["stages"]][:4] == \
            ["preparation", "request", "parse", "write"]
        events = service.audit.events()
        provider_events = [e for e in events
                           if e["event_type"] == "provider.call"]
        assert provider_events[-1]["detail"]["phase"] == "script_generation"
        assert provider_events[-1]["detail"]["provider"] == result["provider"]
        assert provider_events[-1]["detail"]["model"] == result["model"]
        assert provider_events[-1]["actor"] == "op"
        # The artifact write is audited BEFORE the provider call event.
        written = [i for i, e in enumerate(events)
                   if e["event_type"] == "artifact.written"
                   and e.get("artifact") == "results/selected_script.json"]
        called = [i for i, e in enumerate(events)
                  if e["event_type"] == "provider.call"]
        # Newest-first: the earlier write carries the HIGHER index.
        assert written[-1] > called[-1]

    def test_cancellation_fails_tracker_and_writes_nothing(self, storage,
                                                           ownership,
                                                           monkeypatch):
        service = _service(storage, ownership)
        _ready(service)
        monkeypatch.setattr(
            script_generation, "run_script_generation",
            lambda *a, **k: (_ for _ in ()).throw(
                OperationCancelled("is kullanici tarafindan durduruldu")))
        with pytest.raises(OperationCancelled):
            service.run_script_generation()
        assert service.load_artifact("selected_script")["data"] is None
        assert _progress(service)["state"] == "failed"

    def test_failed_status_result_fails_the_tracker(self, storage, ownership,
                                                    monkeypatch):
        """The engine never returns 'failed' today, but the branch exists."""
        service = _service(storage, ownership)
        _ready(service)
        real = script_generation.run_script_generation

        def failing(*args, **kwargs):
            result = real(*args, **kwargs)
            result["status"] = "failed"
            result["status_reason"] = "script uretilemedi"
            return result

        monkeypatch.setattr(script_generation, "run_script_generation", failing)
        result = service.run_script_generation()
        assert result["status"] == "failed"
        assert _progress(service)["state"] == "failed"

    def test_generation_does_not_require_transcript_or_glossary(self, service):
        _ready(service)
        assert service.load_artifact("moment_transcript")["data"] is None
        assert service.load_artifact("glossary")["data"] is None
        result = service.run_script_generation()
        assert result["script_text"]
        # Still absent afterwards: neither is read nor written by generation.
        assert service.load_artifact("moment_transcript")["data"] is None
        assert service.load_artifact("glossary")["data"] is None

    def test_generation_grants_no_approval_and_no_downstream(self, service):
        _ready(service)
        events_before = len(service.audit.events())
        service.run_script_generation()
        assert service.approval_state("script")["approved"] is False
        for key in ("selected_voiceover", "render_manifest", "publish_intent"):
            assert service.load_artifact(key)["data"] is None, key
        # `events()` is newest-first: the operation's events are at the FRONT.
        events_now = service.audit.events()
        new_events = events_now[:len(events_now) - events_before]
        assert not any(e["event_type"] in {"approval.granted", "job.submitted"}
                       for e in new_events)

    def test_regeneration_overwrites_and_drops_approval(self, service):
        _ready(service)
        service.run_script_generation()
        digest = service.approval_state("script")["content_sha256"]
        service.approve("script", reviewed_sha256=digest, approved_by="rev")
        assert service.approval_state("script")["valid"] is True
        service.run_script_generation()
        state = service.approval_state("script")
        assert state["approved"] is False
        assert state["valid"] is False


# ---------------------------------------------------------------------------
# update_script_text
# ---------------------------------------------------------------------------


class TestUpdateScriptText:
    def _generated(self, service):
        _ready(service)
        return service.run_script_generation()

    def test_actor_is_required(self, service):
        self._generated(service)
        with pytest.raises(PipelineServiceError, match="duzenleyen kisi"):
            service.update_script_text(script_text="Yeni metin", updated_by=" ")

    def test_empty_text_rejected(self, service):
        self._generated(service)
        with pytest.raises(PipelineServiceError, match="bos olamaz"):
            service.update_script_text(script_text="   ", updated_by="ed")

    def test_length_limit_enforced(self, service):
        self._generated(service)
        with pytest.raises(PipelineServiceError, match="20000"):
            service.update_script_text(script_text="a" * 20001,
                                        updated_by="ed")

    def test_missing_script_raises(self, service):
        _ready(service)
        with pytest.raises(ArtifactProblem):
            service.update_script_text(script_text="Yeni", updated_by="ed")

    def test_unchanged_text_is_noop(self, service):
        generated = self._generated(service)
        events_before = len(service.audit.events())
        result = service.update_script_text(
            script_text=generated["script_text"], updated_by="ed")
        assert result["status"] == "review_required"
        assert not result.get("manual_edits")
        assert len(service.audit.events()) == events_before

    def test_whitespace_is_stripped_before_comparison(self, service):
        generated = self._generated(service)
        result = service.update_script_text(
            script_text=f"  {generated['script_text']}  ", updated_by="ed")
        assert not result.get("manual_edits")

    def test_edit_records_manual_edit_and_audit(self, service):
        generated = self._generated(service)
        old_hash = service.approval_state("script")["content_sha256"]
        result = service.update_script_text(
            script_text="Elle yazilmis yeni script metni.", updated_by="ed",
            notes="hook zayifti")
        assert result["script_text"] == "Elle yazilmis yeni script metni."
        assert result["status"] == "review_required"
        assert "insan tarafindan duzenlendi" in result["status_reason"]
        assert result["human_approval"] == {
            "approved": False, "approved_by": None, "approved_at": None,
            "notes": None, "approved_content_sha256": None}
        entry = result["manual_edits"][0]
        assert entry["updated_by"] == "ed"
        assert entry["notes"] == "hook zayifti"
        assert entry["previous_script_sha256"] == old_hash
        assert entry["revision_id"]
        assert entry["updated_at"]
        event = [e for e in service.audit.events()
                 if e["event_type"] == "script.updated"][-1]
        assert event["actor"] == "ed"
        assert event["artifact"] == "results/selected_script.json"
        assert event["detail"]["previous_script_sha256"] == old_hash
        assert event["detail"]["revision_id"] == entry["revision_id"]
        assert generated["script_text"] != result["script_text"]

    def test_manual_edit_history_capped_at_fifty(self, service):
        self._generated(service)
        artifact = service.load_artifact("selected_script")["data"]
        artifact["manual_edits"] = [{"updated_by": f"e{i}"} for i in range(50)]
        service.write_artifact("selected_script", artifact, actor="seed")
        result = service.update_script_text(script_text="Yepyeni metin",
                                            updated_by="ed")
        assert len(result["manual_edits"]) == 50
        assert result["manual_edits"][0]["updated_by"] == "ed"

    def test_edit_invalidates_a_valid_approval(self, service):
        self._generated(service)
        digest = service.approval_state("script")["content_sha256"]
        service.approve("script", reviewed_sha256=digest, approved_by="rev")
        assert service.approval_state("script")["valid"] is True
        service.update_script_text(script_text="Degisti artik", updated_by="ed")
        state = service.approval_state("script")
        assert state["approved"] is False
        assert state["valid"] is False

    def test_edit_archives_voice_downstream(self, service):
        self._generated(service)
        # Seed the downstream artifacts a real voice run would have produced.
        service.write_artifact("selected_voiceover", {
            "artifact_type": "selected_voiceover", "status": "review_required",
            "script_sha256": "x" * 64, "audio_path": "results/tts/a.wav"},
            actor="seed")
        result = service.update_script_text(script_text="Yeni metin",
                                            updated_by="ed")
        assert service.load_artifact("selected_voiceover")["data"] is None
        revision_id = result["manual_edits"][0]["revision_id"]
        archived = (_ns_root(service) / "revisions" / revision_id
                    / "results" / "selected_voiceover.json")
        assert archived.is_file()
        event = [e for e in service.audit.events()
                 if e["event_type"] == "script.updated"][-1]
        assert "results/selected_voiceover.json" in \
            event["detail"]["downstream_archived"]

    def test_edit_does_not_start_tts_render_or_publish(self, service):
        self._generated(service)
        events_before = len(service.audit.events())
        service.update_script_text(script_text="Yeni metin", updated_by="ed")
        for key in ("selected_voiceover", "render_manifest", "publish_intent",
                    "publish_manifest"):
            assert service.load_artifact(key)["data"] is None, key
        # `events()` is newest-first: the operation's events are at the FRONT.
        events_now = service.audit.events()
        new_events = events_now[:len(events_now) - events_before]
        assert not any(e["event_type"] in {"provider.call", "job.submitted",
                                           "approval.granted"}
                       for e in new_events)


# ---------------------------------------------------------------------------
# downstream boundaries (TTS / render) and read purity
# ---------------------------------------------------------------------------


class TestScriptDownstreamBoundary:
    def test_tts_requires_an_approved_script(self, service):
        _ready(service)
        service.run_script_generation()
        with pytest.raises(PipelineServiceError, match="ONAYLI olmali"):
            service.run_tts()

    def test_tts_rejects_a_stale_approval(self, service):
        _ready(service)
        service.run_script_generation()
        digest = service.approval_state("script")["content_sha256"]
        service.approve("script", reviewed_sha256=digest, approved_by="rev")
        artifact = service.load_artifact("selected_script")["data"]
        artifact["script_text"] += " sonradan degisti"
        service.write_artifact("selected_script", artifact, actor="tamper")
        with pytest.raises(PipelineServiceError, match="onayi artik gecerli"):
            service.run_tts()

    def test_missing_script_blocks_tts(self, service):
        _ready(service)
        with pytest.raises(ArtifactProblem):
            service.run_tts()

    def test_script_review_view_shape(self, service):
        from creator_console.app import _script_review_view
        _ready(service)
        generated = service.run_script_generation()
        view = _script_review_view(service)
        assert view["data"]["script_text"] == generated["script_text"]
        assert view["data"]["hook"] == generated["hook"]
        assert view["data"]["character_count"] == len(generated["script_text"])
        assert view["data"]["provider"] == generated["provider"]
        assert view["data"]["model"] == generated["model"]
        assert view["data"]["status"] == "review_required"
        assert set(view["data"]) == {
            "hook", "script_text", "estimated_duration_sec",
            "character_count", "language", "provider", "model", "status",
            "status_reason", "theory_angle", "generated_at", "latency_sec"}

    def test_script_review_view_without_artifact(self, service):
        from creator_console.app import _script_review_view
        _ready(service)
        assert _script_review_view(service) == {"data": None, "problem": None}

    def test_read_queries_do_not_write(self, service):
        from creator_console.app import _script_review_view
        _ready(service)
        service.run_script_generation()
        before_events = len(service.audit.events())
        before_files = {
            str(p.relative_to(_ns_root(service))): p.read_bytes()
            for p in _ns_root(service).rglob("*") if p.is_file()}
        _script_review_view(service)
        service.approval_state("script")
        service.load_artifact("selected_script")
        after_files = {
            str(p.relative_to(_ns_root(service))): p.read_bytes()
            for p in _ns_root(service).rglob("*") if p.is_file()}
        assert after_files == before_files
        assert len(service.audit.events()) == before_events


# ---------------------------------------------------------------------------
# scope
# ---------------------------------------------------------------------------


class TestScriptScope:
    def test_selected_script_is_project_wide(self, service):
        # narrated_theory forces output_count=1, so the multi-slot namespace
        # is built in direct mode -- the PATH question is mode-independent.
        _seed(service, mode="direct_clip_series", output_count=2)
        _select(service, rank=1, slot=1)
        _select(service, rank=2, slot=2)
        # Not a per-slot artifact: every slot resolves the same path.
        assert service.artifact_relative("selected_script", 1) == \
            "results/selected_script.json"
        assert service.artifact_relative("selected_script", 2) == \
            "results/selected_script.json"
        from application import artifact_catalog
        assert "selected_script" not in artifact_catalog.PER_SLOT_ARTIFACTS

    def test_generation_reads_the_unslotted_selection(self, service):
        _ready(service)
        result = service.run_script_generation()
        assert result["selected_moment"]["rank"] == 1
        selection = service.load_artifact("moment_selection")["data"]
        assert video_analysis.validate_moment_selection(
            selection, service.load_artifact("hot_moments")["data"]) == []


# ---------------------------------------------------------------------------
# parity: standalone ScriptService vs the facade delegates
# ---------------------------------------------------------------------------

from application.services import ScriptService, WorkflowGate  # noqa: E402
from creator_console.audit import AuditLog  # noqa: E402
from infrastructure.persistence.local import (  # noqa: E402
    LocalArtifactRepository, LocalRevisionArchive)
from infrastructure.progress.local import LocalProgressFactory  # noqa: E402


def _standalone(storage, ownership, *, google=True, secrets=None):
    """A ScriptService built from real adapters -- no facade anywhere."""
    audit = AuditLog(storage, ownership)
    artifacts = LocalArtifactRepository(storage, ownership, audit)
    return ScriptService(
        artifacts=artifacts,
        workflow=WorkflowGate(artifacts=artifacts),
        archiver=LocalRevisionArchive(namespace=storage, ownership=ownership),
        progress=LocalProgressFactory(storage, ownership),
        audit=audit,
        secrets=secrets or FakeSecrets(),
        google_client_factory=((lambda: fixtures.fake_google_client())
                              if google else None))


def _ready_pair(tmp_path):
    """Two identically-seeded namespaces: one facade-driven, one standalone."""
    storage_a = LocalFilesystemStorage(tmp_path / "a" / "console_root")
    storage_b = LocalFilesystemStorage(tmp_path / "b" / "console_root")
    own = Ownership("usr_test", "wsp_test", "prj_test")
    facade = _service(storage_a, own)
    seeder = _service(storage_b, own)
    _ready(facade)
    _ready(seeder)
    return facade, seeder, _standalone(storage_b, own)


def _normalise(value):
    """Strip only legitimately-nondeterministic fields."""
    drop = {"generated_at", "reviewed_at", "updated_at", "latency_sec",
            "at", "recorded_at", "created_at", "usage_metadata",
            "revision_id", "previous_script_sha256", "hot_moments_source_path",
            "source_video", "source_video_identifier"}
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items() if k not in drop}
    if isinstance(value, list):
        return [_normalise(v) for v in value]
    return value


class TestScriptParity:
    def test_hot_moments_source_path_matches_the_facade_form(self, service):
        _ready(service)
        assert service.relative(service.path("hot_moments")) == \
            service._artifacts.artifact_relative("hot_moments")
        result = service.run_script_generation()
        assert result["hot_moments_source_path"] == "results/hot_moments.json"

    def test_generation_parity(self, tmp_path):
        facade, _seeder, standalone = _ready_pair(tmp_path)
        fixtures.PROVIDER_CALLS.clear()
        facade_art = facade.run_script_generation(actor="op")
        facade_calls = len(fixtures.PROVIDER_CALLS)
        fixtures.PROVIDER_CALLS.clear()
        solo_art = standalone.run_script_generation(actor="op")
        # Identical provider cost, not just identical output.
        assert len(fixtures.PROVIDER_CALLS) == facade_calls == 1
        assert _normalise(facade_art) == _normalise(solo_art)
        assert facade_art["hot_moments_source_path"] == \
            solo_art["hot_moments_source_path"]
        stored_facade = facade.load_artifact("selected_script")["data"]
        stored_solo = standalone._artifacts.load_artifact(
            "selected_script")["data"]
        assert _normalise(stored_facade) == _normalise(stored_solo)

    def test_generation_audit_and_progress_parity(self, tmp_path):
        facade, seeder, standalone = _ready_pair(tmp_path)
        facade.run_script_generation(actor="op")
        standalone.run_script_generation(actor="op")

        def trail(events):
            return [(e["event_type"], e["actor"], e.get("artifact"),
                     _normalise(e.get("detail")))
                    for e in events
                    if e["event_type"] in {"artifact.written", "provider.call"}]

        assert trail(facade.audit.events())[:2] == \
            trail(standalone._audit.events())[:2]
        facade_progress = _progress(facade)
        solo_progress = _progress(seeder)
        assert facade_progress["state"] == solo_progress["state"] == "completed"
        assert [s["key"] for s in facade_progress["stages"]] == \
            [s["key"] for s in solo_progress["stages"]]

    def test_edit_parity(self, tmp_path):
        facade, _seeder, standalone = _ready_pair(tmp_path)
        facade.run_script_generation()
        standalone.run_script_generation()
        facade_art = facade.update_script_text(
            script_text="Elle yazilmis metin", updated_by="ed", notes="n")
        solo_art = standalone.update_script_text(
            script_text="Elle yazilmis metin", updated_by="ed", notes="n")
        assert _normalise(facade_art) == _normalise(solo_art)
        assert facade_art["manual_edits"][0]["previous_script_sha256"] == \
            solo_art["manual_edits"][0]["previous_script_sha256"]

    def test_noop_edit_parity(self, tmp_path):
        facade, _seeder, standalone = _ready_pair(tmp_path)
        generated = facade.run_script_generation()
        standalone.run_script_generation()
        facade_art = facade.update_script_text(
            script_text=generated["script_text"], updated_by="ed")
        solo_art = standalone.update_script_text(
            script_text=generated["script_text"], updated_by="ed")
        assert _normalise(facade_art) == _normalise(solo_art)
        assert not facade_art.get("manual_edits")
        assert not solo_art.get("manual_edits")

    def test_exception_parity(self, tmp_path):
        facade, _seeder, standalone = _ready_pair(tmp_path)
        facade.run_script_generation()
        standalone.run_script_generation()
        for call in (
                lambda s: s.update_script_text(script_text="x", updated_by=""),
                lambda s: s.update_script_text(script_text=" ",
                                                updated_by="ed"),
                lambda s: s.update_script_text(script_text="a" * 20001,
                                                updated_by="ed")):
            with pytest.raises(Exception) as facade_exc:
                call(facade)
            with pytest.raises(Exception) as solo_exc:
                call(standalone)
            assert type(facade_exc.value) is type(solo_exc.value)
            assert str(facade_exc.value) == str(solo_exc.value)

    def test_gate_denial_parity(self, tmp_path):
        storage_a = LocalFilesystemStorage(tmp_path / "a" / "console_root")
        storage_b = LocalFilesystemStorage(tmp_path / "b" / "console_root")
        own = Ownership("usr_test", "wsp_test", "prj_test")
        facade = _service(storage_a, own)
        seeder = _service(storage_b, own)
        _seed(facade, mode="direct_clip_series")
        _seed(seeder, mode="direct_clip_series")
        standalone = _standalone(storage_b, own)
        with pytest.raises(PipelineServiceError) as facade_exc:
            facade.run_script_generation()
        with pytest.raises(PipelineServiceError) as solo_exc:
            standalone.run_script_generation()
        assert str(facade_exc.value) == str(solo_exc.value)


# ---------------------------------------------------------------------------
# provider behaviour
# ---------------------------------------------------------------------------


class TestScriptProvider:
    def test_fallback_stays_disabled(self, service, monkeypatch):
        """Only google is ever a candidate, even with other clients present."""
        seen = {}
        real = script_generation.build_script_candidates

        def spy(provider, model, allow_fallback, available):
            result = real(provider, model, allow_fallback, available)
            seen.update({"provider": provider, "allow_fallback": allow_fallback,
                         "candidates": result, "available": available})
            return result

        monkeypatch.setattr(script_generation, "build_script_candidates", spy)
        _ready(service)
        service.run_script_generation()
        assert seen["provider"] == "google"
        assert seen["allow_fallback"] is False
        assert [c[0] for c in seen["candidates"]] == ["google"]

    def test_other_provider_clients_are_built_but_not_selected(self, storage,
                                                               ownership):
        service = _service(storage, ownership, openai=True, anthropic=True)
        _ready(service)
        fixtures.PROVIDER_CALLS.clear()
        result = service.run_script_generation()
        assert result["provider"] == "google"
        providers = {c[0].split(".")[0] for c in fixtures.PROVIDER_CALLS}
        assert providers == {"google"}, fixtures.PROVIDER_CALLS

    def test_google_route_models_are_unchanged(self):
        assert script_generation.DEFAULT_SCRIPT_PROVIDER == "google"
        assert [m[0] for m in script_generation.GOOGLE_SCRIPT_MODELS] == [
            "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]

    def test_empty_provider_response_yields_prompt_only(self, storage,
                                                        ownership,
                                                        monkeypatch):
        service = _service(storage, ownership)
        _ready(service)
        monkeypatch.setattr(
            script_generation, "call_google_script_routed",
            lambda *a, **k: {"candidate": "google-route", "error": "bos yanit",
                             "latency_sec": 0.0, "google_route": {}})
        result = service.run_script_generation()
        assert result["script_text"] is None
        assert result["status"] == "review_required"

    def test_credentials_never_reach_artifact_or_audit(self, storage,
                                                       ownership):
        secret = "k" * 20
        service = _service(storage, ownership, google=False,
                           secrets=FakeSecrets(configured=True))
        _ready(service)
        # No client factory: the service builds one from the credential, which
        # the fake google_ai client builder will reject -- what matters is that
        # the secret value never lands in an artifact or an event.
        try:
            service.run_script_generation()
        except Exception:  # noqa: BLE001 - builder may refuse a fake key
            pass
        blob = json.dumps(service.audit.events(), default=str)
        loaded = service.load_artifact("selected_script")["data"]
        blob += json.dumps(loaded, default=str)
        assert secret not in blob


# ---------------------------------------------------------------------------
# explicit negatives
# ---------------------------------------------------------------------------


class TestScriptNegatives:
    def test_standalone_service_needs_no_facade(self, storage, ownership):
        standalone = _standalone(storage, ownership)
        # No plan at all: the gate -- not a facade -- denies the phase.
        with pytest.raises(PipelineServiceError):
            standalone.run_script_generation()

    def test_failed_generation_write_reports_no_success(self, service):
        from creator_console.faults import disk_full, patch_atomic_write
        _ready(service)
        events_before = len(service.audit.events())
        with patch_atomic_write(exception=disk_full("selected_script.json"),
                                match="selected_script.json"):
            with pytest.raises(OSError):
                service.run_script_generation(actor="op")
        assert service.load_artifact("selected_script")["data"] is None
        events_now = service.audit.events()
        new_events = events_now[:len(events_now) - events_before]
        # Provider.call is audited AFTER the write, so a failed write must not
        # leave a provider-success trail either.
        assert not any(e["event_type"] == "provider.call" for e in new_events)
        assert service.approval_state("script")["approved"] is False
        assert _progress(service)["state"] == "failed"

    def test_permission_denied_generation_write(self, service):
        from creator_console.faults import (patch_atomic_write,
                                            permission_denied)
        _ready(service)
        with patch_atomic_write(
                exception=permission_denied("selected_script.json"),
                match="selected_script.json"):
            with pytest.raises(PermissionError):
                service.run_script_generation()
        assert service.load_artifact("selected_script")["data"] is None

    def test_partial_generation_write_is_rejected_on_read(self, service):
        from creator_console.faults import InterruptedWrite, patch_atomic_write
        _ready(service)
        with patch_atomic_write(exception=InterruptedWrite("kesildi"),
                                partial_bytes=48,
                                match="selected_script.json"):
            with pytest.raises(InterruptedWrite):
                service.run_script_generation()
        loaded = service.load_artifact("selected_script")
        assert loaded["data"] is None
        assert loaded["problem"]
        assert service.approval_state("script")["approved"] is False

    def test_failed_edit_write_reports_no_success(self, service):
        from creator_console.faults import disk_full, patch_atomic_write
        _ready(service)
        generated = service.run_script_generation()
        events_before = len(service.audit.events())
        with patch_atomic_write(exception=disk_full("selected_script.json"),
                                match="selected_script.json"):
            with pytest.raises(OSError):
                service.update_script_text(script_text="Yeni metin",
                                            updated_by="ed")
        events_now = service.audit.events()
        new_events = events_now[:len(events_now) - events_before]
        assert not any(e["event_type"] == "script.updated"
                       for e in new_events)
        # The archive ran BEFORE the failed write: the script artifact itself
        # is unchanged, which is what must not be silently lost.
        loaded = service.load_artifact("selected_script")
        assert loaded["data"]["script_text"] == generated["script_text"]
        assert not loaded["data"].get("manual_edits")

    def test_generation_and_edit_never_publish(self, service):
        _ready(service)
        service.run_script_generation()
        digest = service.approval_state("script")["content_sha256"]
        service.approve("script", reviewed_sha256=digest, approved_by="rev")
        events_before = len(service.audit.events())
        service.update_script_text(script_text="Yeni metin", updated_by="ed")
        events_now = service.audit.events()
        new_events = events_now[:len(events_now) - events_before]
        assert not any(e["event_type"].startswith("publish")
                       for e in new_events)
        assert service.load_artifact("publish_manifest")["data"] is None

    def test_render_eligibility_blocked_without_script_approval(self, service):
        _ready(service)
        service.run_script_generation()
        chain = service.approval_chain()
        assert chain["script"]["approved"] is False
        assert chain["render"]["effective_valid"] is False

    def test_approved_script_satisfies_tts_prerequisite(self, service,
                                                        monkeypatch):
        _ready(service)
        service.run_script_generation()
        digest = service.approval_state("script")["content_sha256"]
        service.approve("script", reviewed_sha256=digest, approved_by="rev")
        assert service.approval_state("script")["valid"] is True
        # TTS proceeds past the script gate (the synthesis itself is faked).
        calls = {}
        monkeypatch.setattr(
            "local_pipeline.google_ai.run_google_tts",
            lambda *a, **k: calls.setdefault("ran", True) and None or
            {"status": "failed", "status_reason": "fixture"})
        try:
            service.run_tts()
        except Exception:  # noqa: BLE001 - synthesis is out of scope here
            pass
        assert calls.get("ran") is True
