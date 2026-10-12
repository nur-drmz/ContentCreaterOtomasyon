# ADR-013 — Dead / Unbound Code Policy

## Status
Accepted

## Context
`creator_console/pipeline_service.py` has `resolve_edl_revision()` but `creator_console/api.py` has no route for it. `POST /edl/revision` only calls `request_edl_revision()`.

## Decision
Classify `resolve_edl_revision` as **expose later**.

Reasoning:
- It is not dead; it completes the EDL revision state machine (clear notes, increment version).
- `request_edl_revision` (POST /edl/revision) creates a revision note.
- `resolve_edl_revision` would be the operator/admin action that clears the note after edits are accepted.
- No frontend currently needs it, but the state machine logically needs it.
- No tests fail because the method is not exposed.

Action:
- Keep the method.
- Do not add a public API route until the Render Atelier has a "mark as resolved" UI.
- When the UI is added, expose `POST /edl/revision/resolve` with `admin`/`editor` permission.

## Consequences
- Dead-code deletion is avoided for now.
- `resolve_edl_revision` is documented as a pending feature.
- Any future editor redesign must include "resolve revision" semantics.

## Rejected alternatives
- A. Delete it now. Rejected: it represents a real, planned state transition; deletion risks losing the intent.
- B. Expose it now. Rejected: no consumer, no test, and premature API surface.

## Migration path
1. Add "Mark resolved" UI in Render Atelier.
2. Add `POST /edl/revision/resolve`.
3. Add `edl.revision_resolved` audit test.
4. If no UI is planned for 6 months, revisit deletion.
