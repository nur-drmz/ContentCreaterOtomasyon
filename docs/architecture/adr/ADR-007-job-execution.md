# ADR-007 — Job and Distributed Execution

## Status
Accepted

## Context
Current `creator_console/jobs.py` provides idempotency key, compare-and-set claim, lease, heartbeat, and retry. Jobs are JSON files.

## Decision
Database is the source of truth; queue is an execution signal.

Canonical job record fields:
- `job_id`, `idempotency_key`, `job_type`
- `project_id`, `workspace_id`, `tenant_id`
- `status`, `artifact_status`
- `params` (JSON, scrubbed)
- `attempts`, `max_attempts`
- `claimed_by`, `lease_expires_at`, `heartbeat_at`
- `created_at`, `started_at`, `completed_at`, `cancelled_at`
- `result` (summary only, no large blobs)

Queue message role:
- Contains only `job_id` and `priority`.
- Worker polls DB for `status = queued` before claiming.
- No job state in the queue message itself.

Lease and recovery:
- Worker claims by `UPDATE jobs SET claimed_by = ..., lease_expires_at = ... WHERE status='queued' AND claimed_by IS NULL`.
- Heartbeat updates `heartbeat_at`.
- `recover_abandoned()` marks jobs with `lease_expires_at < now()` as failed + recoverable.

Retry rules:
- `MAX_ATTEMPTS = 3` default.
- Backoff: `2^attempt * 5s` with jitter.
- Cancellation: request sets `cancel_requested`; worker checks before next stage.

API visibility:
- `GET /jobs` lists jobs for the project.
- `GET /jobs/{id}` returns record.
- `POST /jobs/{id}/retry` for `failed` or `review_required`.

## Consequences
- Idempotency, single-claim, lease semantics survive migration.
- Queue can be Redis/RabbitMQ/SQS; DB remains authoritative.
- Worker can be a separate container or local process.

## Rejected alternatives
- A. Pure queue state (e.g., Celery result backend). Rejected: loses idempotency/lease if queue is lost.
- B. Worker in HTTP process. Rejected: not horizontally scalable.

## Migration path
1. Add `JobRepository` interface with local and PostgreSQL implementations.
2. Keep `JobStore` as the public class.
3. Add queue publisher and consumer abstractions.
4. Containerize worker.
