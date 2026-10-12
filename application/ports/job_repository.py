"""Job persistence port.

Derived from `creator_console.jobs.JobStore`, which the local adapter already
satisfies. The method set is deliberately NOT generic CRUD: every method below
carries a semantic guarantee a future PostgreSQL adapter must reproduce.

SEMANTICS A CONFORMING IMPLEMENTATION MUST PRESERVE
---------------------------------------------------
idempotency   `submit()` returns `(record, created=False)` when an equivalent job
              is already queued/running/completed/review_required for the same
              `idempotency_key`. A duplicate submission must not schedule work.

single claim  `claim()` is a compare-and-set. Exactly one caller may move a job
              from queued to running; a second caller must raise `JobConflict`
              while the lease is live.

lease         A claim carries `lease_expires_at`. `heartbeat()` extends it and
              must reject a worker that does not hold the claim.

recovery      `recover_abandoned()` may only reclaim a job whose lease expired OR
              whose owner process is provably dead on this host. A job with a
              live owner is never reclaimed.

retry         `request_retry()` accepts only terminal, non-successful jobs and
              must refuse once `attempts >= max_attempts`.

cancellation  `request_cancel()` terminates a queued job immediately but leaves a
              running job in `running` with `cancel_requested_at` set, so the UI
              never claims work stopped while ffmpeg is still unwinding.

two statuses  `status` is EXECUTION state; `artifact_status` is whatever the
              pipeline wrote. They must stay separate fields.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class JobRepository(Protocol):
    """Durable job state for one project namespace."""

    def get(self, job_id: str) -> dict[str, Any]:
        """Return one job record, or raise `JobNotFound`.

        A record that is unreadable must raise rather than return a default: a
        caller must never be handed a plausible-looking empty job.
        """
        ...

    def list(self, *, statuses=None, job_type=None, limit: int | None = None) -> list[dict[str, Any]]:
        """Return job records, newest first.

        A corrupt record must be SURFACED as a failed/recoverable entry, not
        skipped silently.
        """
        ...

    def submit(self, job_type: str, *, idempotency_key: str, params=None,
               submitted_by: str = "console") -> tuple[dict[str, Any], bool]:
        """Create a job, or return the existing one for this idempotency key.

        Returns `(record, created)`. `created=False` means no new work was
        scheduled. An unknown `job_type` must raise.
        """
        ...

    def claim(self, job_id: str, worker_id: str) -> dict[str, Any]:
        """Atomically move queued -> running for exactly one worker.

        Must raise `JobConflict` when another worker holds a live lease, and when
        the job is in a terminal state. Increments `attempts` and records the
        owner process/host so recovery can tell a dead thread from a live worker.
        """
        ...

    def heartbeat(self, job_id: str, worker_id: str) -> dict[str, Any]:
        """Extend the lease. Must raise `JobConflict` for a non-holder."""
        ...

    def finish(self, job_id: str, status: str, *, artifact_status=None, result=None,
               artifacts=None, error=None, error_kind=None,
               recoverable: bool = False) -> dict[str, Any]:
        """Move a job to a terminal status and release the lease.

        A non-terminal `status` must raise. `error` must be scrubbed before it is
        persisted: an error string is the most likely place for a credential to
        escape.
        """
        ...

    def request_cancel(self, job_id: str, *, requested_by: str = "console") -> dict[str, Any]:
        """Request cooperative cancellation without pretending work stopped."""
        ...

    def cancel_requested(self, job_id: str) -> bool:
        """Cheap checkpoint for a running pipeline. False for an unknown job."""
        ...

    def request_retry(self, job_id: str, *, requested_by: str = "console") -> dict[str, Any]:
        """Requeue a terminal, non-successful job within its attempt budget."""
        ...

    def recover_abandoned(self, *, ignore_lease: bool = False,
                          reclaim_dead_owners: bool = True) -> list[dict[str, Any]]:
        """Move genuinely abandoned jobs out of `running`.

        Leaving a job stuck in `running` forever is the worst outcome: the UI
        spins on work nobody is doing and the user cannot retry.
        """
        ...
