# ADR-006 — Approval Policy and Maker-Checker

## Status
Accepted

## Context
Approval is SHA-256 digest bound (`local_pipeline/approval.py`). There is no code-level distinction between machine self-approval and human maker-checker policy.

## Decision
Preserve three invariants and add a versioned approval policy.

### Invariants (non-negotiable)

- **A. Machine self-approval never allowed.** A generation function/provider cannot mark its own output as human-approved. AI-generated approvals are not permitted.
- **B. Stale-content approval invalidation.** Approval is valid only for the exact content digest it was bound to. Mutating any referenced artifact invalidates the approval.
- **C. Approval requires an authenticated actor.** Every approval records `actor`, `timestamp`, `artifact_digests`.

### Policy (workspace-configurable)

- `solo_allowed`: same authenticated user may edit and approve (default for personal workspaces).
- `distinct_reviewer_required`: approver must be a different `user_id` than the last editor.
- `role_reviewer_required`: approver must have `reviewer`, `admin`, or `owner` role.

Policy is stored on the workspace and referenced by each approval record.

## Consequences
- `approve()` checks workspace policy before recording.
- `PipelineService` remains the entry point, but `application/services/approval_service.py` enforces policy.
- `approval` table includes `policy_version` and `policy_kind`.

**Update (Milestone 5):** `application/services/approval_service.py` now exists and owns the approval lifecycle — granting, state, chain validity, invalidation records — with `local_pipeline.approval` still the canonical digest engine. The reviewer *policy* is deliberately **not** implemented yet: local mode records `approved_by` verbatim as an unauthenticated actor string, and `solo_allowed` remains the effective (and only) behaviour. The seam for the future policy is the `approve()` method itself — once server-side auth supplies a trusted actor and workspace identity, the `solo_allowed`/`distinct_reviewer`/`role_reviewer` lookup plugs in before the write without changing the call shape.

## Rejected alternatives
- A. Always forbid same-actor approval. Rejected: blocks solo creators.
- B. No policy, rely on convention. Rejected: not enforceable at scale.

## Migration path
1. Add `approval_policy` to workspace schema.
2. Default to `solo_allowed` for existing local projects.
3. Add `application/approval_service.py`.
4. Update `PipelineService.approve()` to call it.
