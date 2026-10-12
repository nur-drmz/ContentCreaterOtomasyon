"""Persistence and storage ports.

WHY PROTOCOLS AND NOT BASE CLASSES
----------------------------------
The existing local implementations (`LocalFilesystemStorage`, `JobStore`,
`AuditLog`) already satisfy these contracts structurally. Declaring the ports as
`typing.Protocol` means they are recognised WITHOUT touching those classes or
adding an inheritance edge, so this milestone introduces zero runtime change.

WHY NOT A GENERIC REPOSITORY
----------------------------
`get/save/delete` for every entity would throw away the semantics that actually
matter here: idempotent submission, single claim, lease ownership, closed audit
vocabulary, namespace isolation. Each port below is derived from the operations
the current code really performs.
"""
from .approval_gate import ApprovalInvalidator, ApprovalQuery
from .artifact_repository import ArtifactRecord, ArtifactRepository
from .audit_sink import AuditSink
from .job_repository import JobRepository
from .namespace_store import ProjectNamespaceStore
from .object_store import ObjectStat, ObjectStore
from .progress import ProgressFactory, ProgressTracker
from .review_gate import ReviewGate
from .revision_archiver import RevisionArchiver
from .source_query import SourceQuery
from .workflow_gate import WorkflowGateQuery

__all__ = [
    "ApprovalInvalidator",
    "ApprovalQuery",
    "ArtifactRecord",
    "ArtifactRepository",
    "AuditSink",
    "JobRepository",
    "ObjectStat",
    "ObjectStore",
    "ProgressFactory",
    "ProgressTracker",
    "ProjectNamespaceStore",
    "ReviewGate",
    "RevisionArchiver",
    "SourceQuery",
    "WorkflowGateQuery",
]
