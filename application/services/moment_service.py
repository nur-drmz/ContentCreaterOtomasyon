"""MomentService -- the phase-2 boundary for "which moment becomes this Short".

Responsibilities extracted from the console façade (Milestone 11):

* ``record_moment_selection`` -- the ONLY application writer of
  ``moment_selection``: the phase gate, rank-ownership conflict check and the
  canonical `local_pipeline.video_analysis.build_moment_selection` call that
  binds the pick to the hot_moments manifest hash and the selected preview
  bytes.
* Read queries -- ``selected_moment``, ``moment_rank_owner`` (scans every
  plan-declared slot), ``clip_bounds_relative``, ``clip_bounds``,
  ``clip_bounds_override`` and the editor-facing ``clip_bounds_view``.
* Clip-bounds mutations -- ``save_clip_bounds`` / ``reset_clip_bounds``,
  including their render/transcript-invalidating revision snapshot.

What deliberately STAYS outside: ``hot_moments`` production
(VideoAnalysisService), the canonical selection builders and bounds math
(`local_pipeline.video_analysis` / `local_pipeline.scene_editor`), approval
state (ApprovalService -- a selection never grants approval), and the
revision-snapshot machinery itself (`ports.RevisionArchiver` -- shared with
rewind/review flows, so it is an injected adapter, not a façade callback).
"""
from __future__ import annotations

from pathlib import Path

from application import artifact_catalog
from application.errors import ArtifactProblem, PipelineServiceError
from application.time_utils import now_iso
from local_pipeline import scene_editor, video_analysis

RESULTS_DIRNAME = artifact_catalog.RESULTS_DIRNAME


class MomentService:
    """Moment selection + clip-bounds ownership; slot/rank isolation rules."""

    def __init__(self, *, artifacts, source, workflow, archiver, audit):
        #: ports.ArtifactRepository -- moment_selection / hot_moments /
        #: processing_plan / project reads and the moment_selection write;
        #: `write_artifact_path` persists the non-catalog clip_bounds document.
        self._artifacts = artifacts
        #: ports.SourceQuery -- source path and duration for bounds
        #: validation and the editor view's editability check.
        self._source = source
        #: ports.WorkflowGateQuery -- `require_phase`/`require_slot` gates and
        #: the plan-derived `slot_indexes`/`slot_moment_rank` readers.
        self._workflow = workflow
        #: ports.RevisionArchiver -- a bounds change invalidates this slot's
        #: rendered output (and, in direct mode, its transcript) through the
        #: shared recoverable-snapshot mechanism.
        self._archiver = archiver
        #: ports.AuditSink -- emits `clip_bounds.saved`.
        self._audit = audit

    # --------------------------------------------------------- clip bounds
    def clip_bounds_relative(self, slot: int = 1) -> str:
        index = self._workflow.require_slot(slot)
        return f"{RESULTS_DIRNAME}/clip_bounds/output_{index:02d}.json"

    def selected_moment(self, slot: int = 1) -> dict:
        """The analysed moment this slot renders, or {} when none is chosen."""
        selection = self._artifacts.load_artifact("moment_selection", slot=slot)
        hot = self._artifacts.load_artifact("hot_moments")
        if selection["problem"] or hot["problem"]:
            return {}
        rank = (selection["data"] or {}).get("selected_moment_rank")
        for moment in ((hot["data"] or {}).get("hot_moments") or []):
            if moment.get("rank") == rank:
                return moment
        return {}

    def clip_bounds(self, slot: int = 1) -> dict:
        """This slot's edited clip range, falling back to the analysed moment.

        Per SLOT, not per project: `results/edl/edl.json` is a single
        project-wide artifact, so storing a slot's in/out there would let one
        Short's edit silently move another's -- the leak `PER_SLOT_ARTIFACTS`
        exists to stop.
        """
        moment = self.selected_moment(slot)
        default_start = moment.get("source_start_sec")
        default_end = moment.get("source_end_sec")
        loaded = self._artifacts.load_artifact_path(
            self.clip_bounds_relative(slot))
        if isinstance(loaded, dict) and loaded.get("source_end_sec") is not None:
            return {
                "source_start_sec": float(loaded["source_start_sec"]),
                "source_end_sec": float(loaded["source_end_sec"]),
                "edited": True,
                "moment_start_sec": default_start,
                "moment_end_sec": default_end,
                "updated_at": loaded.get("updated_at"),
                "updated_by": loaded.get("updated_by"),
            }
        return {
            "source_start_sec": default_start,
            "source_end_sec": default_end,
            "edited": False,
            "moment_start_sec": default_start,
            "moment_end_sec": default_end,
            "updated_at": None, "updated_by": None,
        }

    def clip_bounds_override(self, slot: int = 1):
        """What `_run_render` passes down -- `None` when nothing was edited."""
        bounds = self.clip_bounds(slot)
        if not bounds["edited"]:
            return None
        return {"source_start_sec": bounds["source_start_sec"],
                "source_end_sec": bounds["source_end_sec"]}

    def source_duration_sec(self):
        """How long the source video is, in seconds, or None."""
        return self._source.get_duration()

    def clip_bounds_view(self, slot: int = 1) -> dict:
        """Everything the single-clip editor needs, decided on the server.

        Includes `source_duration_sec` because a timeline that does not know how
        long the source is cannot draw a scrubber, and `editable` because the
        answer depends on which file the render will actually seek into -- an
        analysis clip is already cropped and absolute source seconds mean
        something different in it.
        """
        try:
            slot = self._workflow.require_slot(slot)
        # Rendered on EVERY page load, including a project with no plan yet.
        # A panel that cannot open must say so, never break the page.
        except (PipelineServiceError, ArtifactProblem):
            return {"available": False, "slot": int(slot or 1),
                    "reason": "bu slot planda tanimli degil"}
        moment = self.selected_moment(slot)
        if not moment:
            return {"available": False, "slot": slot,
                    "reason": "once bir moment secilmeli"}
        source = ((self._artifacts.load_artifact("hot_moments")["data"] or {})
                  .get("source") or {})
        raw_path = source.get("raw_video_path")
        # A project can reach this screen with its source file gone (moved,
        # archived, restored onto another machine). That is a reason to say
        # the range cannot be edited -- not a reason to break the whole page.
        try:
            project_source = self._source.get_source_path()
        except (PipelineServiceError, ArtifactProblem):
            project_source = None
        editable = bool(project_source and Path(project_source).is_file()) \
            or bool(raw_path and Path(raw_path).is_file())
        bounds = self.clip_bounds(slot)
        pipeline_mode = (self._artifacts.load_artifact("processing_plan")
                         ["data"] or {}).get("pipeline_mode")
        return {
            "available": True,
            "slot": slot,
            "bounds": bounds,
            "source_duration_sec": self.source_duration_sec(),
            "moment_rank": moment.get("rank"),
            # The editor plays the SOURCE, not a rendered clip, and it plays it
            # by byte range -- so dragging a handle costs no FFmpeg run. The
            # server names the path; the browser never builds one.
            "source_media_path": (
                (self._artifacts.load_artifact("project")["data"] or {})
                .get("source") or {}).get("stored_path"),
            "editable": editable,
            "not_editable_reason": ("" if editable else
                                    "Orijinal kaynak video diskte bulunamadi; "
                                    "klip araligi yalnizca orijinal kaynakla "
                                    "duzenlenebilir."),
            # Said before the drag, not after: in direct mode the captions come
            # from this range, so moving it means the transcript has to be
            # produced and read again.
            "transcript_will_be_invalidated":
                pipeline_mode == "original_audio_subtitles",
            "min_duration_sec": scene_editor.MIN_CLIP_DURATION_SEC,
            "not_uploaded": True,
        }

    def save_clip_bounds(self, *, start: float, end: float,
                         actor: str = "console", slot: int = 1) -> dict:
        """Move or resize this slot's clip inside the source.

        Direct mode captions come from THIS range, so changing it invalidates the
        approved transcript: the words a human read are no longer the words that
        will be spoken. Narrated mode keeps its script, voice and subtitles --
        the narration does not change because the picture underneath moved.
        """
        slot = self._workflow.require_slot(slot)
        moment = self.selected_moment(slot)
        if not moment:
            raise PipelineServiceError(
                "Once bir moment secilmeli: duzenlenecek bir klip yok.")
        # The measured source length. Reading it from `hot_moments.source` --
        # which has no duration field -- yielded None, and a None upper bound
        # means NO upper bound: an operator could drag the clip past the end of
        # the file and only find out when ffmpeg failed.
        source_duration = self.source_duration_sec()
        clip = scene_editor.EDLClip(
            clip_id=f"slot_{slot:02d}",
            source_path=str(self._source.get_source_path()),
            source_start_sec=float(moment.get("source_start_sec") or 0.0),
            source_end_sec=float(moment.get("source_end_sec") or 0.0),
        )
        try:
            edited = scene_editor.set_clip_source_bounds(
                clip, start, end,
                float(source_duration) if source_duration else None)
        except ValueError as exc:
            raise PipelineServiceError(str(exc)) from exc

        previous = self.clip_bounds(slot)
        unchanged = (
            abs(previous["source_start_sec"] - edited.source_start_sec) < 1e-6
            and abs(previous["source_end_sec"] - edited.source_end_sec) < 1e-6)
        if unchanged:
            return {"slot": slot, "bounds": previous, "unchanged": True,
                    "archived": None, "transcript_invalidated": False,
                    "not_uploaded": True}

        pipeline_mode = (self._artifacts.load_artifact("processing_plan")
                         ["data"] or {}).get("pipeline_mode")
        is_direct = pipeline_mode == "original_audio_subtitles"
        if is_direct:
            # The caption text was transcribed from the OLD range and a human
            # approved THAT text. Keeping it would burn in words that are no
            # longer spoken -- the exact failure the transcript gate exists for.
            #
            # Dropped from `preserve` rather than appended: for slots 2+ the
            # transcript lives INSIDE the slot directory, so a slot-1-shaped
            # path would name a file that is not there and archive nothing.
            preserve = tuple(
                name for name in self._archiver.RENDER_ARCHIVE_PRESERVED
                if name != "moment_transcript.json")
            paths = self._archiver.render_slot_archive_paths(
                slot, preserve=preserve)
            paths = paths + [
                self._artifacts.artifact_relative("moment_transcript", slot)]
        else:
            paths = self._archiver.render_slot_archive_paths(slot)
        archived = self._archiver.archive_revision(
            paths, action="clip_bounds_edited", actor=actor, target="render")

        self._artifacts.write_artifact_path(
            self.clip_bounds_relative(slot),
            {"artifact_type": "clip_bounds", "schema_version": 1,
             "output_slot": slot, "not_uploaded": True,
             "source_start_sec": edited.source_start_sec,
             "source_end_sec": edited.source_end_sec,
             "moment_start_sec": moment.get("source_start_sec"),
             "moment_end_sec": moment.get("source_end_sec"),
             "updated_at": now_iso(),
             "updated_by": str(actor or "console")[:120]})

        self._audit.record("clip_bounds.saved", actor=actor,
                           artifact=self.clip_bounds_relative(slot),
                           detail={"slot": slot,
                                   "source_start_sec": edited.source_start_sec,
                                   "source_end_sec": edited.source_end_sec,
                                   "transcript_invalidated": is_direct,
                                   "revision_id": (archived or {})
                                   .get("revision_id")})
        return {"slot": slot, "bounds": self.clip_bounds(slot),
                "unchanged": False, "archived": archived,
                "transcript_invalidated": is_direct, "not_uploaded": True}

    def reset_clip_bounds(self, *, actor: str = "console",
                          slot: int = 1) -> dict:
        """Snap back to the range the analysis chose."""
        slot = self._workflow.require_slot(slot)
        moment = self.selected_moment(slot)
        if not moment:
            raise PipelineServiceError("Once bir moment secilmeli.")
        return self.save_clip_bounds(
            start=float(moment["source_start_sec"]),
            end=float(moment["source_end_sec"]), actor=actor, slot=slot)

    # ------------------------------------------------------- moment selection
    def record_moment_selection(self, *, selected_rank: int, reviewed_by: str,
                                rejected_ranks=None, notes: str = "",
                                require_preview: bool = True,
                                slot: int = 1) -> dict:
        """The ONLY write path for `moment_selection`.

        The artifact binds the human pick to three things, all enforced by the
        canonical builder in `local_pipeline.video_analysis`: the hot_moments
        DOCUMENT hash (a re-analysis cannot silently pair an old pick with new
        moments), the chosen PREVIEW's hash (downstream stages render the exact
        bytes that were approved), and the slot's rank OWNERSHIP (one rank, one
        Short -- two Shorts on the same moment is how duplicate uploads happen).
        """
        self._workflow.require_phase("moment_selection")
        self._workflow.require_slot(slot)
        hot = self._artifacts.load_artifact("hot_moments", required=True)
        if hot["problem"]:
            raise ArtifactProblem(hot["problem"])
        taken = self.moment_rank_owner(selected_rank, slot)
        if taken is not None:
            raise PipelineServiceError(
                f"rank {selected_rank} zaten {taken}. cikti yuvasi icin secilmis; "
                "her Short farkli bir moment kullanmali")
        selection = video_analysis.build_moment_selection(
            hot["data"], selected_moment_rank=selected_rank,
            reviewed_by=reviewed_by,
            rejected_moment_ranks=rejected_ranks, review_notes=notes,
            require_preview=require_preview,
        )
        self._artifacts.write_artifact("moment_selection", selection,
                                       actor=reviewed_by, slot=slot)
        return selection

    def moment_rank_owner(self, rank: int, slot: int):
        """Index of another slot that already selected `rank`, or None.

        Producing the same moment twice in one batch is almost always a mistake
        rather than an intent, and it is invisible until both Shorts are rendered
        and watched. It is rejected at selection time, where the fix is one click.
        """
        for other in self._workflow.slot_indexes():
            if int(other) == int(slot):
                continue
            existing = self._artifacts.load_artifact(
                "moment_selection", slot=other)["data"]
            if existing and existing.get("selected_moment_rank") == int(rank):
                return int(other)
        return None
