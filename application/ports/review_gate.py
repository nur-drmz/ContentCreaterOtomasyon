"""Human-review precondition port.

WHY THIS EXISTS
---------------
Approval granting consults three human review checklists before it will let the
canonical hash gate run:

  - the COPYRIGHT checklist, before a processing plan may be approved;
  - the RENDER checklist, before a render may be approved;
  - the VOICE checklist, before a voiceover may be approved.

`ApprovalService` must enforce those preconditions without reaching back into
`PipelineService` (ADR-001), and without a second implementation of the
checklist logic. This port is the narrow contract between the two: three read
queries, nothing else. Checklist MUTATION (ticking an item, requesting or
clearing a revision) stays with its current owner and is not part of this port.

SEMANTICS A CONFORMING IMPLEMENTATION MUST PRESERVE
---------------------------------------------------
complete       `complete` is True only when every required item has been ticked
               AGAINST THE CURRENT CONTENT DIGEST. A checklist ticked for an
               older render/voiceover/source must not read as complete.

open_revision  A requested-but-unresolved revision blocks approval even when
               every item is ticked: an open objection outranks a full list.

blocker        Human-readable reason, used verbatim in the refusal message.

never positive The implementation reports what the stored checklist says. It may
               never manufacture a satisfied precondition.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ReviewGate(Protocol):
    """Read-only view of the human review checklists approval depends on."""

    def copyright_review_state(self) -> dict[str, Any]:
        """Source-rights checklist state, bound to the source fingerprint."""
        ...

    def render_review_state(self, slot: int = 1) -> dict[str, Any]:
        """Per-slot render checklist state, bound to the video digest."""
        ...

    def voice_review_state(self) -> dict[str, Any]:
        """Voiceover checklist state, bound to the audio digest."""
        ...
