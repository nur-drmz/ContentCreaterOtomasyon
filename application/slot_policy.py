"""Output-slot policy derived from the processing plan.

WHY THIS IS A NEUTRAL MODULE
----------------------------
`slot_count` / `slot_indexes` / `require_slot` were `PipelineService` methods,
but `ApprovalService` needs the same rules (approval is per-slot for renders,
and the stale-approval sweep walks every slot). Duplicating them would let the
façade and the service disagree about how many Shorts a plan defines.

Pure functions over an already-loaded plan: no IO, no artifact reads.
"""
from __future__ import annotations

from .errors import PipelineServiceError


def slot_count(plan) -> int:
    """How many Shorts the plan asks for (1 when there is no plan).

    Read from `outputs` rather than `decision.output_count` so the slots the
    engine walks are exactly the slots the plan describes, directories
    included.
    """
    slots = (plan or {}).get("outputs") or []
    return max(1, len(slots))


def slot_indexes(plan) -> list:
    return list(range(1, slot_count(plan) + 1))


def require_slot(plan, slot) -> int:
    """Reject a slot the plan does not define.

    Without this an out-of-range slot would quietly create
    `results/render/output_47/` and render a Short nobody planned -- outside
    every count the plan, the batch board and the approval chain reason about.
    """
    try:
        index = int(slot)
    except (TypeError, ValueError):
        raise PipelineServiceError(f"gecersiz cikti yuvasi: {slot!r}") from None
    total = slot_count(plan)
    if not 1 <= index <= total:
        raise PipelineServiceError(
            f"cikti yuvasi {index} bu planda yok (plan {total} cikti tanimliyor)")
    return index
