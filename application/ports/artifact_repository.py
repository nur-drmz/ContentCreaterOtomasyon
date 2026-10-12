"""Artifact persistence port.

Derived from the artifact methods of `creator_console.pipeline_service`:
`artifact_relative`, `load_artifact`, `load_artifact_path`, `write_artifact`.

The contract is intentionally expressed in terms of LOGICAL artifact keys
(`"edl"`, `"processing_plan"`, ...) and namespace-relative POSIX paths, because
those are what the current system persists and what the API already returns. A
future PostgreSQL adapter stores the same logical identity plus metadata; it must
not require the application to learn database row ids.

SEMANTICS A CONFORMING IMPLEMENTATION MUST PRESERVE
---------------------------------------------------
logical keys   `ARTIFACTS` names and their relative paths are part of the
               compatibility surface. Per-slot artifacts keep the
               `results/render/output_NN/<name>` layout for slot > 1.

honest reads   `load()` REPORTS trustworthiness instead of repairing: a corrupt,
               schema-incompatible or field-missing artifact yields `data=None`
               plus a human-actionable `problem`. It is never guessed at or
               replaced by a plausible default.

no absolute    Returned paths are namespace-relative. An absolute host path must
paths          never reach an artifact field or the browser.

atomic writes  A write is atomic, so a crash cannot leave a "completed" job
               pointing at truncated JSON.

audited        A KEYED write (`write_artifact`) records an `artifact.written`
               audit event. Raw path-level writes (`write_artifact_path`,
               added in Milestone 11) deliberately do NOT: they are the
               persistence primitive for documents outside the catalog --
               `results/clip_bounds/output_NN.json` is written through them
               and the caller emits its domain event (`clip_bounds.saved`)
               instead. A keyed write's audit is the catalog's contract; a
               path write's audit is the caller's.
"""
from __future__ import annotations

from typing import Any, Protocol, TypedDict, runtime_checkable


class ArtifactRecord(TypedDict):
    """What an artifact read reports. Mirrors the current `load_artifact` shape."""

    key: str
    slot: int
    present: bool
    data: dict[str, Any] | None
    problem: str | None
    relative_path: str


@runtime_checkable
class ArtifactRepository(Protocol):
    """JSON artifact persistence for one project namespace."""

    def artifact_relative(self, key: str, slot: int = 1) -> str:
        """Namespace-relative POSIX path for a logical artifact key and slot."""
        ...

    def load_artifact(self, key: str, *, required: bool = False,
                      slot: int = 1) -> ArtifactRecord:
        """Read one registered artifact and report its trustworthiness."""
        ...

    def load_artifact_path(self, relative: str) -> dict[str, Any] | None:
        """Read one JSON document by relative path, or None when absent/unreadable."""
        ...

    def write_artifact(self, key: str, data, *, job_id=None, actor: str = "console",
                       slot: int = 1) -> str:
        """Atomically write an artifact and return its namespace-relative path."""
        ...

    def write_artifact_path(self, relative: str, data) -> str:
        """Atomically write one JSON document by relative path.

        For documents that are NOT catalog artifacts (e.g.
        `results/clip_bounds/output_NN.json`): same atomic write and namespace
        containment guarantees as `write_artifact`, but NO `artifact.written`
        audit event -- the caller emits the domain event instead.
        """
        ...
