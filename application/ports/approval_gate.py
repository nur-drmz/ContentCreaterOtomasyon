"""Approval capabilities other application services may depend on.

WHY THIS EXISTS
---------------
`AnalysisService.update_processing_plan` rebuilds a plan, which resets its
approval. Recording that invalidation is approval semantics -- the event name,
the detail shape and the "never delete the original grant" rule all belong to
the approval owner. But services must not import each other (ADR-001), so the
capability is expressed as a port: `AnalysisService` depends on
`ApprovalInvalidator`, and `ApprovalService` implements it.

`PublishService.create_intent` must refuse an upload intent when an upstream
content approval no longer binds. That is a READ of the approval owner's chain
computation, so it is expressed as a second port: `PublishService` depends on
`ApprovalQuery`, and `ApprovalService` implements it.

WHY THESE ARE THIS NARROW
-------------------------
The approval RESET itself is already a pure artifact transformation performed by
`source_strategy.build_processing_plan`, and the rebuilt plan is persisted by
the one write the edit already performs. So the only thing the editing service
needs from the approval owner is the invalidation RECORD. Exposing anything more
(granting, state queries) would hand a mutation service authority it must not
have, and would tempt a second write.

`ApprovalQuery` is likewise read-only: the publish gate needs the chain verdict,
never the ability to grant, reject or invalidate. Content approval must not be
confusable with publication authorization, so this port exposes no write path.

SEMANTICS A CONFORMING IMPLEMENTATION MUST PRESERVE
---------------------------------------------------
no extra write  Recording an invalidation must not persist the artifact again.
                The caller already wrote it; a second write would double the
                `artifact.written` trail and could interleave with a failure.

exactly once    One invalidation per edit that actually dropped a live approval.
                Nothing is recorded when the previous state was unapproved.

never deletes   The original `approval.granted` event stays in the trail. "This
                approval no longer binds" and "this approval never happened" are
                very different facts.

read-only       `ApprovalQuery.chain` reports `effective_valid` per kind; it
                never mutates artifacts, approvals or the audit trail.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ApprovalInvalidator(Protocol):
    """Records that previously granted approval no longer binds."""

    def record_invalidation(self, kind: str, *, artifact: str, actor: str,
                            reason: str, previous_approval: dict[str, Any] | None,
                            slot: int = 1) -> bool:
        """Record one invalidation; return whether an event was written.

        Returns False (and writes nothing) when `previous_approval` shows the
        artifact was not actually approved.
        """
        ...


@runtime_checkable
class ApprovalQuery(Protocol):
    """Read-only view of the content-approval chain for one output slot.

    Implemented by `ApprovalService`; consumed by `PublishService`, whose
    `create_intent` must know whether the render approval STILL binds before a
    publish intent may exist, and by `TranscriptService`, whose `transcript_view`
    reports the live approval state of one kind to the caption screen. The port
    deliberately exposes only reads: a caller needs the verdict, never the
    means to grant, reject or invalidate one.
    """

    def state(self, kind: str, slot: int = 1) -> dict[str, Any]:
        """Live approval validity for ONE kind, re-derived from current content.

        Returns the same shape `ApprovalService.state` produces: `present`,
        `approved`, `valid`, `reason` and the review-facing fields.
        """
        ...

    def chain(self, slot: int = 1) -> dict[str, Any]:
        """Approval validity per kind, INCLUDING upstream dependencies.

        Returns the same shape `ApprovalService.chain` produces: one entry per
        registered kind with `approved`, `valid`, `effective_valid` and
        `upstream_invalid` keys.
        """
        ...
