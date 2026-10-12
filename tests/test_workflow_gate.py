"""Workflow-gate characterization, parity and boundary tests.

WHY THIS FILE EXISTS
--------------------
Milestone 9 extracts the plan-derived workflow-eligibility surface
(`plan_gate`, `require_phase`, `plan_decision`, `slot_*`, `require_slot`,
`slot_moment_rank`) out of `PipelineService` into
`application.services.WorkflowGate` behind `ports.WorkflowGateQuery`.

The `TestGateCharacterization` classes pin the PRE-EXTRACTION behaviour using
only the public façade: they must pass unchanged both before and after the
move. `TestGateParity` then proves the direct service call and the façade
delegate agree exactly, and `TestGateBoundaries` proves what the gate must
never do (grant approval, render, publish, write artifacts).
"""
from __future__ import annotations

import inspect

import pytest

from creator_console.audit import AuditLog
from creator_console.ids import Ownership
from creator_console.pipeline_service import (
    ArtifactProblem,
    PipelineService,
    PipelineServiceError,
)
from creator_console.storage import LocalFilesystemStorage
from local_pipeline import source_strategy

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
def with_source(service):
    service.create_project(title="Kapi", created_by="tester")
    service.register_source_video(filename="src.mp4", payload=SOURCE_BYTES,
                                  registered_by="tester")
    return service


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


@pytest.fixture
def planned(with_source):
    """Unapproved direct-mode plan."""
    _make_plan(with_source)
    return with_source


@pytest.fixture
def approved(planned):
    _clear_rights(planned)
    _approve_plan(planned)
    return planned


# ---------------------------------------------------------------------------
# Characterization: gate behaviour as it exists TODAY (all through the facade)
# ---------------------------------------------------------------------------

class TestPlanGateCharacterization:
    def test_no_plan_denies_every_phase(self, with_source):
        for phase in source_strategy.GATED_PHASES:
            allowed, reason = with_source.plan_gate(phase)
            assert allowed is False
            assert "plan" in reason.lower()

    def test_no_plan_require_phase_raises_service_error(self, with_source):
        with pytest.raises(PipelineServiceError):
            with_source.require_phase("render")

    def test_unapproved_plan_denies_every_phase(self, planned):
        for phase in source_strategy.GATED_PHASES:
            allowed, reason = planned.plan_gate(phase)
            assert allowed is False
            assert "onaylanmamis" in reason

    def test_approved_direct_plan_allows_its_phases(self, approved):
        for phase in ("video_analysis", "moment_selection", "transcript",
                      "render"):
            allowed, reason = approved.plan_gate(phase)
            assert allowed is True, (phase, reason)
            assert reason == ""

    def test_approved_direct_plan_denies_generation_phases(self, approved):
        """direct_clip_series makes zero generation-API calls by gate, not by
        convention: script and TTS are denied with the mode reason."""
        allowed, reason = approved.plan_gate("script_generation")
        assert allowed is False and "direct_clip_series" in reason
        allowed, reason = approved.plan_gate("tts")
        assert allowed is False and "direct_clip_series" in reason

    def test_approved_narrated_plan_allows_generation_phases(
            self, storage, ownership):
        """Narrated mode needs a script provider: the assessment marks it
        blocked when no Gemini credential exists, so this project is built on
        a resolver that reports one configured."""
        service = PipelineService(
            storage, ownership, FakeSecrets(configured=True),
            probe_fn=lambda *a: (PROBE_INFO, "ffprobe"),
            ffmpeg_bin="/x/ffmpeg", ffprobe_bin="/x/ffprobe")
        service.create_project(title="Kapi", created_by="tester")
        service.register_source_video(filename="src.mp4", payload=SOURCE_BYTES,
                                      registered_by="tester")
        with_source = service
        _make_plan(with_source, mode="narrated_theory")
        _clear_rights(with_source)
        _approve_plan(with_source)
        for phase in ("video_analysis", "moment_selection",
                      "script_generation", "tts", "render"):
            allowed, reason = with_source.plan_gate(phase)
            assert allowed is True, (phase, reason)
        # ...and the narrated plan denies the source-transcript phase: its
        # captions come from the approved script, not the source's own words.
        allowed, reason = with_source.plan_gate("transcript")
        assert allowed is False and "narrated_theory" in reason

    def test_stale_plan_approval_denies_the_gate(self, approved):
        """An approval binds the decision it was granted over. Editing the
        decision OUT OF BAND -- status stays 'approved' but the digest no
        longer matches -- must re-derive to denied, not trust the status."""
        import json

        assert approved.plan_gate("render")[0] is True
        path = approved.storage.resolve(
            approved.ownership, "results/processing_plan.json")
        plan = json.loads(path.read_text(encoding="utf-8"))
        plan["decision"]["tampered"] = True
        path.write_text(json.dumps(plan), encoding="utf-8")
        allowed, reason = approved.plan_gate("render")
        assert allowed is False
        assert "gecerli degil" in reason

    def test_plan_edit_resets_status_and_denies_the_gate(self, approved):
        """The IN-BAND edit path: `update_processing_plan` returns the plan to
        review_required, which is an unapproved plan -- denied for a different
        reason than the out-of-band tamper above."""
        assert approved.plan_gate("render")[0] is True
        approved.update_processing_plan(updated_by="editor", output_count=2)
        allowed, reason = approved.plan_gate("render")
        assert allowed is False
        assert "onaylanmamis" in reason

    def test_unknown_phase_raises_value_error_not_service_error(self, approved):
        """An unknown phase is a PROGRAMMING error: `ValueError` propagates
        (even before the plan check), it is not reported as a denied phase."""
        with pytest.raises(ValueError, match="bilinmeyen faz"):
            approved.plan_gate("bogus_phase")
        with pytest.raises(ValueError, match="bilinmeyen faz"):
            approved.require_phase("bogus_phase")

    def test_unknown_phase_raises_even_without_a_plan(self, with_source):
        with pytest.raises(ValueError, match="bilinmeyen faz"):
            with_source.plan_gate("bogus_phase")

    def test_corrupt_plan_denies_with_the_artifact_problem(self, planned):
        path = planned.storage.resolve(
            planned.ownership, "results/processing_plan.json")
        path.write_text("{corrupt", encoding="utf-8")
        allowed, reason = planned.plan_gate("render")
        assert allowed is False
        assert "gecerli JSON degil" in reason

    def test_gate_is_read_only(self, approved):
        """Eligibility must not persist artifacts or audit anything."""
        root = approved.storage.namespace_root(approved.ownership)
        files_before = {p for p in root.rglob("*") if p.is_file()}
        events_before = len(approved.audit.events())
        for phase in source_strategy.GATED_PHASES:
            approved.plan_gate(phase)
        assert {p for p in root.rglob("*") if p.is_file()} == files_before
        assert len(approved.audit.events()) == events_before


class TestPlanDerivedQueryCharacterization:
    def test_plan_decision_returns_the_decision_block(self, planned):
        decision = planned.plan_decision()
        assert decision["mode"] == "direct_clip_series"
        assert decision["output_count"] == 1

    def test_plan_decision_is_empty_without_a_plan(self, with_source):
        assert with_source.plan_decision() == {}

    def test_slot_count_is_one_without_a_plan(self, with_source):
        assert with_source.slot_count() == 1
        assert with_source.slot_indexes() == [1]

    def test_slot_count_follows_the_plan_outputs(self, with_source):
        _make_plan(with_source, output_count=3)
        assert with_source.slot_count() == 3
        assert with_source.slot_indexes() == [1, 2, 3]

    def test_require_slot_accepts_defined_slots(self, with_source):
        _make_plan(with_source, output_count=2)
        assert with_source.require_slot(1) == 1
        assert with_source.require_slot(2) == 2

    def test_require_slot_rejects_undefined_slots(self, with_source):
        _make_plan(with_source, output_count=1)
        with pytest.raises(PipelineServiceError, match="planda yok"):
            with_source.require_slot(2)
        with pytest.raises(PipelineServiceError, match="gecersiz"):
            with_source.require_slot("iki")

    def test_slot_moment_rank_reads_the_plan_entry(self, with_source):
        _make_plan(with_source, output_count=2)
        # No video analysis ran, so no moment ranks were pre-assigned -- the
        # entries exist but carry None. The query reports the entry's value
        # for a defined slot and None for a slot the plan does not define.
        assert with_source.slot_moment_rank(1) is None
        assert with_source.slot_moment_rank(2) is None
        assert with_source.slot_moment_rank(99) is None

    def test_slot_moment_rank_is_none_without_a_plan(self, with_source):
        assert with_source.slot_moment_rank(1) is None


# ---------------------------------------------------------------------------
# Parity: the facade delegate and the direct WorkflowGate must agree exactly
# ---------------------------------------------------------------------------

@pytest.fixture
def direct_gate(storage, ownership):
    """A WorkflowGate wired by hand -- NO PipelineService involved anywhere."""
    from application.services import WorkflowGate
    from infrastructure.persistence.local import LocalArtifactRepository

    audit = AuditLog(storage, ownership)
    return WorkflowGate(
        artifacts=LocalArtifactRepository(storage, ownership, audit))


class TestGateParity:
    """Every migrated method: identical observable result through the facade
    and through a standalone gate reading the same artifact store."""

    @pytest.mark.parametrize("phase", list(source_strategy.GATED_PHASES))
    def test_plan_gate_matches_on_an_approved_project(self, phase, approved,
                                                      direct_gate):
        assert approved.plan_gate(phase) == direct_gate.plan_gate(phase)

    def test_require_phase_raises_the_same_error(self, planned, direct_gate):
        for phase in source_strategy.GATED_PHASES:
            with pytest.raises(PipelineServiceError) as facade_exc:
                planned.require_phase(phase)
            with pytest.raises(PipelineServiceError) as gate_exc:
                direct_gate.require_phase(phase)
            assert str(facade_exc.value) == str(gate_exc.value)

    def test_plan_and_slot_queries_match(self, approved, direct_gate):
        assert approved.plan_decision() == direct_gate.plan_decision()
        assert approved.slot_count() == direct_gate.slot_count()
        assert approved.slot_indexes() == direct_gate.slot_indexes()
        for slot in (1, 2, 3):
            assert approved.slot_moment_rank(slot) == \
                direct_gate.slot_moment_rank(slot)

    def test_require_slot_matches_including_the_error(self, approved,
                                                      direct_gate):
        assert approved.require_slot(1) == direct_gate.require_slot(1)
        with pytest.raises(PipelineServiceError) as facade_exc:
            approved.require_slot(99)
        with pytest.raises(PipelineServiceError) as gate_exc:
            direct_gate.require_slot(99)
        assert str(facade_exc.value) == str(gate_exc.value)

    def test_unknown_phase_raises_value_error_on_both(self, approved,
                                                      direct_gate):
        with pytest.raises(ValueError):
            approved.plan_gate("bogus_phase")
        with pytest.raises(ValueError):
            direct_gate.plan_gate("bogus_phase")


# ---------------------------------------------------------------------------
# Boundaries: what the gate may NOT do or know
# ---------------------------------------------------------------------------

class TestGateBoundaries:
    def test_workflow_gate_satisfies_the_port(self, direct_gate):
        from application.ports import WorkflowGateQuery

        assert isinstance(direct_gate, WorkflowGateQuery)

    def test_the_facade_gate_is_the_service_not_the_facade(self, service):
        from application.services import WorkflowGate

        assert isinstance(service.workflow_gate, WorkflowGate)
        assert not isinstance(service.workflow_gate, PipelineService)

    def test_gate_holds_no_facade_or_transport(self, direct_gate):
        """Constructor takes only the artifact repository -- no secrets, no
        provider, no storage, no callback a facade could hide behind."""
        params = set(inspect.signature(type(direct_gate).__init__).parameters)
        assert params == {"self", "artifacts"}

    def test_gate_exposes_no_mutation_or_execution_surface(self, direct_gate):
        """Eligibility is all it can do: nothing to write, approve, render,
        publish or schedule exists on the object."""
        forbidden = ("approve", "write_artifact", "run_render", "run_tts",
                     "run_video_analysis", "submit", "publish", "upload",
                     "set_visibility", "create_processing_plan")
        for name in forbidden:
            assert not hasattr(direct_gate, name), name

    def test_a_service_can_consume_the_port_without_a_facade(self, direct_gate):
        """The future-service contract: code holding only `WorkflowGateQuery`
        performs the eligibility check -- this is the dependency future
        production services will receive, not a PipelineService."""
        class FutureMomentService:
            def __init__(self, workflow):
                self._workflow = workflow

            def ensure_selectable(self):
                self._workflow.require_phase("moment_selection")

        consumer = FutureMomentService(direct_gate)
        # No plan exists in this fresh namespace, so the phase must be denied
        # by the gate's own answer -- proving the query reached the policy.
        with pytest.raises(PipelineServiceError):
            consumer.ensure_selectable()

    def test_no_application_service_imports_the_facade(self):
        """Architecture guard: workflow_gate.py must not reach back into
        creator_console -- checked on the real import graph, not docstrings."""
        import ast
        from pathlib import Path

        path = (Path(__file__).parent.parent /
                "application/services/workflow_gate.py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not name.startswith("creator_console"), name
                assert not name.startswith("services.api"), name
                assert not name.startswith("fastapi"), name
