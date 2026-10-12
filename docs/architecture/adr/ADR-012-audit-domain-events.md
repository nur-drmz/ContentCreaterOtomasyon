# ADR-012 — Audit, Domain and Telemetry Events

## Status
Accepted

## Context
`creator_console/audit.py` has 41 closed vocabulary event types. It is currently an append-only JSONL file per project.

## Decision
Three separate event streams.

### Audit events
- Purpose: non-repudiable, legally important.
- Properties: actor, workspace, project, target_type, target_id, event_type, payload, timestamp, job_id.
- Storage: append-only, immutable, long retention.
- Closed vocabulary; unknown types raise.
- Current 41 types are the seed; v2 schema adds `workspace_id`, `tenant_id`, `request_id`, `job_id`.

### Domain events
- Purpose: trigger other bounded contexts.
- Examples: `render.completed`, `publish.intent.approved`.
- Storage: outbox table + queue.
- Consumers: analytics, billing, notifications, search index.

### Telemetry / log events
- Purpose: debugging, performance, cost.
- Examples: provider latency, filtergraph compile time.
- Storage: time-series/log sink, short retention.
- NOT used for audit or business logic.

## Consequences
- `AuditLog` keeps closed vocabulary; `DomainEventBus` is separate.
- Provider call events go to telemetry, not audit.
- Security: secrets redacted in all three.

## Rejected alternatives
- A. Single event stream. Rejected: different retention, consumer, and trust needs.
- B. Open audit vocabulary. Rejected: loses non-repudiation guarantees.

## Migration path
1. Add `AuditEvent` and `DomainEvent` schemas.
2. Refactor `AuditLog` to write to append-only DB table.
3. Add `DomainEventBus` with outbox pattern.
4. Route `provider.call` to telemetry.
