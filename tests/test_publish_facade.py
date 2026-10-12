"""Characterization + negative tests for the publication boundary on the façade.

Written BEFORE the PublishService extraction. They pin the invariants that make
this repository's publishing safe: private-first is mandatory, a publish intent
authorizes exactly ONE upload, metadata/intent/visibility are three separate
approvals bound to three separate digests, and content approval alone never
authorizes an upload.

All tests walk the REAL approval path: artifacts are written with realistic
shapes and granted through `service.approve(...)`, the metadata/intent/
visibility gates and the checklist preconditions. The YouTube provider is a
double -- nothing here contacts a network, reads a real credential or uploads.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from creator_console.ids import Ownership
from creator_console.pipeline_service import (
    ArtifactProblem,
    PipelineService,
    PipelineServiceError,
)
from creator_console.storage import LocalFilesystemStorage
from local_pipeline import publish, video_analysis

SOURCE_BYTES = b"\x00\x01synthetic-source" * 512

PROBE_INFO = {
    "streams": [{"codec_type": "video", "width": 1920, "height": 1080},
                {"codec_type": "audio"}],
    "format": {"duration": "180.0"},
}


class FakeSecrets:
    """No credential is configured; `credential()`/`reference()` only answer
    capability questions -- they never carry a value."""

    def reference(self, kind):
        return type("Ref", (), {"configured": False})()

    def credential(self, kind):
        return type("Cred", (), {"configured": False})()


class FakePublishProvider(publish.PublishProvider):
    """A `local_pipeline.publish.PublishProvider` double.

    Records every call so a test can prove WHEN the provider was reached --
    the whole point of the gates is that most paths must never get here.
    """

    platform = publish.PLATFORM_YOUTUBE

    def __init__(self, *, upload_result=None, visibility_result=None, state=None):
        self.upload_calls: list = []
        self.visibility_calls: list = []
        self._upload_result = upload_result or publish.PublishResult(
            platform=publish.PLATFORM_YOUTUBE,
            status="review_required",
            status_reason="private yukleme tamamlandi (fake)",
            remote_id="fakeVideoId123",
            remote_url="https://youtu.be/fakeVideoId123",
            visibility=publish.VISIBILITY_PRIVATE,
            published_at="2026-01-01T00:00:00Z",
            quota_units_estimated=1600,
        )
        self._visibility_result = visibility_result  # None = echo request
        self._state = state or {
            "platform": publish.PLATFORM_YOUTUBE,
            "configured": True,
            "authorized": True,
            "channel_id": "UC_fake",
        }

    def describe_credential_state(self) -> dict:
        return dict(self._state)

    def upload_private(self, video_path, metadata, target, progress_callback=None):
        self.upload_calls.append({
            "video_path": str(video_path),
            "metadata": dict(metadata or {}),
            "visibility": getattr(target, "visibility", None),
        })
        if callable(progress_callback):
            progress_callback(10, 10)
        return self._upload_result

    def set_visibility(self, remote_id, visibility, publish_at=None):
        self.visibility_calls.append({
            "remote_id": remote_id, "visibility": visibility,
            "publish_at": publish_at,
        })
        if self._visibility_result is not None:
            return self._visibility_result
        return publish.PublishResult(
            platform=publish.PLATFORM_YOUTUBE,
            status="production_ready",
            status_reason="gorunurluk uygulandi (fake)",
            remote_id=remote_id,
            visibility=visibility,
        )


@pytest.fixture
def ownership():
    return Ownership("usr_test", "wsp_test", "prj_test")


@pytest.fixture
def storage(tmp_path):
    return LocalFilesystemStorage(tmp_path / "console_root")


@pytest.fixture
def provider():
    return FakePublishProvider()


@pytest.fixture
def service(storage, ownership, provider):
    return PipelineService(
        storage, ownership, FakeSecrets(), youtube_provider=provider,
        probe_fn=lambda *a: (PROBE_INFO, "ffprobe"),
        ffmpeg_bin="/x/ffmpeg", ffprobe_bin="/x/ffprobe")


def complete_copyright(service):
    """Satisfy the real rights checklist (never bypass it)."""
    for item in service.copyright_review_state()["items"]:
        service.set_copyright_review_item(item["key"], checked=True,
                                           reviewed_by="reviewer")


def plan_digest(service):
    return service.approval_state("processing_plan")["content_sha256"]


def build_hot_moments():
    return {
        "method": "fixture",
        "status": "review_required",
        "hot_moments": [{
            "rank": 1,
            "source_start_sec": 0.0, "source_end_sec": 30.0,
            "aligned_start_sec": 0.0, "aligned_end_sec": 30.0,
            "score": 0.9, "reason": "fixture moment",
            "transcript": "secilen sahnenin diyalog metni",
        }],
    }


def write_approved_chain(service, storage, ownership):
    """Walk the REAL chain until `approval_chain()["render"].effective_valid`.

    plan (+ copyright checklist) -> moment_selection -> transcript -> render
    (+ render checklist). Nothing is stubbed: every approval is granted by the
    canonical hash-bound gate through `service.approve(...)`.
    """
    service.create_project(title="Yayin", created_by="tester")
    service.register_source_video(filename="src.mp4", payload=SOURCE_BYTES,
                                  registered_by="tester")
    service.run_source_assessment(actor="tester")
    service.create_processing_plan(mode="direct_clip_series", selected_by="tester",
                                   output_count=1)
    complete_copyright(service)
    service.approve("processing_plan", reviewed_sha256=plan_digest(service),
                     approved_by="reviewer")

    hot = build_hot_moments()
    service.write_artifact("hot_moments", hot, actor="tester")
    service.write_artifact(
        "moment_selection",
        video_analysis.build_moment_selection(hot, 1, "tester",
                                              require_preview=False),
        actor="tester")

    service.write_artifact("moment_transcript", {
        "artifact_type": "moment_transcript", "status": "review_required",
        "words": [{"text": "merhaba", "start": 0.0, "end": 0.5}],
        "cues": [],
    }, actor="tester")
    service.approve("transcript", approved_by="reviewer",
                     reviewed_sha256=service.approval_state("transcript")
                     ["content_sha256"])

    video = storage.resolve(ownership, "results/render/final_short.mp4",
                            create_parent=True)
    video.write_bytes(b"final-video-bytes" * 1024)
    service.write_artifact("render_manifest", {
        "status": "review_required",
        "config": {"pipeline_mode": "original_audio_subtitles"},
        "output": {"final_video_path": str(video),
                   "validation": {"passed": True}},
        "approval": {"conditions": {"technical_validation_passed": True,
                                    "moment_selection_approved": True},
                     "failed_conditions": []},
    }, actor="tester")
    for item in service.render_review_state()["items"]:
        service.set_render_review_item(item["key"], checked=True,
                                        reviewed_by="reviewer")
    service.approve("render", approved_by="reviewer",
                     reviewed_sha256=service.approval_state("render")
                     ["content_sha256"])
    return video


def write_ingest(service):
    service.write_artifact("ingest_manifest", {
        "artifact_kind": "ingest_manifest", "status": "review_required",
        "candidate_url": "https://www.youtube.com/watch?v=fixture",
        "uploader": "Ornek Kanal",
        "rights": {
            "license_type": "own",
            "decided_by": "tester",
            "decided_at": "2026-01-01T00:00:00Z",
            "decision_sha256": "a" * 64,
        },
    }, actor="tester")


def write_approved_metadata(service):
    artifact = service.write_manual_metadata(
        title="Bu sahnede gozden kacan detay", description="aciklama",
        tags=["analiz"], actor="reviewer")
    artifact = service.select_publication_metadata(title_index=0)
    return service.approve_publication_metadata(
        reviewed_sha256=artifact["selected_sha256"], approved_by="reviewer")


@pytest.fixture
def publishable(service, storage, ownership):
    """A project whose whole content + metadata approval chain is satisfied."""
    video = write_approved_chain(service, storage, ownership)
    write_ingest(service)
    metadata = write_approved_metadata(service)
    return service, video, metadata


def approved_intent(service):
    intent = service.create_publish_intent(actor="reviewer")
    return service.approve_publish_intent(
        reviewed_sha256=intent["decision_sha256"], approved_by="reviewer")


# ---------------------------------------------------------------------------
# publication state
# ---------------------------------------------------------------------------

class TestPublishState:
    def test_empty_project_reports_blockers(self, service):
        state = service.publish_state()
        assert state["publishable"] is False
        assert state["preconditions"]
        assert state["initial_visibility"] == "private"
        assert state["visibility_values"] == ["private", "unlisted", "public"]
        assert state["planned_platforms"]
        assert state["registered_platforms"] == ["youtube"]
        assert state["intent"] is None
        assert state["manifest"] is None
        assert state["intent_approval"]["valid"] is False
        assert state["credential"]["platform"] == "youtube"
        assert state["credential"]["configured"] is True

    def test_credential_state_never_contains_a_token(self, service, provider):
        state = service.publish_credential_state()
        assert state == provider._state
        assert "token" not in str(state).lower()


# ---------------------------------------------------------------------------
# metadata: manual path, selection, approval
# ---------------------------------------------------------------------------

class TestPublicationMetadata:
    def test_manual_metadata_is_born_review_required(self, service):
        artifact = service.write_manual_metadata(
            title="Manuel yazilan baslik", description="desc", tags=["a"])
        assert artifact["status"] == "review_required"
        assert artifact["origin"] == "manual"
        assert artifact["artifact_kind"] == "publication_metadata"
        loaded = service.load_artifact("publication_metadata")
        assert loaded["data"]["title_variants"] == ["Manuel yazilan baslik"]

    def test_selection_binds_the_exact_choice(self, service):
        artifact = service.write_manual_metadata(title="Secilecek baslik")
        selected = service.select_publication_metadata(title_index=0)
        assert selected["selected"]["title"] == "Secilecek baslik"
        assert selected["selected_sha256"]
        assert selected["status"] == "review_required"

    def test_out_of_range_selection_is_a_plain_value_error(self, service):
        service.write_manual_metadata(title="Secilecek baslik")
        with pytest.raises(ValueError, match="title_index"):
            service.select_publication_metadata(title_index=4)

    def test_approve_with_wrong_digest_is_rejected_and_audited(self, service):
        service.write_manual_metadata(title="Onaylanacak baslik")
        service.select_publication_metadata(title_index=0)
        with pytest.raises(PipelineServiceError, match="ONAY REDDEDILDI"):
            service.approve_publication_metadata(
                reviewed_sha256="0" * 64, approved_by="reviewer")
        rejected = [e for e in service.audit.events()
                    if e["event_type"] == "approval.rejected"]
        assert rejected and rejected[0]["detail"]["kind"] == "publication_metadata"

    def test_approve_binds_selection_digest_and_audits(self, service):
        artifact = service.write_manual_metadata(title="Onaylanacak baslik")
        artifact = service.select_publication_metadata(title_index=0)
        approved = service.approve_publication_metadata(
            reviewed_sha256=artifact["selected_sha256"], approved_by="reviewer")
        assert approved["status"] == "approved"
        block = approved["metadata_approval"]
        assert block["approved_selection_sha256"] == artifact["selected_sha256"]
        granted = [e for e in service.audit.events()
                   if e["event_type"] == "approval.granted"]
        assert granted[0]["detail"]["kind"] == "publication_metadata"
        assert granted[0]["detail"]["approved_content_sha256"] == \
            artifact["selected_sha256"]

    def test_reselection_invalidates_the_metadata_approval(self, service):
        artifact = service.write_manual_metadata(title="Yeniden secim")
        artifact = service.select_publication_metadata(title_index=0)
        service.approve_publication_metadata(
            reviewed_sha256=artifact["selected_sha256"], approved_by="reviewer")
        reselected = service.select_publication_metadata(title_index=0)
        assert reselected["status"] == "review_required"
        assert "metadata_approval" not in reselected

    def test_metadata_approval_is_not_intent_approval(self, publishable):
        service, _video, _metadata = publishable
        with pytest.raises((ArtifactProblem, PermissionError)):
            service.run_publish_upload(actor="reviewer")


class TestMetadataGeneration:
    def test_without_script_or_transcript_is_refused(self, service):
        service.create_project(title="Meta", created_by="tester")
        with pytest.raises(PipelineServiceError,
                           match="script veya transcript gerekli"):
            service.run_metadata_generation(actor="tester")

    def test_without_google_credential_is_actionable(self, service):
        service.create_project(title="Meta", created_by="tester")
        service.write_artifact("selected_script", {
            "status": "review_required", "provider_attempts": [],
            "script_text": "Yeterince uzun bir script metni."}, actor="tester")
        with pytest.raises(PipelineServiceError, match="Gemini kimlik bilgisi"):
            service.run_metadata_generation(actor="tester")

    def test_generated_metadata_flow_and_audit(self, storage, ownership, provider):
        payload = json.dumps({
            "titles": ["Uretilen baslik bir", "Uretilen baslik iki",
                       "Uretilen baslik uc"],
            "description": "Uretilmis aciklama",
            "tags": ["bir", "iki"], "hashtags": ["#bir"],
        })

        class _Models:
            def generate_content(self, *, model, contents, config=None):
                return type("R", (), {"text": payload, "candidates": [],
                                      "usage_metadata": {"total_token_count": 9}})()

            def list(self):
                from local_pipeline import google_ai
                return [type("M", (), {"name": f"models/{m}"})()
                        for m in google_ai.MODEL_ALLOWLIST]

        svc = PipelineService(
            storage, ownership, FakeSecrets(), youtube_provider=provider,
            google_client_factory=lambda: type("C", (), {"models": _Models()})(),
            probe_fn=lambda *a: (PROBE_INFO, "ffprobe"),
            ffmpeg_bin="/x/ffmpeg", ffprobe_bin="/x/ffprobe")
        svc.create_project(title="Meta", created_by="tester")
        svc.write_artifact("selected_script", {
            "status": "review_required", "provider_attempts": [],
            "script_text": "Yeterince uzun bir script metni."}, actor="tester")
        artifact = svc.run_metadata_generation(actor="tester")
        assert artifact["artifact_kind"] == "publication_metadata"
        calls = [e for e in svc.audit.events() if e["event_type"] == "provider.call"]
        assert calls and calls[0]["detail"]["phase"] == "metadata_generation"


# ---------------------------------------------------------------------------
# publish intent
# ---------------------------------------------------------------------------

class TestPublishIntent:
    def test_missing_render_is_an_artifact_problem_not_a_gate_error(self, service):
        service.create_project(title="Yayin", created_by="tester")
        with pytest.raises(ArtifactProblem, match="bulunamadi"):
            service.create_publish_intent(actor="reviewer")

    def test_unapproved_chain_cannot_create_an_intent(self, service):
        service.create_project(title="Yayin", created_by="tester")
        service.write_artifact("render_manifest", {
            "status": "review_required",
            "output": {"final_video_path": "results/render/final_short.mp4"},
            "approval": {"conditions": {}, "failed_conditions": []},
        }, actor="tester")
        service.write_manual_metadata(title="Baslik burada")
        service.select_publication_metadata(title_index=0)
        loaded = service.load_artifact("publication_metadata")["data"]
        service.approve_publication_metadata(
            reviewed_sha256=loaded["selected_sha256"], approved_by="reviewer")
        with pytest.raises(PipelineServiceError, match="YAYIN ENGELLI"):
            service.create_publish_intent(actor="reviewer")

    def test_intent_is_private_first_and_digest_bound(self, publishable):
        service, video, _metadata = publishable
        intent = service.create_publish_intent(actor="reviewer")
        assert intent["status"] == "review_required"
        assert intent["decision"]["initial_visibility"] == "private"
        assert intent["upload_performed"] is False
        assert intent["decision"]["video_sha256"] == \
            hashlib.sha256(video.read_bytes()).hexdigest()
        written = [e for e in service.audit.events()
                   if e["event_type"] == "artifact.written"]
        assert written[0]["artifact"] == "results/publish_intent.json"
        assert written[0]["detail"]["initial_visibility"] == "private"

    def test_intent_approval_wrong_digest_is_rejected(self, publishable):
        service, _video, _metadata = publishable
        service.create_publish_intent(actor="reviewer")
        with pytest.raises(PipelineServiceError, match="ONAY REDDEDILDI"):
            service.approve_publish_intent(
                reviewed_sha256="0" * 64, approved_by="reviewer")
        rejected = [e for e in service.audit.events()
                    if e["event_type"] == "approval.rejected"]
        assert rejected[0]["detail"]["kind"] == "publish_intent"

    def test_intent_approval_records_one_upload_authority(self, publishable):
        service, _video, _metadata = publishable
        approved = approved_intent(service)
        assert approved["status"] == "approved"
        assert approved["publish_approval"]["authorizes"] == \
            "single upload with visibility=private"
        granted = [e for e in service.audit.events()
                   if e["event_type"] == "approval.granted"]
        assert granted[0]["detail"]["kind"] == "publish_intent"


# ---------------------------------------------------------------------------
# private upload
# ---------------------------------------------------------------------------

class TestPrivateUpload:
    def test_unapproved_intent_never_reaches_the_provider(self, publishable,
                                                        provider):
        service, _video, _metadata = publishable
        service.create_publish_intent(actor="reviewer")
        with pytest.raises(PermissionError, match="YUKLEME REDDEDILDI"):
            service.run_publish_upload(actor="reviewer")
        assert provider.upload_calls == []

    def test_successful_upload_stays_private_and_persists(self, publishable,
                                                          provider):
        service, video, _metadata = publishable
        approved_intent(service)
        manifest = service.run_publish_upload(actor="reviewer")
        assert manifest["upload"]["remote_id"] == "fakeVideoId123"
        assert manifest["current_visibility"] == "private"
        assert manifest["visibility_approval"] is None
        # The provider saw PRIVATE bytes -- never a wider audience.
        assert provider.upload_calls[0]["visibility"] == "private"
        # The intent was spent, both artifacts persisted.
        intent = service.load_artifact("publish_intent")["data"]
        assert intent["upload_performed"] is True
        assert intent["upload_attempted_at"]
        saved = service.load_artifact("publish_manifest")["data"]
        assert saved["upload"]["remote_id"] == "fakeVideoId123"
        calls = [e for e in service.audit.events() if e["event_type"] == "provider.call"]
        assert calls[0]["detail"]["phase"] == "publish_upload"

    def test_a_persisted_remote_id_blocks_a_second_upload(self, publishable,
                                                          provider):
        service, _video, _metadata = publishable
        approved_intent(service)
        service.run_publish_upload(actor="reviewer")
        with pytest.raises(PipelineServiceError, match="zaten bir yukleme"):
            service.run_publish_upload(actor="reviewer")
        assert len(provider.upload_calls) == 1

    def test_a_failed_upload_still_consumes_the_intent(self, publishable,
                                                       provider):
        """Existing semantics: a provider-level failure consumes the intent.
        Re-upload requires a NEW intent + approval -- retry is not free."""
        provider._upload_result = publish.PublishResult(
            platform=publish.PLATFORM_YOUTUBE, status="failed",
            status_reason="quota", error="quotaExceeded")
        service, _video, _metadata = publishable
        approved_intent(service)
        manifest = service.run_publish_upload(actor="reviewer")
        assert manifest["status"] == "failed"
        # M7: the recorded attempt fails closed -- same consumed-intent
        # semantics, now refused at the service gate with an explanation.
        with pytest.raises(PipelineServiceError, match="yukleme denemesi"):
            service.run_publish_upload(actor="reviewer")
        assert len(provider.upload_calls) == 1

    def test_stale_intent_digest_is_refused(self, publishable, provider):
        service, _video, _metadata = publishable
        approved_intent(service)
        intent = service.load_artifact("publish_intent")["data"]
        intent["decision"]["requested_visibility"] = "unlisted"
        service.write_artifact("publish_intent", intent, actor="tester")
        with pytest.raises(PermissionError, match="ONAY GECERSIZ|REDDEDILDI"):
            service.run_publish_upload(actor="reviewer")
        assert provider.upload_calls == []

    def test_changed_video_bytes_block_the_upload(self, publishable, provider,
                                                storage, ownership):
        service, video, _metadata = publishable
        approved_intent(service)
        video.write_bytes(b"swapped-after-approval" * 100)
        with pytest.raises(PermissionError, match="degismis"):
            service.run_publish_upload(actor="reviewer")
        assert provider.upload_calls == []


# ---------------------------------------------------------------------------
# visibility: the THIRD gate
# ---------------------------------------------------------------------------

class TestVisibilityGate:
    def _manifest(self, service):
        approved_intent(service)
        return service.run_publish_upload(actor="reviewer")

    def test_successful_upload_does_not_authorize_public(self, publishable,
                                                         provider):
        service, _video, _metadata = publishable
        manifest = self._manifest(service)
        assert manifest["current_visibility"] == "private"
        with pytest.raises(PermissionError, match="GORUNURLUK"):
            service.run_visibility_change(actor="reviewer")
        assert provider.visibility_calls == []

    def test_visibility_approval_binds_the_reviewed_remote_id(self, publishable):
        service, _video, _metadata = publishable
        self._manifest(service)
        with pytest.raises(PipelineServiceError, match="eslesmiyor"):
            service.approve_publish_visibility(
                target_visibility="public", reviewed_remote_id="someOtherId",
                approved_by="reviewer")
        rejected = [e for e in service.audit.events()
                    if e["event_type"] == "approval.rejected"]
        assert rejected[0]["detail"]["kind"] == "visibility"

    def test_approved_visibility_is_applied_and_audited(self, publishable,
                                                        provider):
        service, _video, _metadata = publishable
        self._manifest(service)
        manifest = service.approve_publish_visibility(
            target_visibility="public", reviewed_remote_id="fakeVideoId123",
            approved_by="reviewer")
        assert manifest["visibility_approval"]["target_visibility"] == "public"
        updated = service.run_visibility_change(actor="reviewer")
        assert updated["current_visibility"] == "public"
        assert provider.visibility_calls == [
            {"remote_id": "fakeVideoId123", "visibility": "public",
             "publish_at": None}]
        calls = [e for e in service.audit.events() if e["event_type"] == "provider.call"]
        assert calls[0]["detail"]["phase"] == "visibility_change"

    def test_publish_state_after_upload(self, publishable):
        service, _video, _metadata = publishable
        self._manifest(service)
        state = service.publish_state()
        assert state["manifest"]["upload"]["remote_id"] == "fakeVideoId123"
        assert state["intent"]["upload_performed"] is True
        assert state["intent_approval"]["approved"] is True


# ---------------------------------------------------------------------------
# fault-injection: external side effect vs local persistence
# ---------------------------------------------------------------------------

class TestPublishFaultBoundaries:
    def test_failed_intent_persistence_is_not_reported_as_granted(self, publishable):
        from creator_console.faults import patch_atomic_write

        service, _video, _metadata = publishable
        intent = service.create_publish_intent(actor="reviewer")
        with patch_atomic_write(match="publish_intent.json"):
            with pytest.raises(OSError):
                service.approve_publish_intent(
                    reviewed_sha256=intent["decision_sha256"],
                    approved_by="reviewer")
        assert service.load_artifact("publish_intent")["data"]["status"] == \
            "review_required"
        granted = [e for e in service.audit.events()
                   if e["event_type"] == "approval.granted"
                   and e["detail"].get("kind") == "publish_intent"]
        assert granted == []

    def test_upload_success_plus_manifest_write_failure_leaves_an_honest_state(
            self, publishable, provider):
        """The closed crash window (M7): remote upload succeeded but the
        local manifest write failed. The system must NOT report a successful
        publish and must NOT fabricate a video id -- but the persisted attempt
        marker now preserves the observed remote id as evidence and blocks a
        duplicate upload on retry."""
        from creator_console.faults import patch_atomic_write

        service, _video, _metadata = publishable
        approved_intent(service)
        with patch_atomic_write(match="publish_manifest.json"):
            with pytest.raises(OSError):
                service.run_publish_upload(actor="reviewer")
        # The provider DID upload (side effect happened), but nothing local
        # claims success: no manifest, intent still not upload_performed.
        assert len(provider.upload_calls) == 1
        assert service.load_artifact("publish_manifest")["data"] is None
        intent = service.load_artifact("publish_intent")["data"]
        assert intent["upload_performed"] is False
        assert intent["upload_attempt"]["status"] == "uncertain"
        assert intent["upload_attempt"]["observed_remote_id"] == "fakeVideoId123"
        # M7: the retry is refused BEFORE the provider -- no duplicate.
        with pytest.raises(PipelineServiceError, match="yukleme denemesi"):
            service.run_publish_upload(actor="reviewer")
        assert len(provider.upload_calls) == 1

    def test_manifest_written_but_intent_mark_failed_retry_is_safe(self, publishable,
                                                                   provider):
        """The other half of the window: manifest persisted with remote_id,
        then the 'spent' write failed. The remote-id guard deduplicates the
        retry -- no second upload."""
        from creator_console.faults import patch_atomic_write

        service, _video, _metadata = publishable
        approved_intent(service)
        # Fault only the FINAL write: the in-flight marker (1st publish_intent
        # write) and the manifest succeed; the spent-marker update fails.
        original = service.publish._artifacts.write_artifact
        calls = {"n": 0}

        def flaky(key, data, **kw):
            if key == "publish_intent":
                calls["n"] += 1
                if calls["n"] >= 2:
                    raise OSError(28, "ENOSPC", "publish_intent.json")
            return original(key, data, **kw)

        service.publish._artifacts.write_artifact = flaky
        try:
            with pytest.raises(OSError):
                service.run_publish_upload(actor="reviewer")
        finally:
            service.publish._artifacts.write_artifact = original
        # The manifest exists with a remote_id: the retry is refused BEFORE
        # the provider is reached again.
        assert service.load_artifact("publish_manifest")["data"]["upload"]["remote_id"]
        with pytest.raises(PipelineServiceError, match="zaten bir yukleme"):
            service.run_publish_upload(actor="reviewer")
        assert len(provider.upload_calls) == 1



class _StubApprovalQuery:
    """ApprovalQuery port double -- proves PublishService consumes the port,
    not the facade and not a concrete service."""

    def __init__(self, render_valid=True):
        self.chain_calls = 0
        self._render_valid = render_valid

    def chain(self, slot=1):
        self.chain_calls += 1
        return {"render": {"effective_valid": self._render_valid}}


def hand_wired_publish(tmp_path, provider=None, approvals=None):
    from application.services.publish_service import PublishService
    from infrastructure.persistence.local import LocalArtifactRepository
    from infrastructure.progress.local import LocalProgressFactory
    from creator_console.audit import AuditLog
    ownership = Ownership("usr_hw", "wsp_hw", "prj_hw")
    storage = LocalFilesystemStorage(tmp_path / "store")
    audit = AuditLog(storage, ownership)
    artifacts = LocalArtifactRepository(storage, ownership, audit)
    provider = provider or FakePublishProvider()
    approvals = approvals or _StubApprovalQuery()
    service = PublishService(
        artifacts=artifacts, audit=audit, namespace=storage,
        ownership=ownership,
        progress=LocalProgressFactory(storage, ownership),
        approvals=approvals, secrets=FakeSecrets(), provider=provider)
    return service, provider, approvals, artifacts


class TestPublishServiceDirectConstruction:
    """PublishService constructs and operates with only ports -- no facade."""

    def test_constructs_with_ports_only(self, tmp_path):
        service, provider, approvals, _artifacts = hand_wired_publish(tmp_path)
        assert service._provider is provider
        assert service._approvals is approvals

    def test_manual_metadata_without_facade(self, tmp_path):
        service, _provider, _approvals, artifacts = hand_wired_publish(tmp_path)
        artifacts.write_artifact("processing_plan", {
            "status": "approved", "decision": {"content_language": "tr-TR"}},
            actor="tester")
        artifacts.write_artifact("ingest_manifest", {
            "status": "review_required",
            "rights": {"license_type": "own"}}, actor="tester")
        service.write_manual_metadata(title="Manuel baslik",
                                      description="aciklama", actor="editor")
        service.select_metadata(title_index=0)
        state = service.publish_state()
        assert state["metadata"]["artifact_kind"] == "publication_metadata"
        assert state["metadata"]["selected"]["title"] == "Manuel baslik"

    def test_create_intent_uses_approval_query_port(self, publishable):
        """The facade-built service routes chain() through ApprovalService,
        which satisfies the ApprovalQuery protocol."""
        from application.ports import ApprovalQuery
        service, _video, _metadata = publishable
        assert isinstance(service.approval, ApprovalQuery)
        assert service.publish._approvals is service.approval
        intent = approved_intent(service)
        assert intent["status"] == "approved"

    def test_intent_rejected_when_query_reports_stale(self, tmp_path):
        """Render manifest + metadata present; the port says the render
        approval is stale -> YAYIN ENGELLI before any intent is written."""
        service, _p, approvals, artifacts = hand_wired_publish(
            tmp_path, approvals=_StubApprovalQuery(render_valid=False))
        artifacts.write_artifact("render_manifest", {
            "status": "approved",
            "approval": {"status": "approved"},
            "output": {"final_video_path": "results/render/final_short.mp4"}},
            actor="tester")
        artifacts.write_artifact("processing_plan", {
            "status": "approved", "decision": {"content_language": "tr-TR"}},
            actor="tester")
        artifacts.write_artifact("ingest_manifest", {
            "status": "review_required",
            "rights": {"license_type": "own"}}, actor="tester")
        service.write_manual_metadata(title="Manuel baslik", actor="editor")
        service.select_metadata(title_index=0)
        with pytest.raises(PipelineServiceError, match="YAYIN ENGELLI"):
            service.create_intent(actor="reviewer")
        assert approvals.chain_calls == 1
        assert artifacts.load_artifact("publish_intent")["data"] is None


class TestPublishFacadeParity:

    def test_publish_attr_is_publish_service(self, publishable):
        from application.services.publish_service import PublishService
        service, _video, _metadata = publishable
        assert isinstance(service.publish, PublishService)

    def test_all_public_methods_delegate(self, publishable):
        """Every migrated public method routes to a PublishService method;
        the facade adds nothing."""
        service, _video, _metadata = publishable
        reached = {}

        def wrap(name):
            original = getattr(service.publish, name)

            def spy(*a, **kw):
                reached[name] = True
                return original(*a, **kw)
            return spy

        for name in ("generate_metadata", "write_manual_metadata",
                     "select_metadata", "approve_metadata", "provider",
                     "credential_state", "authorization_url",
                     "complete_authorization", "create_intent",
                     "approve_intent", "run_upload", "approve_visibility",
                     "apply_visibility", "publish_state"):
            setattr(service.publish, name, wrap(name))

        state = service.publish_state()
        sha = state["metadata"]["selected_sha256"]
        calls = [
            (service.run_metadata_generation, (),
             {"actor": "editor"}),
            (service.write_manual_metadata, (),
             {"title": "T", "actor": "editor"}),
            (service.select_publication_metadata, (),
             {"title_index": 0}),
            (service.approve_publication_metadata, (),
             {"reviewed_sha256": sha, "approved_by": "reviewer"}),
            (service.youtube_provider, (), {}),
            (service.publish_credential_state, (), {}),
            (service.youtube_authorization_url, (),
             {"redirect_uri": "http://localhost/cb"}),
            (service.complete_youtube_authorization, (),
             {"code": "x", "redirect_uri": "http://localhost/cb"}),
            (service.create_publish_intent, (), {"actor": "reviewer"}),
            (service.approve_publish_intent, (),
             {"reviewed_sha256": "0" * 64, "approved_by": "reviewer"}),
            (service.run_publish_upload, (), {"actor": "reviewer"}),
            (service.approve_publish_visibility, (),
             {"target_visibility": "public",
              "reviewed_remote_id": "x", "approved_by": "reviewer"}),
            (service.run_visibility_change, (), {"actor": "reviewer"}),
            (service.publish_state, (), {}),
        ]
        for invoke, args, kwargs in calls:
            try:
                invoke(*args, **kwargs)
            except Exception:
                pass  # only the routing matters, not the outcome
        missing = {"generate_metadata", "write_manual_metadata",
                   "select_metadata", "approve_metadata", "provider",
                   "credential_state", "authorization_url",
                   "complete_authorization", "create_intent",
                   "approve_intent", "run_upload", "approve_visibility",
                   "apply_visibility", "publish_state"} - set(reached)
        assert not missing, f"facade methods never reached: {missing}"


class TestPublishArchitectureGuards:

    def test_publish_service_does_not_import_facade_or_transport(self):
        import ast
        from pathlib import Path
        src = Path("application/services/publish_service.py").read_text(
            encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        roots = {m.split(".")[0] for m in imported}
        assert not (roots & {"creator_console", "fastapi", "starlette"})
        assert "local_pipeline" in roots  # policy + provider stay canonical

    def test_publish_service_uses_approval_query_not_facade_method(self):
        import inspect
        from application.services.publish_service import PublishService
        sig = inspect.signature(PublishService.__init__)
        assert "approvals" in sig.parameters
        import application.services.publish_service as mod
        src = inspect.getsource(mod)
        assert "ApprovalQuery" in src
        assert "approval_chain" not in src  # no facade method coupling

    def test_approval_service_satisfies_approval_query(self, storage,
                                                     ownership):
        from application.ports import ApprovalQuery
        from application.services.approval_service import ApprovalService
        from infrastructure.persistence.local import LocalArtifactRepository
        from infrastructure.review.local import LocalReviewGate
        from creator_console.audit import AuditLog
        audit = AuditLog(storage, ownership)
        artifacts = LocalArtifactRepository(storage, ownership, audit)
        gate = LocalReviewGate(artifacts=artifacts, ownership=ownership,
                               source=None)
        svc = ApprovalService(artifacts=artifacts, audit=audit,
                              ownership=ownership, review_gate=gate)
        assert isinstance(svc, ApprovalQuery)

    def test_provider_is_injected_not_imported(self):
        """The service reaches YouTube only through the injected provider or
        the lazy publish_youtube construction -- no HTTP/Sdk imports."""
        import inspect
        import application.services.publish_service as mod
        src = inspect.getsource(mod)
        assert "googleapiclient" not in src
        assert "requests." not in src


# ---------------------------------------------------------------------------
# Milestone 7 — job dispatch + upload reliability
# ---------------------------------------------------------------------------

def make_app(tmp_path, provider):
    """A real ConsoleApp wired to a fake publish provider."""
    from creator_console import fixtures
    from creator_console.app import ConsoleApp, ConsoleConfig
    from application.services.publish_service import PublishService

    app = ConsoleApp(ConsoleConfig(
        storage_root=tmp_path / "console", inline_jobs=True,
        secret_getter=fixtures.fake_secret_getter(),
        **fixtures.fixture_client_factories()))

    def publish_factory(**kwargs):
        if kwargs.get("provider") is None:
            kwargs["provider"] = provider
        return PublishService(**kwargs)

    app.publish_service_cls = publish_factory
    return app


def write_visibility_approved_manifest(service, remote_id="vid123"):
    service.write_artifact("publish_manifest", {
        "artifact_kind": "publish_manifest",
        "status": "review_required",
        "platform": "youtube",
        "upload": {"status": "review_required", "remote_id": remote_id,
                   "visibility": "private",
                   "remote_url": f"https://youtu.be/{remote_id}"},
        "visibility_approval": {
            "approved": True, "approved_by": "reviewer",
            "target_visibility": "public", "publish_at": None,
            "reviewed_remote_id": remote_id,
        },
    }, actor="reviewer")


class TestVisibilityJobDispatch:
    """Defect A: the generic executor passes `job_id`; the old signature
    rejected it, so every `visibility_change` job died at dispatch."""

    def test_job_dispatch_reaches_the_method(self, tmp_path, provider):
        app = make_app(tmp_path, provider)
        ownership = Ownership("usr_test", "wsp_test", "prj_vis")
        write_visibility_approved_manifest(app.service(ownership))
        record, created = app.submit_job(ownership, "visibility_change",
                                         params={}, submitted_by="reviewer")
        assert created
        record = app.job_store(ownership).get(record["job_id"])
        # The fix makes this COMPLETED. Pre-fix the record was STATUS_FAILED
        # with 'unexpected keyword argument job_id'.
        assert record["status"] == "completed"
        assert provider.visibility_calls == [
            {"remote_id": "vid123", "visibility": "public", "publish_at": None}]

    def test_job_dispatch_audit_and_manifest(self, tmp_path, provider):
        app = make_app(tmp_path, provider)
        ownership = Ownership("usr_test", "wsp_test", "prj_vis2")
        service = app.service(ownership)
        write_visibility_approved_manifest(service)
        record, _ = app.submit_job(ownership, "visibility_change",
                                   params={}, submitted_by="reviewer")
        record = app.job_store(ownership).get(record["job_id"])
        assert record["status"] == "completed"
        manifest = service.load_artifact("publish_manifest")["data"]
        assert manifest["current_visibility"] == "public"
        calls = [e for e in service.audit.events()
                 if e["event_type"] == "provider.call"]
        assert calls[0]["detail"]["phase"] == "visibility_change"
        assert calls[0]["job_id"] == record["job_id"]

    def test_direct_call_still_works(self, publishable, provider):
        service, _video, _metadata = publishable
        approved_intent(service)
        service.run_publish_upload(actor="reviewer")
        service.approve_publish_visibility(
            target_visibility="public", reviewed_remote_id="fakeVideoId123",
            approved_by="reviewer")
        manifest = service.run_visibility_change(actor="reviewer")
        assert manifest["current_visibility"] == "public"
        # and with an explicit job_id, as the executor passes it
        service.approve_publish_visibility(
            target_visibility="unlisted", reviewed_remote_id="fakeVideoId123",
            approved_by="reviewer")
        manifest = service.run_visibility_change(job_id="job_x",
                                                 actor="reviewer")
        assert manifest["current_visibility"] == "unlisted"

    def test_unauthorized_visibility_job_never_reaches_provider(
            self, tmp_path, provider):
        app = make_app(tmp_path, provider)
        ownership = Ownership("usr_test", "wsp_test", "prj_vis3")
        app.service(ownership).write_artifact("publish_manifest", {
            "artifact_kind": "publish_manifest",
            "status": "review_required", "platform": "youtube",
            "upload": {"status": "review_required", "remote_id": "vid9",
                       "visibility": "private"},
        }, actor="reviewer")
        record, _ = app.submit_job(ownership, "visibility_change",
                                   params={}, submitted_by="reviewer")
        record = app.job_store(ownership).get(record["job_id"])
        assert record["status"] == "failed"
        assert provider.visibility_calls == []

    def test_duplicate_job_submission_is_deduplicated(self, tmp_path, provider):
        app = make_app(tmp_path, provider)
        ownership = Ownership("usr_test", "wsp_test", "prj_vis5")
        write_visibility_approved_manifest(app.service(ownership))
        first, created1 = app.submit_job(ownership, "visibility_change",
                                         params={}, submitted_by="reviewer")
        second, created2 = app.submit_job(ownership, "visibility_change",
                                          params={}, submitted_by="reviewer")
        assert created1 and not created2
        assert first["job_id"] == second["job_id"]
        assert len(provider.visibility_calls) == 1

    def test_provider_failure_job_fails_not_succeeds(self, tmp_path):
        provider = FakePublishProvider(visibility_result=publish.PublishResult(
            platform=publish.PLATFORM_YOUTUBE, status="failed",
            status_reason="saglayici hatasi"))
        app = make_app(tmp_path, provider)
        ownership = Ownership("usr_test", "wsp_test", "prj_vis4")
        write_visibility_approved_manifest(app.service(ownership))
        record, _ = app.submit_job(ownership, "visibility_change",
                                   params={}, submitted_by="reviewer")
        record = app.job_store(ownership).get(record["job_id"])
        assert record["status"] == "failed"
        assert provider.visibility_calls  # reached, reported failure


class TestUploadAttemptMarker:
    """Defect B: a persisted in-flight marker makes an uncertain outcome
    fail CLOSED -- the same intent is never auto-uploaded twice."""

    def test_marker_persistence_failure_prevents_upload(self, publishable,
                                                        provider):
        from creator_console.faults import patch_atomic_write
        service, _video, _metadata = publishable
        approved_intent(service)
        with patch_atomic_write(match="publish_intent.json"):
            with pytest.raises(OSError):
                service.run_publish_upload(actor="reviewer")
        # The marker write failed -> the provider must never have been called.
        assert provider.upload_calls == []

    def test_marker_write_permission_denied_prevents_upload(self, publishable,
                                                            provider):
        from creator_console.faults import patch_atomic_write, permission_denied
        service, _video, _metadata = publishable
        approved_intent(service)
        marker = service.load_artifact("publish_intent")["relative_path"]
        with patch_atomic_write(exception=permission_denied(marker),
                                match="publish_intent.json"):
            with pytest.raises(OSError):
                service.run_publish_upload(actor="reviewer")
        assert provider.upload_calls == []
        # and nothing claims an attempt happened
        intent = service.load_artifact("publish_intent")["data"]
        assert not intent.get("upload_attempt")

    def test_marker_partial_write_blocks_upload(self, publishable, provider):
        """A truncated marker file must read back as corrupt (ArtifactProblem),
        not be half-trusted -- and the upload never starts."""
        from creator_console.faults import patch_atomic_write
        service, _video, _metadata = publishable
        approved_intent(service)
        with patch_atomic_write(match="publish_intent.json", partial_bytes=40):
            with pytest.raises(OSError):
                service.run_publish_upload(actor="reviewer")
        assert provider.upload_calls == []
        loaded = service.load_artifact("publish_intent")
        assert loaded["problem"]  # corrupt tail is reported, not parsed

    def test_remote_success_manifest_write_failure_blocks_retry(
            self, publishable, provider):
        """The crash window: upload succeeded remotely, manifest write failed.
        The retry must refuse BEFORE the provider -- no duplicate upload."""
        from creator_console.faults import patch_atomic_write
        service, _video, _metadata = publishable
        approved_intent(service)
        with patch_atomic_write(match="publish_manifest.json"):
            with pytest.raises(OSError):
                service.run_publish_upload(actor="reviewer")
        assert len(provider.upload_calls) == 1
        with pytest.raises(PipelineServiceError, match="yukleme denemesi"):
            service.run_publish_upload(actor="reviewer")
        # No second upload: the blocked retry never reached the provider.
        assert len(provider.upload_calls) == 1
        # The attempt record preserved the observed remote id for manual
        # reconciliation -- evidence, not a fabricated success.
        attempt = service.load_artifact("publish_intent")["data"]["upload_attempt"]
        assert attempt["status"] == "uncertain"
        assert attempt["observed_remote_id"] == "fakeVideoId123"

    def test_provider_exception_marks_attempt_uncertain(self, publishable,
                                                        provider):
        class ExplodingProvider(FakePublishProvider):
            def upload_private(self, video_path, metadata, target,
                               progress_callback=None):
                self.upload_calls.append({"video_path": str(video_path)})
                raise RuntimeError("baglanti koptu mid-upload")

        exploding = ExplodingProvider()
        service = publishable[0]
        service.publish._provider = exploding
        approved_intent(service)
        with pytest.raises(RuntimeError):
            service.run_publish_upload(actor="reviewer")
        attempt = service.load_artifact("publish_intent")["data"]["upload_attempt"]
        assert attempt["status"] == "uncertain"
        with pytest.raises(PipelineServiceError, match="yukleme denemesi"):
            service.run_publish_upload(actor="reviewer")
        assert len(exploding.upload_calls) == 1

    def test_reported_failure_blocks_retry_too(self, publishable):
        """A `status=failed` result is ambiguous (the chunk may have landed):
        the attempt is recorded and the intent is spent."""
        failing = FakePublishProvider(upload_result=publish.PublishResult(
            platform=publish.PLATFORM_YOUTUBE, status="failed",
            status_reason="resumable oturum kirildi",
            error="upload_reset"))
        service, _video, _metadata = publishable
        service.publish._provider = failing
        approved_intent(service)
        manifest = service.run_publish_upload(actor="reviewer")
        assert manifest["upload"]["status"] == "failed"
        with pytest.raises(PipelineServiceError, match="yukleme denemesi"):
            service.run_publish_upload(actor="reviewer")
        assert len(failing.upload_calls) == 1

    def test_concurrent_second_attempt_is_blocked(self, publishable, provider):
        """While attempt #1 is inside the provider, a second caller sees the
        persisted in-flight marker and refuses."""
        import threading
        service, _video, _metadata = publishable
        approved_intent(service)
        entered = threading.Event()
        release = threading.Event()

        original_upload = provider.upload_private

        def gated(*a, **kw):
            entered.set()
            release.wait(timeout=10)
            return original_upload(*a, **kw)

        provider.upload_private = gated
        errors, results = [], []

        def run():
            try:
                results.append(service.run_publish_upload(actor="reviewer"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        first = threading.Thread(target=run)
        first.start()
        assert entered.wait(timeout=10)
        second = threading.Thread(target=run)
        second.start()
        second.join(timeout=10)
        release.set()
        first.join(timeout=10)
        assert len(errors) == 1 and "yukleme denemesi" in str(errors[0])
        assert len(results) == 1
        assert len(provider.upload_calls) == 1

    def test_legacy_spent_intent_without_marker_is_still_blocked(
            self, publishable, provider):
        """Pre-M7 artifacts: upload_performed=True with no upload_attempt
        field. The dedup + canonical gate still refuse."""
        service, _video, _metadata = publishable
        approved_intent(service)
        intent = service.load_artifact("publish_intent")["data"]
        intent["upload_performed"] = True
        service.write_artifact("publish_intent", intent, actor="tester")
        with pytest.raises((PipelineServiceError, PermissionError)):
            service.run_publish_upload(actor="reviewer")
        assert provider.upload_calls == []
