"""Progress-reporting port.

WHY THIS EXISTS
---------------
Milestone 3 handed `SourceService` a `PipelineService` bound method as its
tracker factory. That satisfied every import guard while still being a
dependency on the façade -- exactly the pattern ADR-001 now forbids. This port
is the replacement: an application service asks for a tracker by activity and
never learns how progress is persisted, who owns the cancel signal, or that an
untracked call uses a no-op sink.

SEMANTICS A CONFORMING IMPLEMENTATION MUST PRESERVE
---------------------------------------------------
never fatal      A tracker that cannot write must NOT take the phase down with
                 it. Progress is a UI convenience; losing it is not worth
                 losing a render. Write failures are swallowed by the tracker.

cancellation     `raise_if_cancelled()` (called inside `stage`/`finish`) stops
                 the record and raises `OperationCancelled` at a safe pipeline
                 boundary. The cancel signal belongs to the job runtime and is
                 bound by the composition root, not by the service.

untracked        `track=False` yields a no-op tracker with the same surface, so
                 a caller stays unconditional.

one activity     `activity` must be a key the progress catalog knows; its label,
                 tab and default stages come from there, so the UI renders
                 exactly one honest panel per screen.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ProgressTracker(Protocol):
    """The subset of the tracker surface application services actually use."""

    def start(self, message: str = "") -> None: ...

    def stage(self, key: str, message: str = "") -> None: ...

    def count(self, completed, total, *, unit: str = "", message: str = "") -> None: ...

    def note(self, message: str) -> None:
        """Update the free-text message without advancing the stage or count."""
        ...

    def finish(self, *, message: str = "") -> None: ...

    def fail(self, error: str, *, message: str = "") -> None: ...


@runtime_checkable
class ProgressFactory(Protocol):
    """Builds the tracker for one activity inside one project namespace."""

    def create(self, activity: str, *, job_id: str | None = None,
               attempt: int = 1, stages=None, track: bool = True) -> Any:
        """Return a `ProgressTracker` for `activity`.

        `track=False` must return a no-op tracker rather than `None`, so the
        caller never branches on it.
        """
        ...
