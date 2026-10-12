"""Audit persistence port.

Derived from `creator_console.audit.AuditLog`. This is NOT a generic logging
abstraction: audit events, domain events and telemetry are three different
concepts (see ADR-012) and only the first one is described here.

SEMANTICS A CONFORMING IMPLEMENTATION MUST PRESERVE
---------------------------------------------------
closed vocabulary  An event type outside the registered set must RAISE. An
                   unknown name is a bug, not a free-text field, and must never
                   become an unqueryable record.

append-only        A recorded event is never rewritten or deleted. When a source
                   video is replaced and downstream approvals go stale, the
                   RECORD of the original approval must survive: "this approval
                   is no longer valid" and "this approval never happened" are
                   very different facts.

redaction          Payloads are scrubbed before they are written, so a
                   credential cannot reach the trail via an error message.

ordering           `events()` returns newest-first. A truncated trailing line
                   (crash mid-append) must not hide the rest of the trail.

context            Every event carries actor plus the ownership triple
                   (user/workspace/project).
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AuditSink(Protocol):
    """Append-only audit trail for one project namespace."""

    def record(self, event_type: str, *, actor: str = "system", detail=None,
               job_id: str | None = None, artifact: str | None = None) -> dict[str, Any]:
        """Append one event and return it.

        Must raise `ValueError` when `event_type` is not in the closed
        vocabulary. `detail` must be scrubbed before persisting.
        """
        ...

    def events(self, *, limit: int | None = None,
               job_id: str | None = None) -> list[dict[str, Any]]:
        """Return recorded events, newest first, optionally filtered by job."""
        ...
