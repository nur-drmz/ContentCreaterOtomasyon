"""The approval catalog: which artifacts the CONTENT approval gate covers.

WHY THIS IS A NEUTRAL MODULE
----------------------------
The registry used to be `PipelineService.APPROVAL_ARTIFACTS`. Both the façade
and `ApprovalService` need it, so it is defined once here. `pipeline_service`
re-exports it as a class attribute, which keeps
`service.APPROVAL_ARTIFACTS` working for every existing caller.

WHAT IS AND IS NOT IN HERE
--------------------------
These five kinds share ONE mechanism: a human echoes the SHA-256 of the exact
content they reviewed, and `local_pipeline.approval` promotes the artifact only
if that digest still matches. Nothing else belongs in this registry.

Publication approvals are deliberately ABSENT. `publication_metadata`,
`publish_intent` and `publish_manifest` have their own state machines
(`metadata_approval`, publish intent/visibility blocks) implemented in
`local_pipeline.metadata_studio` / `local_pipeline.publish`, with their own
digests and their own façade methods. Merging them here would let one generic
"approve" call authorize an upload, which is exactly the boundary that must not
blur: approving content is not authorizing publication. A future PublishService
owns those; see APPROVAL_SERVICE.md.
"""
from __future__ import annotations

#: Approval kind -> the logical artifact key it approves.
APPROVAL_ARTIFACTS = {
    "processing_plan": "processing_plan",
    "script": "selected_script",
    "voiceover": "selected_voiceover",
    "transcript": "moment_transcript",
    "render": "render_manifest",
}

#: Kinds whose approval ALSO requires a completed human review checklist, on top
#: of the canonical hash gate. The hash proves the reviewer saw the bytes; the
#: checklist proves they checked the things that actually go wrong.
CHECKLIST_GATED_KINDS = ("render", "voiceover")

#: The one kind gated on SOURCE RIGHTS rather than on something this project
#: produced. It is the earliest gate in the pipeline, so a project with no
#: basis for its footage is stopped before analysis or render time is spent.
RIGHTS_GATED_KIND = "processing_plan"
