"""Artifact catalog: the logical names, paths and trust rules of artifacts.

WHY THIS IS A NEUTRAL MODULE
----------------------------
The catalog used to live inside `creator_console.pipeline_service`, which made
the façade the only place that knew where an artifact lives and what makes it
trustworthy. Both the façade AND the local persistence adapter need that
knowledge, so it is defined here once:

    application/artifact_catalog.py
        ^                    ^
    PipelineService   LocalArtifactRepository

Nothing in this module performs IO. It is pure policy: logical key ->
namespace-relative path, per-slot layout, required fields, supported schema
versions and the `production_ready` allow-list. `pipeline_service` re-exports
these names so existing callers (`verify_*`, tests) keep working.
"""
from __future__ import annotations

from .errors import PipelineServiceError

RESULTS_DIRNAME = "results"

#: Where source media lives inside a project namespace. Shared layout policy:
#: `SourceService` writes here, and the façade's rewind/reset/ingest paths refer
#: to the same directory, so the name must have exactly one definition.
SOURCE_DIRNAME = "source_videos"

#: Namespace-local runtime state that is NOT an artifact: the OAuth token file,
#: the YouTube cache and transient work dirs live here. Shared with
#: `pipeline_service` (which re-exports it) so the publish service and the
#: façade can never disagree about where the token store is.
RUNTIME_DIRNAME = ".runtime"

#: Artifact names inside a project namespace. Relative on purpose: an absolute
#: host path must never reach the browser or an artifact field.
ARTIFACTS = {
    "project": "project.json",
    "source_candidates": "results/source_candidates.json",
    "rights_decision": "results/rights_decision.json",
    "ingest_manifest": "results/ingest_manifest.json",
    "publication_metadata": "results/publication_metadata.json",
    "publish_intent": "results/publish_intent.json",
    "publish_manifest": "results/publish_manifest.json",
    "edl": "results/edl/edl.json",
    "source_assessment": "results/source_assessment.json",
    "processing_plan": "results/processing_plan.json",
    "hot_moments": "results/hot_moments.json",
    "moment_selection": "results/moment_selection.json",
    "selected_script": "results/selected_script.json",
    "selected_voiceover": "results/selected_voiceover.json",
    "subtitle_track": "results/subtitle_track.json",
    "moment_transcript": "results/moment_transcript.json",
    "glossary": "results/glossary.json",
    "long_form_plan": "results/long_form_plan.json",
    "turbo_brief": "results/turbo/brief.json",
    "turbo_workflow": "results/turbo/workflow.json",
    "turbo_timeline": "results/turbo/timeline.json",
    "render_manifest": "results/render/render_manifest.json",
}

#: Artifacts a batch produces once PER SHORT. Everything else in ARTIFACTS
#: describes the project as a whole and is shared by every slot: the source
#: assessment, the approved plan and the analysis are the common ground all the
#: Shorts of one project are cut from, and duplicating them per slot would let
#: two Shorts of the same project disagree about their own source.
#:
#: Narration artifacts are absent on purpose: `build_processing_plan` refuses a
#: `narrated_theory` plan with output_count != 1, so a narrated project has
#: exactly one slot and its script/voiceover cannot be ambiguous.
PER_SLOT_ARTIFACTS = frozenset({"moment_selection", "moment_transcript", "render_manifest"})

#: Artifact kind -> the schema fields that must exist for it to be usable at all.
REQUIRED_FIELDS = {
    "source_candidates": ("status", "candidates", "requires_rights_decision"),
    "rights_decision": ("artifact_kind", "status", "decision", "decision_sha256"),
    "ingest_manifest": ("artifact_kind", "status", "candidate_url", "rights"),
    "publication_metadata": ("artifact_kind", "status", "title_variants"),
    "publish_intent": ("artifact_kind", "status", "decision", "decision_sha256"),
    "publish_manifest": ("artifact_kind", "status", "platform", "upload"),
    "edl": ("artifact_type", "status", "title", "width", "height", "clips"),
    "source_assessment": ("artifact_type", "status", "source", "mode_scores"),
    "processing_plan": ("artifact_type", "status", "decision", "phase_gates"),
    "hot_moments": ("status", "hot_moments"),
    "moment_selection": ("artifact_type", "status", "selected_moment_rank"),
    "selected_script": ("status", "provider_attempts"),
    "selected_voiceover": ("status",),
    "subtitle_track": ("artifact_type", "status", "cues"),
    "moment_transcript": ("artifact_type", "status", "words", "cues"),
    "glossary": ("artifact_type", "status", "terms"),
    "long_form_plan": ("artifact_type", "status", "topic", "content_type",
                       "target_duration_min", "chapter_count"),
    "turbo_brief": ("artifact_type", "topic", "duration_sec", "aspect_ratio",
                    "brief_sha256"),
    "turbo_workflow": ("artifact_type", "status", "stages"),
    "turbo_timeline": ("artifact_type", "status", "clips", "video", "audio",
                       "duration_sec"),
    "render_manifest": ("status", "output", "approval"),
}

#: v2 adds the transcript's raw-ASR copy and revision history. Both versions
#: stay readable: a project written before revisions existed is upgraded in
#: memory on read and only rewritten when its owner actually edits it.
SUPPORTED_SCHEMA_VERSIONS = {1, 2}

#: `production_ready` is normally proof that a file was hand-edited, because no
#: phase in this project may promote itself. The publish manifest is the ONE
#: exception: a video that a human approved for public visibility and that the
#: platform confirmed as public genuinely IS live, and calling that
#: `review_required` would be the dishonest option.
PRODUCTION_READY_ALLOWED = frozenset({"publish_manifest"})


def artifact_relative(key: str, slot: int = 1) -> str:
    """Namespace-relative path of an artifact FOR ONE OUTPUT SLOT.

    Only the artifacts a batch produces once per Short are slot-scoped. The
    source assessment, the plan and the analysis describe the project as a
    whole and are deliberately shared: re-deriving them per slot would let
    two Shorts of the same project disagree about the source they came from.

    Slot 1 resolves to the historical path, so every existing project,
    artifact and test keeps working unchanged.
    """
    relative = ARTIFACTS[key]
    index = int(slot or 1)
    if index < 1:
        raise PipelineServiceError(f"gecersiz cikti yuvasi: {slot!r}")
    if index == 1 or key not in PER_SLOT_ARTIFACTS:
        return relative
    name = relative.rsplit("/", 1)[-1]
    return f"{RESULTS_DIRNAME}/render/output_{index:02d}/{name}"


# Rewinding never deletes user work.  The selected stage and everything after
# it is moved into `.revisions/<id>/...`, while upstream inputs remain live.
REWIND_PATHS = {
    "assessment": (
        "results/source_assessment.json", "results/processing_plan.json",
        "results/hot_moments.json", "results/moment_previews",
        "results/moment_selection.json", "results/selected_script.json",
        "results/selected_voiceover.json", "results/tts", "results/voice_review.json",
        "results/render", "results/render_review.json", "results/automation.json",
        "progress",
    ),
    "plan": (
        "results/processing_plan.json", "results/hot_moments.json",
        "results/moment_previews", "results/moment_selection.json",
        "results/selected_script.json", "results/selected_voiceover.json",
        "results/tts", "results/voice_review.json", "results/render",
        "results/render_review.json", "results/automation.json", "progress",
    ),
    "analysis": (
        "results/hot_moments.json", "results/moment_previews",
        "results/moment_selection.json", "results/selected_script.json",
        "results/selected_voiceover.json", "results/tts", "results/voice_review.json",
        "results/render", "results/render_review.json", "results/automation.json",
        "progress",
    ),
    "moment": (
        "results/moment_selection.json", "results/selected_script.json",
        "results/selected_voiceover.json", "results/tts", "results/voice_review.json",
        "results/render", "results/render_review.json", "results/automation.json",
        "progress",
    ),
    "script": (
        "results/selected_script.json", "results/selected_voiceover.json",
        "results/tts", "results/voice_review.json", "results/render",
        "results/render_review.json", "results/automation.json", "progress",
    ),
    "voice": (
        "results/selected_voiceover.json", "results/tts", "results/voice_review.json",
        "results/render", "results/render_review.json", "results/automation.json",
        "progress",
    ),
    "render": (
        "results/render", "results/render_review.json", "results/automation.json",
        "progress",
    ),
}

#: The caption transcript was missing from EVERY stage, so rewinding to the
#: analysis or the moment left the previous moment's transcript in place and the
#: next run silently reused it. Slots 2+ keep theirs under `results/render/...`
#: and were already covered; only slot 1's historical path was orphaned.
#:
#: It stops at `script` because in `direct_clip_series` the transcript IS that
#: stage's artifact. Rewinding to `voice` or `render` must NOT discard it -- the
#: transcript is upstream of both, and throwing away a reviewed caption to redo
#: an encode would destroy human work for no reason.
#:
#: `results/glossary.json` is deliberately absent from all of them: proper names
#: are knowledge about the SOURCE, not output of a stage, and a reviewer who
#: taught the system "Sheepstealer" should not have to teach it again because
#: they re-ran the analysis.
REWIND_PATHS = {
    stage: (paths + ("results/moment_transcript.json",)
            if stage in ("assessment", "plan", "analysis", "moment", "script")
            else paths)
    for stage, paths in REWIND_PATHS.items()
}

# Narrated subtitles are derived from the approved voice. Preserve manual text
# edits across a plain re-render, but archive them when the plan, moment, script
# or voice changes because their timings/text basis is then stale.
REWIND_PATHS = {
    stage: (paths + ("results/subtitle_track.json",)
            if stage in ("assessment", "plan", "analysis", "moment", "script", "voice")
            else paths)
    for stage, paths in REWIND_PATHS.items()
}

# Framing adjustments belong to the selected moment. Re-encoding the same
# moment preserves them; selecting a different moment archives them so an old
# face focus cannot silently carry onto a new shot.
REWIND_PATHS = {
    stage: (paths + ("results/render_adjustments",)
            if stage in ("assessment", "plan", "analysis", "moment")
            else paths)
    for stage, paths in REWIND_PATHS.items()
}

#: A publish INTENT authorizes ONE upload of ONE exact video hash, so rewinding
#: to any stage at or above the render invalidates it and it is archived with the
#: rest. `publish_manifest.json` is deliberately NOT archived: it is the
#: permanent record of what actually went live, and rewinding a local project
#: cannot un-publish a video that already exists on a platform.
REWIND_PATHS = {
    stage: (paths + ("results/publish_intent.json",)
            if "results/render" in paths else paths)
    for stage, paths in REWIND_PATHS.items()
}
