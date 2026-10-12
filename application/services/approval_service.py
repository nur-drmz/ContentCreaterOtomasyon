"""ApprovalService: the human-approval lifecycle for produced content.

WHAT THIS OWNS
--------------
  - granting approval through the canonical hash-bound gate,
  - the preconditions that gate consults (rights + human review checklists),
  - live approval state, re-derived from current content,
  - approval validity along the upstream dependency chain,
  - the stale-approval sweep,
  - recording invalidation when another service edits approved content.

THE INVARIANTS THIS CLASS EXISTS TO PROTECT
-------------------------------------------
A. No machine self-approval. Nothing here is reachable from a generation path.
   `approve()` requires a caller-supplied digest that only a human who looked at
   the review output can know; there is no "approve on success" parameter.
B. Approval is content-bound. The digest is re-derived from current content by
   `local_pipeline.approval`, never trusted from the request.
C. Stale approval is invalid. `state()`/`chain()` recompute validity every time
   and NOTHING is ever deleted -- a grant that stopped binding is still a fact
   that happened, so it stays in the audit trail and in the artifact.
D. Human review stays mandatory. Render and voiceover additionally require a
   completed checklist; the plan additionally requires cleared source rights.
E. Rights cannot be bypassed. The copyright precondition is consulted through
   `ports.ReviewGate`, which reports what the stored checklist says and can
   never manufacture a positive answer.
F. Approval is not publication. This service covers the five CONTENT kinds in
   `application.approval_catalog` only. Publication metadata, upload intent and
   visibility have separate state machines and separate façade methods; an
   unknown kind here is refused rather than generically approved.

WHAT IT DELIBERATELY DOES NOT OWN
---------------------------------
- Digest computation, canonicalization, review descriptions and approval-state
  validation: `local_pipeline.approval` stays canonical. This service never
  re-implements a hash.
- Checklist MUTATION (ticking items, requesting/clearing revisions) and the
  checklist modules themselves.
- Publication approvals (a future PublishService).
- Workflow phase gates (`plan_gate`/`require_phase`), render execution, job
  leasing, provider routing, authentication, billing.
- Producing any of the artifacts it approves.
"""
from __future__ import annotations

from application import slot_policy
from application.approval_catalog import (
    APPROVAL_ARTIFACTS,
    CHECKLIST_GATED_KINDS,
    RIGHTS_GATED_KIND,
)
from application.artifact_catalog import PER_SLOT_ARTIFACTS
from application.errors import ArtifactProblem, PipelineServiceError
from local_pipeline import approval, render_utils, source_strategy, video_analysis


class ApprovalService:
    """Content-approval lifecycle for ONE project namespace.

    Satisfies `ports.ApprovalInvalidator` structurally, which is the narrow
    contract `AnalysisService` depends on.
    """

    def __init__(self, *, artifacts, audit, ownership, review_gate):
        #: `ports.ArtifactRepository` -- approvals live inside the artifacts.
        self._artifacts = artifacts
        #: `ports.AuditSink`.
        self._audit = audit
        self._ownership = ownership
        #: `ports.ReviewGate` -- rights + human review preconditions.
        self._gate = review_gate

    # ------------------------------------------------------------- helpers
    def _plan(self) -> dict:
        return self._artifacts.load_artifact("processing_plan")["data"] or {}

    def _resolve_slot(self, kind: str, key: str, slot, *, strict: bool) -> int:
        """Per-slot artifacts get a validated slot; project-wide ones get 1."""
        if key not in PER_SLOT_ARTIFACTS:
            return 1
        if strict:
            return slot_policy.require_slot(self._plan(), slot)
        return int(slot or 1)

    # ------------------------------------------------------------ granting
    def approve(self, kind: str, *, reviewed_sha256: str, approved_by: str,
                notes: str = "", slot: int = 1) -> dict:
        """Approve through the EXISTING hash-bound gate.

        The console cannot approve anything the gate would refuse: it passes the
        user's echoed hash straight to `approval.approve_artifact`, which
        re-derives the digest from current content.
        """
        if kind not in APPROVAL_ARTIFACTS:
            raise PipelineServiceError(f"bilinmeyen onay turu: {kind!r}")
        key = APPROVAL_ARTIFACTS[kind]
        slot = self._resolve_slot(kind, key, slot, strict=True)
        loaded = self._artifacts.load_artifact(key, required=True, slot=slot)
        if loaded["problem"]:
            raise ArtifactProblem(loaded["problem"])
        self._assert_preconditions(kind, slot)
        try:
            approved = approval.approve_artifact(
                loaded["data"], kind, reviewed_sha256, approved_by, notes)
        except ValueError as exc:
            self._audit.record(
                "approval.rejected", actor=approved_by,
                artifact=self._artifacts.artifact_relative(key, slot),
                detail={"kind": kind, "reason": str(exc), "slot": slot})
            raise PipelineServiceError(str(exc)) from None
        relative = self._artifacts.write_artifact(key, approved,
                                                  actor=approved_by, slot=slot)
        self._audit.record("approval.granted", actor=approved_by,
                           artifact=relative,
                           detail={"kind": kind, "slot": slot,
                                   "approved_content_sha256":
                                       approved["human_approval"]
                                       ["approved_content_sha256"]})
        return approved

    def _assert_preconditions(self, kind: str, slot: int) -> None:
        """Rights and human-review gates, enforced BEFORE any write.

        These ADD to the canonical hash gate; they never replace it.
        """
        if kind == RIGHTS_GATED_KIND:
            # The earliest gate in the pipeline, and the only one that asks
            # about the SOURCE rather than something this project produced.
            # Placed here so a project with no basis for its footage is stopped
            # before any analysis or render time is spent on it.
            rights = self._gate.copyright_review_state()
            if not rights["complete"] or rights["open_revision"]:
                raise PipelineServiceError(
                    "ONAY REDDEDILDI: "
                    + (rights["blocker"] or "telif kontrol listesi tamamlanmadi"))
        if kind in CHECKLIST_GATED_KINDS:
            # The hash proves the reviewer saw (or heard) the bytes; the
            # checklist proves they checked the things that actually go wrong.
            state = (self._gate.render_review_state(slot) if kind == "render"
                     else self._gate.voice_review_state())
            if not state["complete"] or state["open_revision"]:
                raise PipelineServiceError(
                    "ONAY REDDEDILDI: "
                    + (state["blocker"] or "insan inceleme listesi tamamlanmadi"))

    # -------------------------------------------------------- invalidation
    def record_invalidation(self, kind: str, *, artifact: str, actor: str,
                            reason: str, previous_approval, slot: int = 1) -> bool:
        """Record that a previously granted approval no longer binds.

        Writes NO artifact: the caller has already persisted the edited content
        (with its approval reset by construction), and a second write here would
        duplicate the `artifact.written` trail. Returns whether an event was
        recorded, so a caller can assert exactly-once behaviour.
        """
        previous = previous_approval or {}
        if not previous.get("approved"):
            return False
        detail = {"kind": kind, "reason": reason,
                  "previous_approved_content_sha256":
                      previous.get("approved_content_sha256")}
        if slot and int(slot) != 1:
            detail["slot"] = int(slot)
        self._audit.record("approval.invalidated", actor=actor,
                           artifact=artifact, detail=detail)
        return True

    # ------------------------------------------------------------- queries
    def state(self, kind: str, slot: int = 1) -> dict:
        """Live approval validity, re-derived from current content every time."""
        key = APPROVAL_ARTIFACTS[kind]
        slot = self._resolve_slot(kind, key, slot, strict=False)
        loaded = self._artifacts.load_artifact(key, slot=slot)
        if loaded["problem"]:
            return {"kind": kind, "slot": slot, "present": True, "valid": False,
                    "approved": False, "reason": loaded["problem"]}
        if not loaded["data"]:
            return {"kind": kind, "slot": slot, "present": False, "valid": False,
                    "approved": False, "reason": "artefakt henuz uretilmedi"}
        state = approval.check_approval(loaded["data"], kind)
        try:
            review = approval.describe_for_review(kind, loaded["data"])
        except Exception as exc:  # noqa: BLE001 - review must never crash the page
            review = {"reviewable": False, "blocker": str(exc), "content_sha256": None}
        return {
            "kind": kind, "slot": slot, "present": True,
            "status": loaded["data"].get("status"),
            "approved": state["approved"], "valid": state["valid"],
            "reason": state["reason"],
            "content_sha256": review.get("content_sha256"),
            "reviewable": review.get("reviewable"),
            "blocker": review.get("blocker"),
            "blocking_conditions": review.get("blocking_conditions") or [],
        }

    def chain(self, slot: int = 1) -> dict:
        """Approval validity INCLUDING upstream dependencies.

        A per-artifact hash check is not enough. Approving a script, then editing
        the processing plan's decision, leaves the script's own digest untouched --
        so `check_approval('script')` still says "valid" even though the strategy
        it was written for no longer exists. The user-visible rule is that a
        change anywhere upstream invalidates everything downstream, so validity is
        computed along the chain:

            processing_plan -> [slot moment_selection] -> script -> voiceover -> slot render

        `script`/`voiceover` are only in the chain for `narrated_theory`; in
        `direct_clip_series` they are never produced and their absence must not
        invalidate the render.

        The chain is evaluated FOR ONE SLOT. Each Short has its own moment
        selection and its own render, so slot 2's render approval must not be
        invalidated by slot 3's stale selection -- they are independent Shorts
        that happen to share a source, a plan and an analysis.

        NOTHING is deleted here. The artifacts, their `human_approval` blocks and
        the audit trail all stay exactly as they were -- this only reports that an
        approval no longer binds.
        """
        plan = self._plan()
        mode = (plan.get("decision") or {}).get("mode")
        chain = ["processing_plan"]
        if mode == source_strategy.MODE_NARRATED_THEORY:
            chain += ["script", "voiceover"]
        else:
            # The caption text of a clip Short is as much a human decision as a
            # narrated script is, so it belongs in the chain: correcting a word
            # after approving the render must invalidate that render.
            chain.append("transcript")
        chain.append("render")

        upstream_problems: list = []

        # The moment selection is upstream of everything after the plan even
        # though it is not an approvable "kind": a stale selection means the
        # script/render were built from a moment that no longer exists.
        selection = self._artifacts.load_artifact("moment_selection", slot=slot)
        hot = self._artifacts.load_artifact("hot_moments")
        selection_problem = None
        if selection["problem"]:
            selection_problem = f"moment_selection: {selection['problem']}"
        elif selection["data"] and hot["data"]:
            errors = video_analysis.validate_moment_selection(
                selection["data"], hot["data"], require_preview=False)
            if errors:
                selection_problem = f"moment_selection bayat: {errors}"

        states = {}
        for kind in chain:
            state = self.state(kind, slot)
            problems = list(upstream_problems)
            if kind != "processing_plan" and selection_problem:
                problems.append(selection_problem)
            state["upstream_invalid"] = problems
            state["effective_valid"] = bool(state.get("valid")) and not problems
            if state.get("approved") and problems and state.get("valid"):
                state["reason"] = (
                    "Kendi icerigi degismedi ama YUKARI-AKIS degisti, bu yuzden bu onay "
                    "artik gecerli sayilmiyor: " + "; ".join(problems)
                )
            # A render built from an older transcript revision is stale even when
            # nothing upstream still holds an INVALID approval: editing a
            # transcript clears its approval outright, so there is no
            # "approved but no longer valid" upstream state left to notice, and
            # a render approved before the edit would otherwise keep looking fine.
            if kind == "render":
                drift = self._render_transcript_drift(slot)
                if drift:
                    problems = list(problems) + [drift]
                    state["upstream_invalid"] = problems
                    state["effective_valid"] = False
            states[kind] = state
            if state.get("present") and not state.get("effective_valid") \
                    and state.get("approved"):
                upstream_problems.append(f"{kind} onayi gecersiz")
            elif kind == "processing_plan" and not state.get("valid"):
                upstream_problems.append("processing_plan onayi gecerli degil")

        for kind in APPROVAL_ARTIFACTS:
            states.setdefault(kind, {**self.state(kind, slot),
                                     "upstream_invalid": [], "effective_valid": False,
                                     "in_chain": False})
            states[kind].setdefault("in_chain", kind in chain)
        return states

    def _render_transcript_drift(self, slot: int):
        """Message when the rendered video predates the current transcript.

        Compares the digest the manifest recorded at render time with the
        transcript's digest now. Manifests written before that digest existed
        carry none and are NOT reported: claiming drift we cannot actually
        observe would be worse than staying quiet about it.
        """
        manifest = self._artifacts.load_artifact(
            "render_manifest", slot=slot)["data"] or {}
        recorded = (manifest.get("subtitles") or {}).get("transcript_content_sha256")
        if not recorded:
            return None
        current = render_utils._transcript_digest(
            self._artifacts.load_artifact("moment_transcript",
                                          slot=slot)["data"] or {})
        if current and current != recorded:
            return ("transcript: render bu altyazi revizyonundan once uretildi; "
                    "video guncel metni yansitmiyor")
        return None

    def invalidated(self) -> list:
        """Approvals that were granted but no longer bind -- their own content or
        anything upstream of them changed.

        Every slot is checked: a batch can have one Short whose approval still
        binds and another whose source moment was re-analysed underneath it.

        Reported, never deleted: the audit trail of the original approval stays
        intact even after it stops being valid.
        """
        stale = []
        seen = set()
        for index in slot_policy.slot_indexes(self._plan()):
            for kind, state in self.chain(index).items():
                if not (state.get("approved") and not state.get("effective_valid")):
                    continue
                # Project-wide approvals (plan, script, voiceover) are identical
                # for every slot; reporting them once per Short would turn one
                # stale plan into N identical warnings.
                marker = (kind, state.get("slot") if kind == "render" else None)
                if marker in seen:
                    continue
                seen.add(marker)
                stale.append(state)
        return stale
