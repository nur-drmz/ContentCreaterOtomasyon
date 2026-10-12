"""Read-only source query port.

WHY A PORT INSTEAD OF PASSING `SourceService` DIRECTLY
------------------------------------------------------
`EditorService` needs exactly two facts about the source: where it is and how
long it is. `SourceService` also ATTACHES and REPLACES sources. Handing the
whole service to the editor would grant it authority it must never use -- an
editor bug could re-point a project's source -- and would couple the two
services' full surfaces together.

This port is the smaller clean abstraction: two read methods, satisfied
structurally by `SourceService`, and trivially satisfiable by a test double. It
also keeps the direction honest: the editor depends on a *contract*, so the
source implementation can later be swapped (e.g. a cached descriptor reader)
without touching the editor.

SEMANTICS A CONFORMING IMPLEMENTATION MUST PRESERVE
---------------------------------------------------
path        `get_source_path()` returns an existing LOCAL filesystem path, so
            `local_pipeline`/ffmpeg can read it directly. It RAISES when no
            source is attached or the file is gone -- it never returns a
            plausible-looking missing path.

duration    `get_duration()` returns the measured duration in seconds, or
            `None` when it is genuinely unknown. `None` must not be replaced by
            a default: a timeline that invents a duration mis-scales every clip.

fingerprint `get_fingerprint()` returns the SOURCE digest that source-rights
            answers are bound to, or `None` when nothing is attached. It must
            change when the source file is swapped, so replacing a source
            resets the copyright checklist instead of silently inheriting it.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SourceQuery(Protocol):
    """The read-only view of a project's attached source media."""

    def get_source_path(self) -> Any:
        """Local path of the attached source, or raise when unavailable."""
        ...

    def get_duration(self) -> float | None:
        """Measured source duration in seconds, or `None` when unknown."""
        ...

    def get_fingerprint(self) -> str | None:
        """Source digest that rights answers bind to, or `None` when absent."""
        ...
