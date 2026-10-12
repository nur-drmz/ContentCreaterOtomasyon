"""Revision-archival port.

WHY A PORT INSTEAD OF LEAVING THIS ON THE FACADE
------------------------------------------------
`save_clip_bounds` (moving into `MomentService`) invalidates the rendered
output and -- in direct mode -- the approved transcript, and the invalidation
mechanism is the recoverable `.revisions/` snapshot. That mechanism is SHARED
with `rewind_project`, render-profile edits and the review flows, so it cannot
become a moment private. But it also cannot stay a façade private, because a
service receiving `service._archive_revision` would be a façade-owned callable
-- exactly the dependency shape ADR-001 forbids.

The seam is genuinely infrastructure: "move these namespace-relative paths
into a recoverable snapshot" + "which namespace paths constitute this output
slot's render". No plan, approval or moment semantics live here.

SEMANTICS A CONFORMING IMPLEMENTATION MUST PRESERVE
---------------------------------------------------
- **Archive means MOVE, not delete.** Artifacts land under
  `.revisions/<revision_id>/…` with a `project_revision` manifest;
  `recoverable: True` is the contract the restore path relies on.
- **Parent paths eat children.** A path that is already covered by an
  archived parent is skipped -- double-moving would turn the second move into
  silently missing data.
- **Slot isolation is the whole point of `render_slot_archive_paths`.**
  `results/render/` contains `output_02`, `output_03`, …; archiving a slot's
  render must never take another Short with it, and `preserve` names files
  (the moment selection, the transcript) that must survive inside a wiped
  directory.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RevisionArchiver(Protocol):
    """Recoverable-namespace-snapshot capability shared by façade and services."""

    #: Filenames that live inside a slot's render directory but are INPUTS the
    #: next render still needs (the moment selection, the approved transcript)
    #: -- preserved by default; a direct-mode clip edit passes them out.
    RENDER_ARCHIVE_PRESERVED: tuple

    def archive_revision(self, paths, *, action: str, actor: str,
                         target: str | None = None) -> dict:
        """Move every given namespace-relative path into `.revisions/<id>/`
        and return the revision manifest."""
        ...

    def render_slot_archive_paths(self, slot: int = 1, *, preserve=None) -> list:
        """Namespace-relative paths that make up THIS slot's render output --
        the set a clip-bounds or render-profile edit must archive, never a
        neighbouring slot's."""
        ...
