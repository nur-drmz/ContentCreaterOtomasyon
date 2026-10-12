"""Read-only workflow-eligibility query port.

WHY A PORT INSTEAD OF PASSING `PipelineService` OR THE GATE ITSELF
------------------------------------------------------------------
Production phases (analysis, moments, transcript, script, TTS, render) all ask
the same question before they run: "does the approved processing plan permit
this, and which output slot am I?" Today that answer lives on the façade as
`plan_gate`/`require_phase` plus the plan-derived slot queries. Any future
application service that asked it by importing `PipelineService` would drag
the whole façade -- providers, jobs, secrets -- into a unit that only needs a
plan lookup.

This port is the smaller clean abstraction: the eligibility and slot-scope
reads, nothing else. `application.services.WorkflowGate` satisfies it
structurally; a test double satisfies it with three dicts.

SEMANTICS A CONFORMING IMPLEMENTATION MUST PRESERVE
---------------------------------------------------
- **Read-only.** A gate query must never write an artifact, record an audit
  event, grant approval, or advance workflow state. Asking "may render run"
  can never make it so.
- **`plan_gate` reports, `require_phase` enforces.** `plan_gate` returns
  `(allowed, reason)` -- a human-readable denial, never an exception for an
  ordinary "no". `require_phase` raises `PipelineServiceError` with that
  reason. An UNKNOWN phase is a programming error, not a denial: `ValueError`
  propagates from the canonical policy in both.
- **Denial reasons are user-facing.** They are the strings the console shows
  the operator ("plani onayla", "bu modda calistirilmaz"), not error codes.
- **Slot queries follow the plan, not the filesystem.** `require_slot`
  rejects a slot the approved plan does not define; `slot_count` is 1 when
  no plan exists (the historical single-output default), never 0.
- **`plan_decision` is the raw decision block** (`{}` when there is no plan):
  callers must treat it as plan data, not as proof the plan is approved --
  that is `plan_gate`'s job.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class WorkflowGateQuery(Protocol):
    """The read-only eligibility + slot-scope view of the processing plan."""

    def plan_gate(self, phase: str) -> tuple:
        """(allowed, reason) for one gated phase; ValueError on unknown phase."""
        ...

    def require_phase(self, phase: str) -> None:
        """Raise PipelineServiceError when `plan_gate` denies the phase."""
        ...

    def plan_decision(self) -> dict:
        """The plan's `decision` block, or {} when there is no plan."""
        ...

    def slot_count(self) -> int:
        """How many Shorts the plan defines (1 when there is no plan)."""
        ...

    def slot_indexes(self) -> list:
        """1..slot_count, in plan order."""
        ...

    def require_slot(self, slot: int) -> int:
        """The validated slot index, or PipelineServiceError if undefined."""
        ...

    def slot_moment_rank(self, slot: int):
        """The moment rank pre-assigned to this slot, or None."""
        ...
