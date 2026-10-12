"""EditorService: the application service for EDL/scene-editor use cases.

WHAT THIS OWNS
--------------
Orchestration of edit DECISIONS: creating and loading the EDL artifact, adding
and updating clips, music assignment, preview-command compilation, and the
revision request/resolve lifecycle -- including persistence through the
artifact port and the `edl.*` audit events.

WHAT IT DELIBERATELY DOES NOT OWN
---------------------------------
- Deterministic edit math (crop/zoom/bounds/caption/filtergraph/serialisation):
  that stays in `local_pipeline.scene_editor`, the canonical engine.
- Render EXECUTION: `scene_editor.render_preview` spawns ffmpeg and stays in
  the `PipelineService` facade until a `RenderService` exists. This service
  only COMPILES the preview command (`build_preview_command`) -- a pure EDL ->
  argument-list derivation that never spawns a process.
- Source ownership: the editor READS the attached source through the narrow
  `ports.SourceQuery` contract (`get_source_path`, `get_duration`), implemented
  by `SourceService`. It cannot attach, replace or probe a source. Milestone 2
  injected two `PipelineService` bound methods here; Milestone 3 replaced them
  with that port, so the editor no longer depends on the façade in any form.
- HTTP, FastAPI, React, `ConsoleApi`, `PipelineService`: this module must not
  import any of them (enforced by the architecture guards).

WHY NO COMMAND OBJECTS
----------------------
The update surface is already a set of named, optional keyword arguments
(`start`, `end`, `crop`, `zoom`, `caption_style`, `b_roll`, `add_b_roll`), not
ambiguous dictionaries -- the API layer parses payloads into these exact
parameters. Wrapping them in dataclasses would add ceremony without new
information, so typed commands are deferred until a boundary actually gains
ambiguity.
"""
from __future__ import annotations

from application import artifact_catalog
from application.errors import ArtifactProblem, PipelineServiceError
from local_pipeline import scene_editor


class EditorService:
    """EDL/editor use cases for ONE project namespace.

    Constructed per project (matching the console's ownership-scoped service
    lifetime) by the composition root or by the facade's compatibility
    defaults.
    """

    def __init__(self, *, artifacts, audit, namespace, ownership, source):
        #: `ports.ArtifactRepository` scoped to this project.
        self._artifacts = artifacts
        #: `ports.AuditSink` scoped to this project.
        self._audit = audit
        #: `ports.ProjectNamespaceStore` -- resolves the preview output dir.
        self._namespace = namespace
        self._ownership = ownership
        #: `ports.SourceQuery` -- read-only view of the attached source.
        self._source = source

    # -------------------------------------------------------------- reads
    def load_edl(self) -> scene_editor.EDL:
        """Load the EDL artifact, or raise `ArtifactProblem` when untrusted."""
        record = self._artifacts.load_artifact("edl", required=True)
        if record["problem"]:
            raise ArtifactProblem(record["problem"])
        return scene_editor.edl_from_artifact(record["data"])

    # -------------------------------------------------------------- writes
    def create_edl(self, *, source_path: str | None = None,
                   source_duration: float, title: str = "",
                   actor: str = "console") -> dict:
        if source_path is None:
            source_path = str(self._source.get_source_path())
        edl = scene_editor.build_edl(
            source_path, source_duration, title=title,
            width=1080, height=1920,
        )
        artifact = scene_editor.edl_to_artifact(edl)
        relative = self._artifacts.write_artifact("edl", artifact, actor=actor)
        self._audit.record("edl.created", actor=actor, artifact=relative,
                           detail={"title": title, "duration": source_duration})
        return artifact

    def add_clip(self, *, source_path: str, source_start_sec: float = 0.0,
                 source_end_sec: float, actor: str = "console") -> dict:
        edl = self.load_edl()
        new_clip = scene_editor.EDLClip(
            clip_id=f"clip_{len(edl.clips)+1:03d}",
            source_path=source_path,
            source_start_sec=source_start_sec,
            source_end_sec=source_end_sec,
        )
        edl = scene_editor.add_clip_to_edl(edl, new_clip)
        artifact = scene_editor.edl_to_artifact(edl)
        relative = self._artifacts.write_artifact("edl", artifact, actor=actor)
        self._audit.record("edl.clip_added", actor=actor, artifact=relative,
                           detail={"clip_id": new_clip.clip_id})
        return artifact

    def update_clip(self, clip_index: int, *, start=None, end=None,
                    crop=None, zoom=None, caption_style=None,
                    b_roll=None, add_b_roll=None, actor="console") -> dict:
        edl = self.load_edl()
        if clip_index < 0 or clip_index >= len(edl.clips):
            raise PipelineServiceError(f"gecersiz klip indeksi: {clip_index}")
        clip = edl.clips[clip_index]
        if start is not None and end is not None:
            clip = scene_editor.set_clip_source_bounds(
                clip, start, end, self._source.get_duration())
        if crop is not None:
            clip = scene_editor.crop_clip(clip, **crop)
        if zoom is not None:
            clip = scene_editor.zoom_clip(clip, **zoom)
        if caption_style is not None:
            clip = scene_editor.set_caption_style(
                clip, scene_editor.CaptionStyle(**caption_style))
        if b_roll is not None:
            new_brolls = [scene_editor.BRoll(**b) for b in b_roll]
            clip = scene_editor.EDLClip(
                clip_id=clip.clip_id,
                source_path=clip.source_path,
                source_start_sec=clip.source_start_sec,
                source_end_sec=clip.source_end_sec,
                timeline_start_sec=clip.timeline_start_sec,
                crop=clip.crop,
                zoom=clip.zoom,
                caption_style=clip.caption_style,
                b_roll=new_brolls,
                notes=clip.notes,
            )
        if add_b_roll is not None:
            clip = scene_editor.add_b_roll(clip, scene_editor.BRoll(**add_b_roll))
        edl.clips[clip_index] = clip
        artifact = scene_editor.edl_to_artifact(edl)
        relative = self._artifacts.write_artifact("edl", artifact, actor=actor)
        self._audit.record("edl.updated", actor=actor, artifact=relative,
                           detail={"clip_index": clip_index})
        return artifact

    def set_music(self, music_path: str, actor: str = "console") -> dict:
        edl = self.load_edl()
        edl = scene_editor.set_music_track(edl, music_path)
        artifact = scene_editor.edl_to_artifact(edl)
        relative = self._artifacts.write_artifact("edl", artifact, actor=actor)
        self._audit.record("edl.music_set", actor=actor, artifact=relative,
                           detail={"music_path": music_path})
        return artifact

    # -------------------------------------------------------------- preview
    def build_preview_command(self, output_name: str = "preview.mp4") -> list[str]:
        """Compile the EDL into an ffmpeg argument list WITHOUT executing it.

        This is the edit side of the render boundary: deterministic command
        construction from the EDL. Spawning the process is `render_edl_preview`
        on the facade, pending a `RenderService`.
        """
        edl = self.load_edl()
        if not edl.is_valid():
            raise PipelineServiceError("EDL gecerli degil")
        render_dir = self._namespace.resolve(
            self._ownership, artifact_catalog.RESULTS_DIRNAME, "render",
            create_parent=True)
        output = render_dir / output_name
        return scene_editor.build_preview_command(edl, output)

    # -------------------------------------------------------------- revision
    def request_revision(self, *, notes: str,
                         requested_by: str = "console") -> dict:
        edl = self.load_edl()
        edl.revision_notes = str(notes or "")
        edl.revision_requested_by = str(requested_by)
        edl.version += 1
        artifact = scene_editor.edl_to_artifact(edl)
        relative = self._artifacts.write_artifact("edl", artifact,
                                                  actor=requested_by)
        self._audit.record("edl.revision_requested", actor=requested_by,
                           artifact=relative,
                           detail={"notes": edl.revision_notes,
                                   "version": edl.version})
        return artifact

    def resolve_revision(self, *, resolved_by: str = "console") -> dict:
        """Clear the pending revision. Internal-only: no API route exposes it."""
        edl = self.load_edl()
        edl.revision_notes = ""
        edl.revision_requested_by = ""
        edl.version += 1
        artifact = scene_editor.edl_to_artifact(edl)
        relative = self._artifacts.write_artifact("edl", artifact,
                                                  actor=resolved_by)
        self._audit.record("edl.revision_resolved", actor=resolved_by,
                           artifact=relative,
                           detail={"version": edl.version})
        return artifact
