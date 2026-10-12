"""Local implementation of `application.ports.ProgressFactory`.

WHY THE CANCEL CHECK LIVES HERE
-------------------------------
`ProgressTracker` takes a `cancel_check` callable, and the job runtime only
knows which job is running AFTER the service graph has been built -- the console
binds it per execution (`ConsoleApp._execute_job` →
`PipelineService.bind_cancel_check`). Previously the façade held that mutable
callable, which is why services had to ask the façade for a tracker.

This factory owns it instead: it is constructed per `(storage, ownership)` with
no cancel check, and the job runtime binds one later through
`bind_cancel_check`. Every tracker created afterwards sees the current signal,
so cancellation keeps working exactly as before while application services
depend only on the port.
"""
from __future__ import annotations

from creator_console.progress import NullTracker, ProgressTracker


class LocalProgressFactory:
    """Builds `ProgressTracker`s for one project namespace."""

    def __init__(self, storage, ownership, cancel_check=None):
        #: `ports.ProjectNamespaceStore` -- where progress records are written.
        self.storage = storage
        self.ownership = ownership
        self._cancel_check = cancel_check

    def bind_cancel_check(self, callback) -> None:
        """Bind the current job's cooperative stop signal.

        Called by the job runtime, not by application services. Trackers built
        after this call observe the signal; trackers already handed out keep the
        callable they were given, which is the same object.
        """
        self._cancel_check = callback

    @property
    def cancel_check(self):
        return self._cancel_check

    def create(self, activity: str, *, job_id=None, attempt: int = 1,
               stages=None, track: bool = True):
        """Return a tracker for `activity`, or a no-op one when `track=False`.

        Separate from the audit log on purpose: the audit log records what
        HAPPENED (append-only, for accountability), progress records what is
        happening RIGHT NOW (overwritten, for the UI).
        """
        if not track:
            return NullTracker()
        return ProgressTracker(
            self.storage, self.ownership, activity,
            job_id=job_id, attempt=attempt, stages=stages,
            cancel_check=self._cancel_check)
