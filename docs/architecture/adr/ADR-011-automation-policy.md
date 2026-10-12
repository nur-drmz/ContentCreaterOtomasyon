# ADR-011 — Automation Policy Model

## Status
Accepted

## Context
`creator_console/automation.py` exists. The prompt pack requires a commercial automation policy with controls.

## Decision
Automation is a workspace policy, not an unconstrained AI agent.

Policy shape:

```
Trigger -> Source Selector -> Workflow -> Template -> Approval Policy -> Publish Policy -> Budget -> Failure Policy
```

### Built-in templates

- `manual_all`: every stage stops for human review.
- `moment_and_final`: machine runs through voice, human approves final.
- `final_only`: machine runs through render, human approves only publish.
- `preapproved_owned_source_autopilot`: only allowed when source is workspace-owned and rights policy is pre-cleared.

### Non-bypassable gates

- Third-party source rights decision must be approved before any `ingest` job.
- Trend research can run automatically.
- Reposting third-party source media is never automatic.
- Budget: per-run spend cap and daily/weekly cap.
- Failure policy: `pause`, `notify`, `retry`, `escalate`.

## Consequences
- `automation.py` becomes a policy interpreter, not a free-runner.
- Jobs are still submitted through `JobStore`.
- Every automated action produces an audit event.

## Rejected alternatives
- A. AI decides rights. Rejected: legal and policy risk.
- B. Fully automatic upload. Rejected: private-first publishing must be default.

## Migration path
1. Add `AutomationPolicy` schema.
2. Refactor `automation.py` into `application/automation_service.py`.
3. Add policy templates.
4. Add trigger/workflow runner.
