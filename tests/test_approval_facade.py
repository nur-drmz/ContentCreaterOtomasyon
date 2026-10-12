"""Characterization + negative tests for approval on the façade.

Written BEFORE the ApprovalService extraction. These pin the invariants that
matter most in this repository: no machine self-approval, content-bound
SHA-256, stale approvals stop binding, rights prerequisites are mandatory, and
content approval is not publication authority.

No providers and no renders: artifacts are written directly with realistic
shapes, and the two file-backed kinds (voiceover/render) use real small files
so `sha256_file` has something genuine to digest.
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
from local_pipeline import approval as approval_module

SOURCE_BYTES = b"\x00\x01synthetic-source" * 512

PROBE_INFO = {
    "streams": [{"codec_type": "video", "width": 1920, "height": 1080},
                {"codec_type": "audio"}],
    "format": {"duration": "180.0"},
}


class FakeSecrets:
    def reference(self, kind):
        return type("Ref", (), {"configured": False})()


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


@pytest.fixture
def planned(service):
    """A project with a source, an assessment and an unapproved plan."""
    service.create_project(title="Onay", created_by="tester")
    service.register_source_video(filename="src.mp4", payload=SOURCE_BYTES,
                                  registered_by="tester")
    service.run_source_assessment(actor="tester")
    plan = service.create_processing_plan(
        mode="direct_clip_series", selected_by="tester", output_count=1)
    return service, plan


def complete_copyright(service):
    """Satisfy the real rights checklist (never bypass it)."""
    for item in service.copyright_review_state()["items"]:
        service.set_copyright_review_item(item["key"], checked=True,
                                           reviewed_by="reviewer")


@pytest.fixture
def rights_cleared(planned):
    service, plan = planned
    complete_copyright(service)
    return service, plan


def plan_digest(service):
    return service.approval_state("processing_plan")["content_sha256"]


# ---------------------------------------------------------------------------
# Invariant A — no machine self-approval
# ---------------------------------------------------------------------------

class TestNoMachineSelfApproval:
    def test_fresh_plan_is_not_approved(self, planned):
        _service, plan = planned
        assert plan["status"] != "approved"
        assert plan["human_approval"]["approved"] is False
        assert plan["human_approval"]["approved_content_sha256"] is None

    def test_assessment_does_not_approve_anything(self, planned):
        service, _plan = planned
        assert service.approval_state("processing_plan")["approved"] is False

    def test_generated_script_is_not_approved(self, rights_cleared):
        service, _plan = rights_cleared
        service.write_artifact("selected_script", {
            "status": "review_required", "provider_attempts": [],
            "script_text": "Makine tarafindan uretilmis metin.",
        }, actor="console")
        state = service.approval_state("script")
        assert state["approved"] is False
        assert state["status"] == "review_required"

    def test_no_approval_without_an_explicit_call(self, rights_cleared):
        service, _plan = rights_cleared
        types = [e["event_type"] for e in service.audit.events()]
        assert "approval.granted" not in types


# ---------------------------------------------------------------------------
# Invariant B — approval is content-bound
# ---------------------------------------------------------------------------

class TestContentBoundApproval:
    def test_approval_with_the_echoed_digest_succeeds(self, rights_cleared):
        service, _plan = rights_cleared
        digest = plan_digest(service)
        approved = service.approve("processing_plan", reviewed_sha256=digest,
                                    approved_by="reviewer", notes="tamam")
        assert approved["status"] == "approved"
        assert approved["human_approval"]["approved"] is True
        assert approved["human_approval"]["approved_by"] == "reviewer"
        assert approved["human_approval"]["approved_content_sha256"] == digest
        assert approved["human_approval"]["notes"] == "tamam"

    def test_digest_is_the_canonical_decision_hash(self, rights_cleared):
        service, _plan = rights_cleared
        loaded = service.load_artifact("processing_plan")["data"]
        assert plan_digest(service) == approval_module.content_hash_for(
            "processing_plan", loaded)

    def test_wrong_digest_is_refused(self, rights_cleared):
        service, _plan = rights_cleared
        with pytest.raises(PipelineServiceError):
            service.approve("processing_plan",
                             reviewed_sha256=hashlib.sha256(b"nope").hexdigest(),
                             approved_by="reviewer")

    def test_refusal_persists_nothing(self, rights_cleared):
        service, _plan = rights_cleared
        with pytest.raises(PipelineServiceError):
            service.approve("processing_plan", reviewed_sha256="deadbeef",
                             approved_by="reviewer")
        assert service.load_artifact("processing_plan")["data"][
            "human_approval"]["approved"] is False

    def test_refusal_audits_rejected_not_granted(self, rights_cleared):
        service, _plan = rights_cleared
        with pytest.raises(PipelineServiceError):
            service.approve("processing_plan", reviewed_sha256="deadbeef",
                             approved_by="reviewer")
        events = service.audit.events()
        assert events[0]["event_type"] == "approval.rejected"
        assert events[0]["actor"] == "reviewer"
        assert events[0]["detail"]["kind"] == "processing_plan"
        assert "approval.granted" not in [e["event_type"] for e in events]

    def test_granted_audit_detail(self, rights_cleared):
        service, _plan = rights_cleared
        digest = plan_digest(service)
        service.approve("processing_plan", reviewed_sha256=digest,
                         approved_by="reviewer")
        events = service.audit.events()
        assert events[0]["event_type"] == "approval.granted"
        assert events[0]["actor"] == "reviewer"
        assert events[0]["detail"] == {"kind": "processing_plan", "slot": 1,
                                        "approved_content_sha256": digest}
        # The artifact is durable before the event describing it.
        assert events[1]["event_type"] == "artifact.written"

    def test_repeated_approval_with_the_same_digest_is_idempotent_in_effect(
            self, rights_cleared):
        service, _plan = rights_cleared
        digest = plan_digest(service)
        first = service.approve("processing_plan", reviewed_sha256=digest,
                                 approved_by="reviewer")
        second = service.approve("processing_plan", reviewed_sha256=digest,
                                  approved_by="reviewer2")
        assert second["human_approval"]["approved"] is True
        assert (second["human_approval"]["approved_content_sha256"]
                == first["human_approval"]["approved_content_sha256"])

    def test_script_approval_binds_the_text(self, rights_cleared):
        service, _plan = rights_cleared
        service.write_artifact("selected_script", {
            "status": "review_required", "provider_attempts": [],
            "script_text": "Onaylanacak metin.",
        }, actor="console")
        digest = service.approval_state("script")["content_sha256"]
        assert digest == approval_module.sha256_text("Onaylanacak metin.")
        approved = service.approve("script", reviewed_sha256=digest,
                                    approved_by="reviewer")
        assert approved["human_approval"]["approved"] is True


# ---------------------------------------------------------------------------
# Invariant C — stale approval is invalid
# ---------------------------------------------------------------------------

class TestStaleApprovalIsInvalid:
    def test_stale_digest_cannot_approve_modified_content(self, rights_cleared):
        service, _plan = rights_cleared
        stale = plan_digest(service)
        service.update_processing_plan(updated_by="editor", output_count=2)
        with pytest.raises(PipelineServiceError):
            service.approve("processing_plan", reviewed_sha256=stale,
                             approved_by="reviewer")

    def test_approval_does_not_survive_a_plan_edit(self, rights_cleared):
        service, _plan = rights_cleared
        service.approve("processing_plan", reviewed_sha256=plan_digest(service),
                         approved_by="reviewer")
        assert service.approval_state("processing_plan")["approved"] is True
        service.update_processing_plan(updated_by="editor", output_count=2)
        state = service.approval_state("processing_plan")
        assert state["approved"] is False
        assert state["valid"] is False

    def test_plan_edit_records_exactly_one_invalidation_event(self, rights_cleared):
        service, _plan = rights_cleared
        service.approve("processing_plan", reviewed_sha256=plan_digest(service),
                         approved_by="reviewer")
        before = sum(1 for e in service.audit.events()
                     if e["event_type"] == "approval.invalidated")
        service.update_processing_plan(updated_by="editor", output_count=2)
        after = [e for e in service.audit.events()
                 if e["event_type"] == "approval.invalidated"]
        assert len(after) - before == 1
        assert after[0]["detail"]["kind"] == "processing_plan"
        assert after[0]["detail"]["reason"] == "plan edited"
        assert after[0]["detail"]["previous_approved_content_sha256"]
        assert after[0]["actor"] == "editor"

    def test_editing_an_unapproved_plan_records_no_invalidation(self, rights_cleared):
        service, _plan = rights_cleared
        service.update_processing_plan(updated_by="editor", output_count=2)
        assert "approval.invalidated" not in [
            e["event_type"] for e in service.audit.events()]

    def test_script_approval_broken_by_text_edit(self, rights_cleared):
        service, _plan = rights_cleared
        service.write_artifact("selected_script", {
            "status": "review_required", "provider_attempts": [],
            "script_text": "ilk metin",
        }, actor="console")
        service.approve("script",
                         reviewed_sha256=service.approval_state("script")["content_sha256"],
                         approved_by="reviewer")
        assert service.approval_state("script")["valid"] is True
        artifact = service.load_artifact("selected_script")["data"]
        artifact["script_text"] = "degistirilmis metin"
        service.write_artifact("selected_script", artifact, actor="console")
        state = service.approval_state("script")
        assert state["approved"] is True       # the record of approval survives
        assert state["valid"] is False         # but it no longer binds

    def test_approval_record_is_never_deleted(self, rights_cleared):
        """Reported, never erased: 'no longer valid' != 'never happened'."""
        service, _plan = rights_cleared
        service.approve("processing_plan", reviewed_sha256=plan_digest(service),
                         approved_by="reviewer")
        service.update_processing_plan(updated_by="editor", output_count=2)
        types = [e["event_type"] for e in service.audit.events()]
        assert "approval.granted" in types
        assert "approval.invalidated" in types


# ---------------------------------------------------------------------------
# Invariant E — rights prerequisites cannot be bypassed
# ---------------------------------------------------------------------------

class TestRightsPrerequisite:
    def test_plan_approval_denied_without_the_copyright_checklist(self, planned):
        service, _plan = planned
        with pytest.raises(PipelineServiceError, match="ONAY REDDEDILDI"):
            service.approve("processing_plan",
                             reviewed_sha256=plan_digest(service),
                             approved_by="reviewer")

    def test_denial_happens_before_any_write(self, planned):
        service, _plan = planned
        with pytest.raises(PipelineServiceError):
            service.approve("processing_plan",
                             reviewed_sha256=plan_digest(service),
                             approved_by="reviewer")
        assert service.load_artifact("processing_plan")["data"][
            "human_approval"]["approved"] is False

    def test_partial_checklist_still_denies(self, planned):
        service, _plan = planned
        items = service.copyright_review_state()["items"]
        service.set_copyright_review_item(items[0]["key"], checked=True,
                                           reviewed_by="reviewer")
        with pytest.raises(PipelineServiceError, match="ONAY REDDEDILDI"):
            service.approve("processing_plan",
                             reviewed_sha256=plan_digest(service),
                             approved_by="reviewer")

    def test_open_copyright_revision_denies(self, rights_cleared):
        service, _plan = rights_cleared
        service.request_copyright_revision(requested_by="reviewer",
                                            notes="kaynak belirsiz")
        with pytest.raises(PipelineServiceError, match="ONAY REDDEDILDI"):
            service.approve("processing_plan",
                             reviewed_sha256=plan_digest(service),
                             approved_by="reviewer")

    def test_rights_gate_applies_only_to_the_plan_kind(self, planned):
        """Script approval has its own gates; it must not inherit the plan's."""
        service, _plan = planned
        service.write_artifact("selected_script", {
            "status": "review_required", "provider_attempts": [],
            "script_text": "metin",
        }, actor="console")
        approved = service.approve(
            "script",
            reviewed_sha256=service.approval_state("script")["content_sha256"],
            approved_by="reviewer")
        assert approved["human_approval"]["approved"] is True


# ---------------------------------------------------------------------------
# Eligibility / error handling
# ---------------------------------------------------------------------------

class TestEligibilityAndErrors:
    def test_unknown_kind_is_refused(self, rights_cleared):
        service, _plan = rights_cleared
        with pytest.raises(PipelineServiceError, match="bilinmeyen onay turu"):
            service.approve("telepathy", reviewed_sha256="x",
                             approved_by="reviewer")

    def test_missing_artifact_raises_artifact_problem(self, rights_cleared):
        service, _plan = rights_cleared
        with pytest.raises(ArtifactProblem, match="bulunamadi"):
            service.approve("script", reviewed_sha256="x", approved_by="r")

    def test_corrupt_artifact_raises_artifact_problem(self, rights_cleared,
                                                      storage, ownership):
        service, _plan = rights_cleared
        storage.resolve(ownership, "results/processing_plan.json").write_text(
            "{broken", encoding="utf-8")
        with pytest.raises(ArtifactProblem, match="gecerli JSON degil"):
            service.approve("processing_plan", reviewed_sha256="x",
                             approved_by="r")

    def test_unreviewable_artifact_reports_a_blocker(self, rights_cleared):
        """A prompt-only script package has nothing to approve."""
        service, _plan = rights_cleared
        service.write_artifact("selected_script", {
            "status": "review_required", "provider_attempts": [],
        }, actor="console")
        state = service.approval_state("script")
        assert state["reviewable"] is False
        assert state["blocker"]
        assert state["content_sha256"] is None

    def test_out_of_range_slot_is_refused(self, rights_cleared):
        service, _plan = rights_cleared
        with pytest.raises(PipelineServiceError, match="bu planda yok"):
            service.approve("render", reviewed_sha256="x", approved_by="r",
                             slot=47)

    def test_state_of_absent_artifact(self, rights_cleared):
        service, _plan = rights_cleared
        state = service.approval_state("render")
        assert state["present"] is False
        assert state["approved"] is False
        assert state["reason"] == "artefakt henuz uretilmedi"

    def test_state_of_corrupt_artifact(self, rights_cleared, storage, ownership):
        service, _plan = rights_cleared
        storage.resolve(ownership, "results/processing_plan.json").write_text(
            "{broken", encoding="utf-8")
        state = service.approval_state("processing_plan")
        assert state["present"] is True
        assert state["valid"] is False
        assert state["approved"] is False
        assert state["reason"]


# ---------------------------------------------------------------------------
# Chain / invalidation reporting
# ---------------------------------------------------------------------------

class TestApprovalChain:
    def test_chain_for_direct_clip_series(self, rights_cleared):
        service, _plan = rights_cleared
        chain = service.approval_chain()
        assert chain["processing_plan"]["in_chain"] is True
        assert chain["transcript"]["in_chain"] is True
        assert chain["render"]["in_chain"] is True
        # script/voiceover are narrated-only and must not block a clip Short.
        assert chain["script"]["in_chain"] is False
        assert chain["voiceover"]["in_chain"] is False

    def test_every_registered_kind_is_present(self, rights_cleared):
        service, _plan = rights_cleared
        chain = service.approval_chain()
        assert set(chain) == set(service.APPROVAL_ARTIFACTS)

    def test_unapproved_plan_marks_downstream_upstream_invalid(self, rights_cleared):
        service, _plan = rights_cleared
        chain = service.approval_chain()
        assert chain["processing_plan"]["valid"] is False
        assert chain["render"]["upstream_invalid"]

    def test_approved_plan_clears_its_own_upstream_problem(self, rights_cleared):
        service, _plan = rights_cleared
        service.approve("processing_plan", reviewed_sha256=plan_digest(service),
                         approved_by="reviewer")
        chain = service.approval_chain()
        assert chain["processing_plan"]["effective_valid"] is True
        assert chain["processing_plan"]["upstream_invalid"] == []

    def test_invalidated_approvals_reports_a_stale_approval(self, rights_cleared):
        service, _plan = rights_cleared
        service.approve("processing_plan", reviewed_sha256=plan_digest(service),
                         approved_by="reviewer")
        assert service.invalidated_approvals() == []
        # Mutate the decision underneath the approval, bypassing the edit path
        # so the approval block itself survives and genuinely goes stale.
        artifact = service.load_artifact("processing_plan")["data"]
        artifact["decision"]["target_duration_sec"] = 45
        service.write_artifact("processing_plan", artifact, actor="console")
        stale = service.invalidated_approvals()
        assert [s["kind"] for s in stale] == ["processing_plan"]
        assert stale[0]["approved"] is True
        assert stale[0]["effective_valid"] is False


# ---------------------------------------------------------------------------
# Invariant F — approval is not publication
# ---------------------------------------------------------------------------

class TestApprovalIsNotPublication:
    def test_content_approval_does_not_create_publication_state(self, rights_cleared):
        service, _plan = rights_cleared
        service.approve("processing_plan", reviewed_sha256=plan_digest(service),
                         approved_by="reviewer")
        assert service.load_artifact("publish_intent")["present"] is False
        assert service.load_artifact("publish_manifest")["present"] is False
        assert service.load_artifact("publication_metadata")["present"] is False

    def test_publication_kinds_are_not_in_the_content_registry(self, service):
        """Publishing uses separate state machines and separate methods."""
        registry = set(service.APPROVAL_ARTIFACTS)
        assert registry == {"processing_plan", "script", "voiceover",
                            "transcript", "render"}
        for publication_kind in ("publish_intent", "publish_manifest",
                                  "publication_metadata"):
            assert publication_kind not in registry

    def test_content_approval_route_cannot_approve_publication(self, rights_cleared):
        service, _plan = rights_cleared
        for publication_kind in ("publish_intent", "publish_manifest",
                                  "publication_metadata"):
            with pytest.raises(PipelineServiceError, match="bilinmeyen onay turu"):
                service.approve(publication_kind, reviewed_sha256="x",
                                 approved_by="reviewer")

    def test_publication_approval_methods_still_exist_separately(self, service):
        for name in ("approve_publication_metadata", "approve_publish_intent",
                      "approve_publish_visibility"):
            assert callable(getattr(service, name))

    def test_project_stays_not_uploaded_after_content_approval(self, rights_cleared):
        service, _plan = rights_cleared
        approved = service.approve("processing_plan",
                                    reviewed_sha256=plan_digest(service),
                                    approved_by="reviewer")
        assert approved["not_uploaded"] is True


# ---------------------------------------------------------------------------
# Solo creator capability (ADR-006 `solo_allowed`, unauthenticated local mode)
# ---------------------------------------------------------------------------

class TestSoloCreatorApproval:
    def test_the_same_local_actor_may_author_and_approve(self, rights_cleared):
        """Current local behaviour: solo creators can approve their own work.

        No maker-checker policy is enforced on spoofable actor strings; see
        APPROVAL_SERVICE.md 'Reviewer policy'.
        """
        service, _plan = rights_cleared
        approved = service.approve("processing_plan",
                                    reviewed_sha256=plan_digest(service),
                                    approved_by="tester")  # same actor as author
        assert approved["human_approval"]["approved"] is True
        assert approved["human_approval"]["approved_by"] == "tester"

    def test_actor_string_is_recorded_verbatim_not_authenticated(self, rights_cleared):
        service, _plan = rights_cleared
        approved = service.approve("processing_plan",
                                    reviewed_sha256=plan_digest(service),
                                    approved_by="  reviewer  ")
        recorded = approved["human_approval"]["approved_by"]
        assert "reviewer" in recorded


# ---------------------------------------------------------------------------
# Facade parity: PipelineService delegates to ApprovalService over real adapters
# ---------------------------------------------------------------------------

def _direct_approval_service(storage, ownership, audit=None):
    """An ApprovalService wired by hand -- NO PipelineService anywhere."""
    from application.services import ApprovalService, SourceService
    from creator_console.audit import AuditLog
    from creator_console.ids import new_id, safe_filename
    from application.media_binaries import MediaBinaries
    from infrastructure.persistence.local import LocalArtifactRepository
    from infrastructure.progress.local import LocalProgressFactory
    from infrastructure.review.local import LocalReviewGate

    storage.ensure_namespace(ownership)
    audit = audit or AuditLog(storage, ownership)
    artifacts = LocalArtifactRepository(storage, ownership, audit)
    source = SourceService(
        artifacts=artifacts, audit=audit, namespace=storage,
        ownership=ownership, binaries=MediaBinaries("/x/ffmpeg", "/x/ffprobe"),
        safe_filename=safe_filename, new_id=new_id,
        progress=LocalProgressFactory(storage, ownership))
    return ApprovalService(
        artifacts=artifacts, audit=audit, ownership=ownership,
        review_gate=LocalReviewGate(artifacts, ownership, source))


class TestApprovalServiceWiring:
    def test_facade_holds_an_approval_service(self, service):
        from application.services import ApprovalService

        assert isinstance(service.approval, ApprovalService)
        assert not isinstance(service.approval, PipelineService)

    def test_analysis_depends_on_the_approval_port_not_the_facade(self, service):
        """The invalidation AnalysisService triggers is recorded by the
        approval owner, not by a second implementation inside analysis."""
        assert service.analysis._approvals is service.approval

    def test_approval_service_satisfies_the_invalidator_port(self, service):
        from application.ports import ApprovalInvalidator

        assert isinstance(service.approval, ApprovalInvalidator)

    def test_review_gate_satisfies_its_port(self, service):
        from application.ports import ReviewGate

        assert isinstance(service._review_gate, ReviewGate)

    def test_delegate_spy_approve(self, rights_cleared, monkeypatch):
        service, _plan = rights_cleared
        seen = {}
        monkeypatch.setattr(service.approval, "approve",
                            lambda *a, **kw: seen.update(kw) or {"ok": True})
        result = service.approve("processing_plan", reviewed_sha256="d",
                                  approved_by="r", notes="n", slot=3)
        assert result == {"ok": True}
        assert seen == {"reviewed_sha256": "d", "approved_by": "r",
                        "notes": "n", "slot": 3}

    def test_delegate_spy_state_chain_invalidated(self, service, monkeypatch):
        calls = []
        monkeypatch.setattr(service.approval, "state",
                            lambda *a, **kw: calls.append(("state", a, kw)) or {})
        monkeypatch.setattr(service.approval, "chain",
                            lambda *a, **kw: calls.append(("chain", a, kw)) or {})
        monkeypatch.setattr(service.approval, "invalidated",
                            lambda *a, **kw: calls.append(("invalidated", a, kw)) or [])
        service.approval_state("render", 2)
        service.approval_chain(4)
        service.invalidated_approvals()
        assert calls == [("state", ("render", 2), {}),
                         ("chain", (4,), {}),
                         ("invalidated", (), {})]

    def test_review_state_delegates_to_the_gate(self, service, monkeypatch):
        calls = []
        for facade_method, gate_method, arg in (
                ("copyright_review_state", "copyright_review_state", None),
                ("voice_review_state", "voice_review_state", None),
                ("render_review_state", "render_review_state", 2),
                ("render_video_digest", "render_video_digest", 5),
                ("source_fingerprint", "source_fingerprint", None),
                ("voice_audio_digest", "voice_audio_digest", None)):
            monkeypatch.setattr(
                service._review_gate, gate_method,
                (lambda m: (lambda *a, **kw: calls.append(m) or {}))(gate_method))
            getattr(service, facade_method)(
                *(() if arg is None else (arg,)))
        assert calls == ["copyright_review_state", "voice_review_state",
                         "render_review_state", "render_video_digest",
                         "source_fingerprint", "voice_audio_digest"]

    def test_direct_service_approves_identically(self, rights_cleared):
        """A hand-wired ApprovalService grants the SAME artifact -- no facade."""
        service, _plan = rights_cleared
        digest = plan_digest(service)
        direct = _direct_approval_service(service.storage, service.ownership,
                                          audit=service.audit)
        approved = direct.approve("processing_plan", reviewed_sha256=digest,
                                  approved_by="reviewer")
        assert approved["human_approval"]["approved"] is True
        assert approved["human_approval"]["approved_content_sha256"] == digest
        # The facade observes the grant the service made: one artifact store.
        assert service.approval_state("processing_plan")["approved"] is True

    def test_direct_service_enforces_the_rights_gate(self, planned):
        """Rights are checked through the gate adapter, not a facade lambda."""
        service, _plan = planned  # a REAL plan; the rights checklist is empty
        direct = _direct_approval_service(service.storage, service.ownership,
                                          audit=service.audit)
        digest = approval_module.content_hash_for(
            "processing_plan",
            service.load_artifact("processing_plan")["data"])
        with pytest.raises(PipelineServiceError, match="ONAY REDDEDILDI"):
            direct.approve("processing_plan", reviewed_sha256=digest,
                           approved_by="reviewer")


# ---------------------------------------------------------------------------
# The invalidation port (AnalysisService -> ApprovalService)
# ---------------------------------------------------------------------------

class TestInvalidationPort:
    def test_record_invalidation_writes_no_event_when_not_approved(self, service):
        recorded = service.approval.record_invalidation(
            "processing_plan", artifact="results/processing_plan.json",
            actor="editor", reason="plan edited",
            previous_approval={"approved": False})
        assert recorded is False
        assert service.audit.events() == []

    def test_record_invalidation_detail_shape_is_unchanged(self, service):
        recorded = service.approval.record_invalidation(
            "processing_plan", artifact="results/processing_plan.json",
            actor="editor", reason="plan edited",
            previous_approval={"approved": True,
                               "approved_content_sha256": "abc123"})
        assert recorded is True
        (event,) = service.audit.events()
        assert event["event_type"] == "approval.invalidated"
        assert event["actor"] == "editor"
        assert event["artifact"] == "results/processing_plan.json"
        assert event["detail"] == {
            "kind": "processing_plan", "reason": "plan edited",
            "previous_approved_content_sha256": "abc123"}
        # Slot is per-slot metadata; a project-wide invalidation must not
        # suddenly gain one (detail-shape parity with the pre-extraction code).
        assert "slot" not in event["detail"]

    def test_invalidation_never_deletes_the_original_grant(self, rights_cleared):
        service, _plan = rights_cleared
        service.approve("processing_plan", reviewed_sha256=plan_digest(service),
                         approved_by="reviewer")
        service.update_processing_plan(updated_by="editor", output_count=2)
        types = [e["event_type"] for e in service.audit.events()]
        assert "approval.granted" in types      # the grant is still on record
        assert "approval.invalidated" in types  # and so is its invalidation


# ---------------------------------------------------------------------------
# Fault injection must still reach approval persistence
# ---------------------------------------------------------------------------

class TestApprovalWriteFaultCoverage:
    """Approval writes go through `LocalArtifactRepository` (a registered
    atomic-write holder). These prove injected write faults still fire after
    the orchestration moved into ApprovalService."""

    def test_disk_full_during_approval_persistence_raises(self, rights_cleared):
        from creator_console.faults import disk_full, patch_atomic_write

        service, _plan = rights_cleared
        with patch_atomic_write(exception=disk_full("processing_plan.json"),
                                match="processing_plan.json"):
            with pytest.raises(OSError):
                service.approve("processing_plan",
                                 reviewed_sha256=plan_digest(service),
                                 approved_by="reviewer")

    def test_failed_persistence_is_not_reported_as_granted(self, rights_cleared):
        """A crashed write must not leave an `approval.granted` lie behind."""
        from creator_console.faults import disk_full, patch_atomic_write

        service, _plan = rights_cleared
        with patch_atomic_write(exception=disk_full("processing_plan.json"),
                                match="processing_plan.json"):
            with pytest.raises(OSError):
                service.approve("processing_plan",
                                 reviewed_sha256=plan_digest(service),
                                 approved_by="reviewer")
        assert "approval.granted" not in [
            e["event_type"] for e in service.audit.events()]
        assert service.approval_state("processing_plan")["approved"] is False

    def test_permission_denied_during_approval_persistence(self, rights_cleared):
        from creator_console.faults import patch_atomic_write, permission_denied

        service, _plan = rights_cleared
        with patch_atomic_write(exception=permission_denied("processing_plan.json"),
                                match="processing_plan.json"):
            with pytest.raises(PermissionError):
                service.approve("processing_plan",
                                 reviewed_sha256=plan_digest(service),
                                 approved_by="reviewer")

    def test_partial_approval_write_surfaces_on_the_next_read(self, rights_cleared):
        from creator_console.faults import patch_atomic_write

        service, _plan = rights_cleared
        with patch_atomic_write(partial_bytes=20, match="processing_plan.json"):
            with pytest.raises(OSError):
                service.approve("processing_plan",
                                 reviewed_sha256=plan_digest(service),
                                 approved_by="reviewer")
        state = service.approval_state("processing_plan")
        assert state["valid"] is False
        assert state["approved"] is False
        assert state["reason"]  # corruption reported, not a plausible default

    def test_checklist_mutation_writes_are_still_fault_covered(self, rights_cleared):
        """Checklist writes still live on the facade, a registered holder."""
        from creator_console.faults import disk_full, patch_atomic_write

        service, _plan = rights_cleared
        with patch_atomic_write(exception=disk_full("copyright_review.json"),
                                match="copyright_review.json"):
            with pytest.raises(OSError):
                service.set_copyright_review_item(
                    service.copyright_review_state()["items"][0]["key"],
                    checked=True, reviewed_by="reviewer")
