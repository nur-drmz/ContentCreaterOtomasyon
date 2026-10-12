"""Local filesystem persistence adapters.

These bind the CURRENT production implementations to the `application.ports`
contracts. `LocalFilesystemStorage`, `JobStore` and `AuditLog` already satisfy
their ports structurally, so they are re-exported under the port-facing names
without wrappers or inheritance edges. `LocalArtifactRepository` is a real
adapter: it owns the artifact read/write rules the façade used to implement,
so application services can depend on the port without a circular reference
back to `PipelineService`.

A later PostgreSQL package will sit beside this one and implement the same
ports. Per the corrected ADR-003 there is no dual-write: one project has
exactly one canonical backend at a time.
"""
from creator_console.audit import AuditLog as LocalAuditSink
from creator_console.jobs import JobStore as LocalJobRepository
from creator_console.storage import (
    LocalFilesystemStorage as LocalProjectNamespaceStore,
)

from .artifact_repository import LocalArtifactRepository
from .revision_archive import LocalRevisionArchive

__all__ = [
    "LocalArtifactRepository",
    "LocalAuditSink",
    "LocalJobRepository",
    "LocalProjectNamespaceStore",
    "LocalRevisionArchive",
]
