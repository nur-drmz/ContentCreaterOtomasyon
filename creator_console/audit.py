"""Append-only audit events.

WHY THIS EXISTS
---------------
Approvals, setting changes, job transitions and provider calls must leave a trail
that survives a later invalidation. When a source video is replaced and every
downstream approval goes stale, the RECORD of the original approval must remain --
"this approval is no longer valid" and "this approval never happened" are very
different facts.

So events are appended to a JSON Lines file and never rewritten. Payloads are
sanitized through `google_ai.sanitize_google_error` before they are written, so a
credential cannot reach the trail via an error message.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .ids import Ownership, new_id
from .redaction import scrub

AUDIT_FILENAME = "audit_events.jsonl"

#: Closed vocabulary; an unknown event type is a bug, not a free-text field.
EVENT_TYPES = {
    "project.created",
    "project.updated",
    "project.rewound",
    "project.reset",
    "project.archived",
    "project.deleted",
    "script.updated",
    "source.registered",
    "assessment.created",
    "plan.created",
    "plan.updated",
    "plan.approved",
    "plan.approval_rejected",
    "job.submitted",
    "job.deduplicated",
    "job.claimed",
    "job.started",
    "job.succeeded",
    "job.failed",
    "job.retry_requested",
    "job.cancel_requested",
    "job.cancelled",
    "job.recovered",
    "artifact.written",
    "artifact.rejected",
    "transcript.manual_edits_archived",
    "approval.granted",
    "approval.rejected",
    "approval.invalidated",
    "settings.changed",
    "provider.call",
}


class AuditLog:
    """Append-only JSONL trail, one file per project namespace."""

    def __init__(self, storage, ownership: Ownership):
        self.storage = storage
        self.ownership = ownership

    @property
    def path(self):
        return self.storage.resolve(self.ownership, AUDIT_FILENAME, create_parent=True)

    def record(self, event_type: str, *, actor: str = "system", detail=None,
               job_id: str | None = None, artifact: str | None = None) -> dict:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"bilinmeyen audit olayi: {event_type!r}")
        event = {
            "event_id": new_id("event"),
            "event_type": event_type,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "actor": str(actor or "system")[:120],
            "job_id": job_id,
            "artifact": artifact,
            "detail": _sanitize(detail),
            **self.ownership.to_dict(),
        }
        line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
        path = self.path
        # Append + fsync: an audit entry that is lost on crash is worse than a
        # slightly slower request.
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        return event

    def events(self, *, limit: int | None = None, job_id: str | None = None) -> list:
        path = self.path
        if not path.is_file():
            return []
        entries = []
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    # A truncated final line (crash mid-append) must not hide the
                    # rest of the trail.
                    entries.append({"event_type": "audit.unreadable_line",
                                     "detail": {"raw_length": len(raw)}})
                    continue
                if job_id and entry.get("job_id") != job_id:
                    continue
                entries.append(entry)
        entries.reverse()
        return entries[:limit] if limit else entries


def _sanitize(detail):
    """Recursively strip registered secret VALUES and anything key-shaped.

    Pattern matching alone was not enough: a provider token that does not look
    like `AIza…` used to survive into the trail. `scrub` also removes the exact
    values the secret resolver has seen this process.
    """
    return scrub(detail, max_len=2000)
