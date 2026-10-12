"""WorkflowGate: the read-only interpreter of the processing plan.

WHAT THIS OWNS
--------------
Every question a production phase asks BEFORE it runs, answered solely from
the `processing_plan` artifact:

  - **phase eligibility** -- may `video_analysis`, `moment_selection`,
    `transcript`, `script_generation`, `tts` or `render` execute?
    (`plan_gate`, `require_phase`)
  - **plan decision** -- which mode/duration/output count the operator chose
    (`plan_decision`)
  - **slot scope** -- how many Shorts the plan defines and whether a given
    slot exists (`slot_count`, `slot_indexes`, `require_slot`,
    `slot_moment_rank`)

The eligibility policy itself is NOT re-implemented here: it stays canonical
in `local_pipeline.source_strategy` (`plan_allows_phase`, `GATED_PHASES`,
`MODE_PHASE_POLICY`, `validate_processing_plan`, `check_approval`) and the
slot rules stay in `application.slot_policy`. This service is the single
reader that binds those pure policies to the stored artifact.

WHY IT IS ITS OWN COMPONENT
---------------------------
Future production services (VideoAnalysis, Moment, Transcript, Script, Voice,
Render) must ask "is this phase allowed / does this slot exist" WITHOUT
importing `PipelineService`. Until now that meant calling façade methods;
this gate is the same answers behind `ports.WorkflowGateQuery` with a single
injected `ArtifactRepository`.

WHAT IT DELIBERATELY DOES NOT OWN
----------------------------------
- **Approval lifecycle.** Whether a plan/script/render approval still binds is
  computed by `local_pipeline.approval` (inside `plan_allows_phase` for the
  plan itself) and reported by `ApprovalService` for everything else. The gate
  CONSUMES that verdict for the plan; it computes no approval of its own.
- **Rights/copyright.** Checklist state lives behind `ports.ReviewGate`,
  consulted by `ApprovalService` at grant time. The gate does not re-check
  rights: a plan that passed approval already carried its rights gate.
- **Phase execution.** No analysis, transcription, script, TTS, render or
  publish call can happen here -- there is no provider, tracker or binary
  resolver in the dependency list to call them with.
- **Job semantics.** No submission, leasing, heartbeats or cancellation.
- **Artifact prerequisites inside a phase.** "hot_moments exists", "moment
  selected", "transcript approved" are enforced inside each `run_*` runner and
  by the approval chain -- the plan gate is the FIRST hurdle, not the whole
  fence.

INVARIANTS
----------
- Read-only: no write, audit, progress or network operation exists here.
- `plan_gate` never raises for an ordinary "no"; an unknown phase raises
  `ValueError` (a programming error), matching the canonical policy.
- `require_phase` raises `PipelineServiceError` with the gate's reason.
"""
from __future__ import annotations

from application import slot_policy
from application.errors import PipelineServiceError
from local_pipeline import source_strategy


class WorkflowGate:
    """Workflow eligibility and slot scope for ONE project namespace.

    Satisfies `ports.WorkflowGateQuery` structurally, which is the narrow
    contract future production services depend on.
    """

    def __init__(self, *, artifacts):
        #: `ports.ArtifactRepository` -- the only fact the gate needs is the
        #: `processing_plan` artifact; a repository scoped to the namespace
        #: supplies it with the honest missing/corrupt report.
        self._artifacts = artifacts

    def _plan(self) -> dict:
        return self._artifacts.load_artifact("processing_plan")

    # ------------------------------------------------------- phase eligibility
    def plan_gate(self, phase: str) -> tuple:
        plan = self._plan()
        if plan["problem"]:
            return False, plan["problem"]
        return source_strategy.plan_allows_phase(plan["data"], phase)

    def require_phase(self, phase: str) -> None:
        allowed, reason = self.plan_gate(phase)
        if not allowed:
            raise PipelineServiceError(reason)

    # ------------------------------------------------------------ plan queries
    def plan_decision(self) -> dict:
        return (self._plan()["data"] or {}).get("decision") or {}

    # ------------------------------------------------------------- output slots
    def slot_count(self) -> int:
        """How many Shorts the APPROVED plan asks for (1 when there is no plan).

        Read from `outputs` rather than `decision.output_count` so the slots
        the engine walks are exactly the slots the plan describes, directories
        included.
        """
        return slot_policy.slot_count(self._plan()["data"] or {})

    def slot_indexes(self) -> list:
        return slot_policy.slot_indexes(self._plan()["data"] or {})

    def require_slot(self, slot: int) -> int:
        """Reject a slot the approved plan does not define.

        Without this an out-of-range slot would quietly create
        `results/render/output_47/` and render a Short nobody planned --
        outside every count the plan, the batch board and the approval chain
        reason about.
        """
        return slot_policy.require_slot(self._plan()["data"] or {}, slot)

    def slot_moment_rank(self, slot: int):
        """The moment rank this slot's plan entry pre-assigned, or None."""
        plan = self._plan()["data"] or {}
        for entry in (plan.get("outputs") or []):
            if int(entry.get("index") or 0) == int(slot):
                return entry.get("moment_rank")
        return None
