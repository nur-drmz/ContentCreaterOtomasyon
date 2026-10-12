"""Binary object storage port.

WHY THIS IS SEPARATE FROM THE REPOSITORY PORTS
----------------------------------------------
Repositories persist STRUCTURED state: project metadata, artifact metadata, jobs,
approvals, workflow state, audit. This port persists BYTES: source video, audio,
images, proxies, render output. Blurring the two is how media ends up in a
relational database.

THE FFMPEG PROBLEM THIS PORT EXISTS TO SOLVE
--------------------------------------------
`local_pipeline` and ffmpeg require a real local filesystem path. A cloud object
store has no such thing. Rather than teaching `local_pipeline` about buckets and
URLs -- which would push infrastructure knowledge into the deterministic media
engine -- the lifecycle is explicit and owned by the application layer:

    ObjectStore key
          v  materialize()          (download to a local working copy)
    local filesystem path
          v
    local_pipeline / ffmpeg
          v
    local filesystem path
          v  put_file()             (upload the result back)
    ObjectStore key

For the local adapter both `materialize()` and `put_file()` are effectively
no-ops: the "working copy" IS the stored file, so development pays nothing for an
abstraction that only matters in production.

`materialize()` returns a context manager so a cloud adapter can delete its
temporary copy deterministically, while the local adapter simply yields the real
path and cleans up nothing.

SEMANTICS A CONFORMING IMPLEMENTATION MUST PRESERVE
---------------------------------------------------
isolation     Keys are namespaced per workspace/project. A key may not address
              another tenant's object, and traversal must be rejected.
opaque keys   A key is an opaque namespace-relative identifier, never a
              client-supplied absolute host path.
integrity     `stat()` reports size and, where known, a SHA-256 digest, so a
              caller can verify bytes rather than trust a filename.
no silent     A missing object raises on read. It must not yield empty bytes that
success       a later stage would treat as valid media.
"""
from __future__ import annotations

from typing import IO, Any, ContextManager, Protocol, TypedDict, runtime_checkable


class ObjectStat(TypedDict):
    """Metadata about a stored object."""

    key: str
    size_bytes: int
    exists: bool
    sha256: str | None
    content_type: str | None


@runtime_checkable
class ObjectStore(Protocol):
    """Binary object storage for one project namespace."""

    def put_bytes(self, ownership, key: str, payload: bytes, *,
                  content_type: str | None = None) -> ObjectStat:
        """Store `payload` under `key`, atomically, and return its stat."""
        ...

    def put_file(self, ownership, key: str, source_path, *,
                 content_type: str | None = None) -> ObjectStat:
        """Store the bytes of a local file under `key`.

        This is the upload half of the ffmpeg lifecycle: a render produced on the
        local filesystem is published back into the store.
        """
        ...

    def open(self, ownership, key: str) -> ContextManager[IO[bytes]]:
        """Open the object for binary reading. Must raise when absent."""
        ...

    def stat(self, ownership, key: str) -> ObjectStat:
        """Metadata for `key`. `exists=False` when absent, rather than raising."""
        ...

    def exists(self, ownership, key: str) -> bool:
        """Whether `key` holds an object."""
        ...

    def delete(self, ownership, key: str) -> bool:
        """Delete `key` where retention policy permits. Returns whether it existed."""
        ...

    def materialize(self, ownership, key: str) -> ContextManager[Any]:
        """Yield a local filesystem path for `key`, for ffmpeg/local_pipeline.

        The local adapter yields the stored path itself and removes nothing. A
        cloud adapter downloads to a temporary working copy and deletes it on
        exit. Callers must not retain the path beyond the context.
        """
        ...
