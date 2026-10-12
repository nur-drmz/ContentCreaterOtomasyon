"""Error types shared by application services and the console façade.

WHY THESE LIVE HERE
-------------------
`PipelineServiceError` and `ArtifactProblem` were historically defined in
`creator_console.pipeline_service`, but the API layer maps them to specific
error codes (`artifact_problem`, classified service errors). Application
services must raise the SAME types -- `application/` may not import the façade
-- so the canonical definitions moved here and `pipeline_service` re-exports
them. The names are kept for compatibility: `PipelineServiceError` is the
console's service-layer error, raised by application services and the façade
alike.

SEMANTICS
---------
`PipelineServiceError`   A request was understood but cannot be carried out
                         (invalid clip index, invalid EDL, missing input).
                         The API maps it to a classified client error.

`ArtifactProblem`        An artifact EXISTS but cannot be trusted (corrupt,
                         schema-incompatible, hand-edited). Distinct from
                         "absent": it is never repaired, guessed at, or
                         replaced by a default. Mapped to `artifact_problem`.
"""
from __future__ import annotations


class PipelineServiceError(RuntimeError):
    pass


class ArtifactProblem(PipelineServiceError):
    """An artifact exists but cannot be trusted. Never downgraded to a default."""
