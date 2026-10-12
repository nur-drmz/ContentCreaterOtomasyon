"""Project namespace port.

Derived from `creator_console.storage.LocalFilesystemStorage` and the project
lifecycle methods of `creator_console.pipeline_service`.

WHY THE NAME CHANGED (was `ProjectRepository`)
----------------------------------------------
The methods below are NAMESPACE and path-safety operations, not project
metadata persistence. "ProjectRepository" implied the future PostgreSQL table
of project rows; a caller expecting metadata CRUD would have been misled.
Project metadata itself is persisted as the `"project"` artifact through
`ArtifactRepository`; inventing a second parallel project-metadata contract
here would duplicate it.

SEMANTICS A CONFORMING IMPLEMENTATION MUST PRESERVE
---------------------------------------------------
isolation      One namespace per `(workspace_id, project_id)`. Nothing may read
               or write outside the namespace it was given.

loud failure   Traversal (`../`), absolute paths, drive letters, UNC paths,
               unsafe filenames and symlink escapes are REJECTED, not sanitised
               and accepted, so a caller bug surfaces as an error instead of a
               silent cross-workspace read.

relative out   `relative_to_namespace()` returns a POSIX relative string.
               Absolute host paths are deliberately not exposed: they leak the
               operator's directory layout and are useless to a client.

recoverable    "Delete" moves one exact namespace into workspace trash and
delete         returns a recovery token. The bytes stay recoverable. The target
               is derived only from validated ownership, never from a
               caller-supplied path, glob or parent directory.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ProjectNamespaceStore(Protocol):
    """Namespace resolution and lifecycle for one workspace's projects."""

    def namespace_root(self, ownership) -> Any:
        """The deepest namespace this ownership may touch."""
        ...

    def ensure_namespace(self, ownership) -> Any:
        """Create the namespace if absent and return it."""
        ...

    def resolve(self, ownership, *parts, create_parent: bool = False) -> Any:
        """Resolve relative segments inside the namespace, or raise `StorageViolation`."""
        ...

    def relative_to_namespace(self, ownership, path) -> str:
        """Namespace-relative POSIX string for artifacts and API responses."""
        ...

    def exists(self, ownership, relative) -> bool:
        """Whether a relative path exists. A violation returns False, not a raise."""
        ...

    def list_dir(self, ownership, relative: str = "") -> list:
        """Sorted entry names in a namespace directory; empty list when absent."""
        ...

    def trash_namespace(self, ownership) -> str:
        """Move one exact project namespace to workspace trash; return a token."""
        ...
