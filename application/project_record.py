"""The project record factory.

WHY THIS IS NOT A `ProjectService`
----------------------------------
Attaching a source to a project that has no `project.json` yet must bootstrap
one -- both the upload path and the trend-ingest path have always done this via
`PipelineService.create_project`. Once source attachment moved into
`SourceService`, that single call would have become a façade back-reference.

Project LIFECYCLE (rename, reset, rewind, delete, archive) deliberately stays in
`PipelineService`; extracting it is a separate milestone. Only this one creation
helper was isolated, so the record shape and the `project.created` audit event
have exactly one implementation shared by the façade and `SourceService`.
"""
from __future__ import annotations

from .time_utils import now_iso


def new_project_record(ownership, *, title: str, created_by: str,
                       operator_name: str | None = None) -> dict:
    """Build the initial `project` artifact body. Pure: performs no IO."""
    operator = str(operator_name or created_by or "console").strip()[:120]
    return {
        "schema_version": 1,
        "artifact_type": "console_project",
        **ownership.to_dict(),
        "title": str(title or "Isimsiz proje").strip()[:200],
        "notes": "",
        "created_by": str(created_by or "console")[:120],
        "operator_name": operator,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "status": "review_required",
        "status_reason": "proje olusturuldu; kaynak video ve Faz 0 plani bekleniyor",
        "source": None,
        "not_uploaded": True,
    }


def create_project(*, artifacts, audit, namespace, ownership, title: str,
                   created_by: str, operator_name: str | None = None) -> dict:
    """Create the namespace, persist the project artifact, audit the creation.

    The write order is part of the contract: the namespace exists before the
    artifact is written, and `project.created` is recorded after the artifact
    it describes is durable.
    """
    record = new_project_record(ownership, title=title, created_by=created_by,
                                operator_name=operator_name)
    namespace.ensure_namespace(ownership)
    artifacts.write_artifact("project", record, actor=created_by)
    audit.record("project.created", actor=created_by,
                 detail={"title": record["title"]})
    return record
