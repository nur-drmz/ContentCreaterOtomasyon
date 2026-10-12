"""TranscriptService -- the phase-3 boundary for the human-reviewed caption.

Responsibilities extracted from the console façade (Milestone 12):

* ``run_transcript`` -- the ONLY generation path for ``moment_transcript``:
  the phase/slot gates, the hot_moments + moment_selection trust check, the
  canonical `local_pipeline.render_utils.run_moment_transcription` engine
  call (selected-interval ASR, no provider), carry-over of word-level
  corrections, the recoverable archive of a hand-edited transcript that a
  re-run replaces, and the progress lifecycle (start/note/finish/fail,
  cancellation through the injected `ports.ProgressFactory`).
* Read queries -- ``transcript_view`` (the caption-screen projection:
  segments, low-confidence words, glossary suggestions, approval state,
  phase eligibility) and ``transcript_segments`` (one editable row per
  caption line). Reads upgrade old artifacts in memory via
  `transcript_edit.ensure_v2` and never rewrite the file.
* Edit lifecycle -- ``save_transcript_edits`` (segment rows or whole text,
  same timings, new revision), ``set_transcript_exclusions`` (kept in the
  review, dropped from captions), ``set_transcript_corrections`` (word-level
  replacements re-derived from the raw ASR snapshot) and
  ``restore_transcript_from_asr`` (put the recogniser's own output back).
  Every successful edit writes ``moment_transcript`` through the artifact
  repository, appends a revision record, and leaves approval/digest
  invalidation to the chain reader -- an edit never grants approval and
  never triggers script, TTS, render or publishing.
* Glossary teaching -- a word-for-word manual fix feeds
  `local_pipeline.glossary.suggest_terms_from_correction`, so the NEXT
  transcription gets the name right at recognition time.

What deliberately STAYS outside: ``moment_selection`` ownership
(MomentService -- this service only READS it), ``hot_moments`` production
(VideoAnalysisService), the canonical ASR engine and transcript-edit math
(`local_pipeline.render_utils` / `local_pipeline.transcript_edit` /
`local_pipeline.glossary`), workflow eligibility (`ports.WorkflowGateQuery`),
approval state (ApprovalService behind `ports.ApprovalQuery` -- read-only
here), revision-snapshot mechanics (`ports.RevisionArchiver`) and progress
persistence (`ports.ProgressFactory`).
"""
from __future__ import annotations

from application import artifact_catalog
from application.errors import ArtifactProblem, PipelineServiceError
from local_pipeline import glossary as glossary_module
from local_pipeline import render_utils, transcript_edit

RUNTIME_DIRNAME = artifact_catalog.RUNTIME_DIRNAME


def _contiguous_runs(indices) -> list:
    """Group sorted indices into runs of consecutive values.

    `[0, 1, 4]` -> `[[0, 1], [4]]`. Used so two adjacent corrected words are
    read as one multi-word name instead of two unrelated ones.
    """
    runs: list = []
    for index in indices:
        if runs and index == runs[-1][-1] + 1:
            runs[-1].append(index)
        else:
            runs.append([index])
    return runs


class TranscriptService:
    """Transcript lifecycle ownership; per-slot isolation is a gate rule."""

    def __init__(self, *, artifacts, source, workflow, approvals, archiver,
                 progress, namespace, ownership, audit):
        #: ports.ArtifactRepository -- moment_transcript reads/writes,
        #: hot_moments + moment_selection trust reads, the shared project
        #: glossary read/write.
        self._artifacts = artifacts
        #: ports.SourceQuery -- the selected moment's audio comes from the
        #: project's ONE source video; the service needs the path, never the
        #: source lifecycle.
        self._source = source
        #: ports.WorkflowGateQuery -- `require_phase`/`plan_gate` eligibility
        #: and `require_slot` scoping; transcript code never bypasses the gate.
        self._workflow = workflow
        #: ports.ApprovalQuery -- `transcript_view` reports the live approval
        #: state for the caption screen; the port is read-only, an edit drops
        #: approval by changing the bound digest, never by writing it.
        self._approvals = approvals
        #: ports.RevisionArchiver -- a re-run archives a hand-edited
        #: transcript through the shared recoverable-snapshot mechanism.
        self._archiver = archiver
        #: ports.ProgressFactory -- the `transcript` activity tracker, with
        #: cancellation bound by the job runtime.
        self._progress = progress
        #: ports.ProjectNamespaceStore + the ownership scope, for the
        #: ASR engine's scratch directory (`runtime/`) and the empty-glossary
        #: skeleton's ownership stamp.
        self._namespace = namespace
        self._ownership = ownership
        #: ports.AuditSink -- the extra detail-bearing `artifact.written`
        #: events the edit paths emit on top of the repository's own write
        #: event, and `transcript.manual_edits_archived`.
        self._audit = audit

    # ------------------------------------------------------------- glossary
    def _glossary(self) -> dict:
        """Project glossary, or an empty one.

        Deliberately NOT per-slot: a proper name is a fact about the source,
        not about one Short cut from it, and every Short of a project should
        benefit from a name its sibling's reviewer already fixed.
        """
        record = self._artifacts.load_artifact("glossary")["data"]
        return record if isinstance(record, dict) else glossary_module.new_glossary(
            self._ownership)

    # --------------------------------------------------------- transcript
    def _transcript_artifact(self, slot: int) -> tuple:
        """The slot's transcript upgraded to the revision-aware shape.

        Upgrading on READ rather than migrating on disk is what keeps projects
        that predate revisions working: nothing is rewritten until the user
        actually edits, and an untouched old project stays byte-identical.
        """
        loaded = self._artifacts.load_artifact(
            "moment_transcript", required=True, slot=slot)
        if loaded["problem"]:
            raise ArtifactProblem(loaded["problem"])
        return transcript_edit.ensure_v2(loaded["data"] or {}), loaded

    def transcript_segments(self, slot: int = 1) -> list:
        artifact, _ = self._transcript_artifact(slot)
        return transcript_edit.build_segments(
            artifact,
            low_confidence_threshold=render_utils.LOW_CONFIDENCE_WORD_THRESHOLD)

    def save_transcript_edits(self, *, segments=None, full_text=None,
                              edited_by: str, slot: int = 1) -> dict:
        """Persist a human edit as a NEW REVISION over the same timings.

        Exactly one of `segments` or `full_text` is accepted. Both paths end in
        the same per-segment application, so the whole-transcript textarea
        cannot take a shortcut that the row editor does not.
        """
        slot = self._workflow.require_slot(slot)
        if (segments is None) == (full_text is None):
            raise PipelineServiceError(
                "ya segment listesi ya da tam metin verilmeli (ikisi birden degil)")
        artifact, _loaded = self._transcript_artifact(slot)
        previous_sha = transcript_edit.content_sha256(artifact)

        if full_text is not None:
            pairs = transcript_edit.parse_full_text(
                artifact, full_text,
                low_confidence_threshold=render_utils.LOW_CONFIDENCE_WORD_THRESHOLD)
        else:
            pairs = []
            for item in (segments or []):
                identifier = str((item or {}).get("id") or "").strip()
                if not identifier:
                    raise PipelineServiceError("segment id'si eksik")
                pairs.append((identifier, (item or {}).get("text")))

        problems = transcript_edit.validate_segment_texts(pairs)
        if problems:
            raise PipelineServiceError("; ".join(problems[:6]))

        applied = transcript_edit.apply_segment_texts(artifact, pairs)
        if not applied["changed_segment_ids"]:
            # Nothing changed: writing a revision would claim an edit that did
            # not happen and would drop a still-valid approval.
            return self.transcript_view(slot)

        rebuilt = render_utils.build_moment_transcript_artifact(
            applied["words"], applied["cues"],
            source=artifact.get("source"), language=artifact.get("language"),
            moment_rank=artifact.get("moment_rank"),
            moment_selection_sha256=artifact.get("moment_selection_sha256"),
            whisper_model_size=artifact.get("whisper_model_size"),
            initial_prompt_used=artifact.get("initial_prompt_used"),
            corrections=artifact.get("corrections"),
            asr=artifact.get("asr"),
            revisions=artifact.get("revisions"),
            edited_segment_ids=artifact.get("edited_segment_ids"),
            glossary_context=artifact.get("glossary_context"))
        rebuilt = transcript_edit.append_revision(
            rebuilt, actor=edited_by,
            kind=("full_text_edit" if full_text is not None else "segment_edit"),
            previous_sha256=previous_sha,
            changed_segment_ids=applied["changed_segment_ids"])

        relative = self._artifacts.write_artifact(
            "moment_transcript", rebuilt, actor=edited_by, slot=slot)
        # Identifiers and counts only -- never the transcript text. A caption is
        # the user's content and the audit log is not where it should be copied.
        self._audit.record(
            "artifact.written", actor=edited_by, artifact=relative,
            detail={"slot": slot, "revision": rebuilt.get("revision"),
                    "changed_segments": applied["changed_segment_ids"],
                    "changed_count": len(applied["changed_segment_ids"]),
                    "edit_kind": ("full_text" if full_text is not None else "segment")})

        # A manual fix is also vocabulary: offer to remember the new spelling so
        # the NEXT transcription gets it right at recognition time.
        self._record_glossary_suggestions_from_edit(
            artifact, rebuilt, applied["changed_segment_ids"], actor=edited_by)
        return self.transcript_view(slot)

    def set_transcript_exclusions(self, *, segment_ids, excluded: bool,
                                  actor: str, slot: int = 1) -> dict:
        """Keep selected vocals reviewable while excluding them from captions."""
        slot = self._workflow.require_slot(slot)
        artifact, _loaded = self._transcript_artifact(slot)
        previous_sha = transcript_edit.content_sha256(artifact)
        try:
            applied = transcript_edit.apply_segment_exclusions(
                artifact, segment_ids, excluded=excluded)
        except transcript_edit.TranscriptEditError as exc:
            raise PipelineServiceError(str(exc)) from exc
        if not applied["changed_segment_ids"]:
            return self.transcript_view(slot)
        rebuilt = render_utils.build_moment_transcript_artifact(
            applied["words"], applied["cues"],
            source=artifact.get("source"), language=artifact.get("language"),
            moment_rank=artifact.get("moment_rank"),
            moment_selection_sha256=artifact.get("moment_selection_sha256"),
            whisper_model_size=artifact.get("whisper_model_size"),
            initial_prompt_used=artifact.get("initial_prompt_used"),
            corrections=artifact.get("corrections"), asr=artifact.get("asr"),
            revisions=artifact.get("revisions"),
            edited_segment_ids=artifact.get("edited_segment_ids"),
            glossary_context=artifact.get("glossary_context"))
        rebuilt = transcript_edit.append_revision(
            rebuilt, actor=actor,
            kind=("subtitle_exclusion" if excluded else "subtitle_inclusion"),
            previous_sha256=previous_sha,
            changed_segment_ids=applied["changed_segment_ids"])
        relative = self._artifacts.write_artifact("moment_transcript", rebuilt,
                                                  actor=actor, slot=slot)
        self._audit.record(
            "artifact.written", actor=actor, artifact=relative,
            detail={"slot": slot, "caption_excluded": bool(excluded),
                    "changed_segments": applied["changed_segment_ids"]})
        return self.transcript_view(slot)

    def _record_glossary_suggestions_from_edit(self, before, after, changed_ids,
                                               *, actor: str) -> None:
        threshold = render_utils.LOW_CONFIDENCE_WORD_THRESHOLD
        old_map = {segment["id"]: segment["text"] for segment in
                   transcript_edit.build_segments(before, low_confidence_threshold=threshold)}
        new_map = {segment["id"]: segment["text"] for segment in
                   transcript_edit.build_segments(after, low_confidence_threshold=threshold)}
        record = self._glossary()
        proposals = []
        for identifier in changed_ids:
            old_tokens = str(old_map.get(identifier, "")).split()
            new_tokens = str(new_map.get(identifier, "")).split()
            # Word-for-word replacements are the ones that name something; a
            # wholesale rewrite is a sentence fix, not a proper noun.
            if old_tokens and len(old_tokens) == len(new_tokens):
                # ADJACENT changed words are ONE name, not several. Suggesting
                # them separately turned "Steelers Rider!" -> "Sheepstealer's
                # rider" into the term "Sheepstealer's", losing the half that
                # makes it a name -- and "rider" alone is dropped as an ordinary
                # lowercase word, so the phrase the human actually taught was
                # never offered back.
                changed = [index for index, (old_token, new_token)
                           in enumerate(zip(old_tokens, new_tokens))
                           if old_token != new_token]
                for run in _contiguous_runs(changed):
                    start, stop = run[0], run[-1] + 1
                    proposals.extend(glossary_module.suggest_terms_from_correction(
                        " ".join(old_tokens[start:stop]),
                        " ".join(new_tokens[start:stop]), record))
            elif new_tokens:
                proposals.extend(glossary_module.suggest_terms_from_correction(
                    " ".join(old_tokens), " ".join(new_tokens), record))
        if not proposals:
            return
        record = glossary_module.record_suggestions(record, proposals, actor=actor)
        self._artifacts.write_artifact("glossary", record, actor=actor)

    def restore_transcript_from_asr(self, *, actor: str, slot: int = 1) -> dict:
        """Put the recogniser's own output back as the current content.

        Possible only because `asr` was never overwritten. Recorded as a
        revision like any other edit, so the history shows the revert instead
        of looking like the manual work never happened.
        """
        slot = self._workflow.require_slot(slot)
        artifact, _ = self._transcript_artifact(slot)
        raw = artifact.get("asr") or {}
        if not (raw.get("words") or raw.get("cues")):
            raise PipelineServiceError("bu transkript icin saklanmis ham ASR ciktisi yok")
        previous_sha = transcript_edit.content_sha256(artifact)
        rebuilt = render_utils.build_moment_transcript_artifact(
            [dict(word) for word in (raw.get("words") or [])],
            [dict(cue) for cue in (raw.get("cues") or [])],
            source=artifact.get("source"), language=artifact.get("language"),
            moment_rank=artifact.get("moment_rank"),
            moment_selection_sha256=artifact.get("moment_selection_sha256"),
            whisper_model_size=artifact.get("whisper_model_size"),
            initial_prompt_used=artifact.get("initial_prompt_used"),
            corrections=[], asr=raw, revisions=artifact.get("revisions"),
            edited_segment_ids=[], glossary_context=artifact.get("glossary_context"))
        rebuilt = transcript_edit.append_revision(
            rebuilt, actor=actor, kind="restore_asr",
            previous_sha256=previous_sha, changed_segment_ids=[])
        relative = self._artifacts.write_artifact("moment_transcript", rebuilt,
                                                  actor=actor, slot=slot)
        self._audit.record("artifact.written", actor=actor, artifact=relative,
                           detail={"slot": slot, "revision": rebuilt.get("revision"),
                                   "edit_kind": "restore_asr"})
        return self.transcript_view(slot)

    def transcript_view(self, slot: int = 1) -> dict:
        """A curated slice for the caption screen.

        The raw artifact carries every word with its timings; dumping that into
        the page would bury the two things a reviewer needs -- the text, and
        which words the recogniser was unsure about.
        """
        slot = int(slot or 1)
        loaded = self._artifacts.load_artifact("moment_transcript", slot=slot)
        data = loaded["data"] or {}
        allowed, gate_reason = self._workflow.plan_gate("transcript")
        state = self._approvals.state("transcript", slot) if data else {}
        selection = self._artifacts.load_artifact(
            "moment_selection", slot=slot)["data"] or {}
        upgraded = transcript_edit.ensure_v2(data) if data else {}
        segments = (transcript_edit.build_segments(
            upgraded, low_confidence_threshold=render_utils.LOW_CONFIDENCE_WORD_THRESHOLD)
            if data else [])
        suggestions = (glossary_module.suggest_for_text(
            upgraded.get("text") or "", self._glossary(),
            low_confidence_terms=[w.get("text") for w in
                                  (upgraded.get("low_confidence_words") or [])])
            if data else [])
        return {
            "slot": slot,
            "available": bool(data),
            **transcript_edit.revision_summary(upgraded),
            "problem": loaded["problem"],
            "phase_allowed": bool(allowed),
            "phase_reason": gate_reason,
            "moment_rank": data.get("moment_rank") or selection.get("selected_moment_rank"),
            "status": data.get("status"),
            "status_reason": data.get("status_reason"),
            "language": data.get("language"),
            "whisper_model_size": data.get("whisper_model_size"),
            "source": data.get("source"),
            "text": data.get("text"),
            "word_count": data.get("word_count"),
            "cue_count": data.get("cue_count"),
            "excluded_cue_count": sum(
                bool(cue.get("excluded_from_subtitles"))
                for cue in (data.get("cues") or [])),
            "has_speech": data.get("has_speech"),
            "average_word_probability": data.get("average_word_probability"),
            "low_confidence_words": data.get("low_confidence_words") or [],
            "low_confidence_count": data.get("low_confidence_count"),
            "corrections": data.get("corrections") or [],
            # The whole transcript, in order, one editable row per caption line.
            # A summary was all this screen used to show, so a misheard proper
            # noun could not be seen -- let alone fixed -- before it was burned
            # into the video.
            "segments": segments,
            "full_text": transcript_edit.segments_to_text(segments),
            "limits": {"max_segment_chars": transcript_edit.MAX_SEGMENT_CHARS,
                       "max_total_chars": transcript_edit.MAX_TOTAL_CHARS},
            "glossary": glossary_module.summarise(self._glossary()),
            "glossary_context": data.get("glossary_context") or {},
            "suggestions": suggestions,
            # Words carry the highlight timings, so the page can show the caption
            # the way it will actually appear, with unsure words marked.
            "words": [
                {"text": w.get("text"), "start_sec": w.get("start"),
                 "probability": w.get("probability"),
                 "low_confidence": float(w.get("probability") or 0.0)
                                   < render_utils.LOW_CONFIDENCE_WORD_THRESHOLD,
                 "corrected": bool(w.get("corrected"))}
                for w in (data.get("words") or [])[:2000]
            ],
            "approval": state,
        }

    def run_transcript(self, *, subtitle_whisper_model_size="small",
                       subtitle_language="auto", caption_theme="auto",
                       job_id=None, actor="console", slot=1) -> dict:
        """Transcribe the selected moment's ORIGINAL audio for human review.

        This is `direct_clip_series`'s caption step. It calls no provider: the
        text is what the source already says, recognised locally. Existing
        corrections are carried over, because re-running the recognition should
        not silently discard the names a human already fixed.
        """
        self._workflow.require_phase("transcript")
        slot = self._workflow.require_slot(slot)
        tracker = self._progress.create("transcript", job_id=job_id)
        tracker.start("Transkript uretimi basladi")
        try:
            hot = self._artifacts.load_artifact("hot_moments", required=True)
            selection = self._artifacts.load_artifact(
                "moment_selection", required=True, slot=slot)
            for loaded in (hot, selection):
                if loaded["problem"]:
                    raise ArtifactProblem(loaded["problem"])
            previous = self._artifacts.load_artifact(
                "moment_transcript", slot=slot)["data"] or {}
            runtime_dir = self._namespace.resolve(
                self._ownership, RUNTIME_DIRNAME, create_parent=True)
            artifact = render_utils.run_moment_transcription(
                hot["data"], selection["data"], runtime_dir,
                subtitle_whisper_model_size=subtitle_whisper_model_size,
                subtitle_language=subtitle_language, caption_theme=caption_theme,
                source_video_path_override=str(self._source.get_source_path()),
                glossary=self._glossary(),
                progress=tracker)
            carried = [
                {"from": item.get("from"), "to": item.get("to")}
                for item in (previous.get("corrections") or [])
                if item.get("from") and item.get("to")
            ]
            if carried:
                artifact = self._apply_transcript_corrections(artifact, carried)
                tracker.note(f"{len(carried)} onceki duzeltme yeniden uygulandi")
            # Word-level corrections survive a re-run, but SEGMENT-level manual
            # work does not: the new recognition has its own words, timings and
            # segment boundaries, so a hand-rewritten line cannot be replayed
            # onto it honestly. Dropping it silently is the one thing that must
            # not happen, so the previous artifact is archived into
            # revisions/<id>/ first and the new one records what was set aside
            # and where to find it.
            artifact = self._archive_replaced_transcript(
                previous, artifact, slot=slot, actor=actor, tracker=tracker)
            self._artifacts.write_artifact("moment_transcript", artifact,
                                           job_id=job_id, actor=actor, slot=slot)
            # Audited only once the replacement is safely on disk. Archiving
            # MOVES the old file, so anything that raises between the move and
            # the write would leave the slot with no transcript at all -- which
            # is exactly the silent loss this whole branch exists to prevent.
            replaced = artifact.get("replaced_manual_edit")
            if replaced:
                self._audit.record(
                    "transcript.manual_edits_archived", actor=actor, job_id=job_id,
                    detail={"slot": slot, "revision_id": replaced["revision_id"],
                            "edited_segment_ids": replaced["edited_segment_ids"]})
            tracker.finish(message=(f"{artifact['word_count']} kelime, inceleme bekliyor"
                                    if artifact["has_speech"] else "konusma yok"))
            return artifact
        except Exception as exc:  # noqa: BLE001
            tracker.fail(f"{type(exc).__name__}: {exc}")
            raise

    def _archive_replaced_transcript(self, previous, artifact, *, slot, actor,
                                     tracker=None) -> dict:
        """Preserve a hand-edited transcript that a re-run is about to replace.

        Returns the new artifact, annotated with `replaced_manual_edit` when
        something was archived. A previous pass that only ever held recogniser
        output is replaced without ceremony -- there is no human work to lose.
        """
        edited_ids = list((previous or {}).get("edited_segment_ids") or [])
        history = list((previous or {}).get("revisions") or [])
        if not edited_ids and not history:
            return artifact
        revision = self._archiver.archive_revision(
            [self._artifacts.artifact_relative("moment_transcript", slot)],
            action="retranscribe", actor=actor, target=f"transcript_slot_{slot}")
        record = dict(artifact)
        record["replaced_manual_edit"] = {
            "revision_id": revision["revision_id"],
            "archived_path": f"revisions/{revision['revision_id']}/",
            "edited_segment_ids": edited_ids,
            "revision_count": len(history),
            "previous_content_sha256": transcript_edit.content_sha256(
                transcript_edit.ensure_v2(previous)),
            "note": ("Yeniden transkripsiyon elle duzenlenmis segmentleri "
                      "TASIMAZ; onceki surum arsivlendi ve geri alinabilir."),
        }
        if tracker is not None:
            tracker.note(
                f"{len(edited_ids)} elle duzenlenmis segment tasinmadi; onceki surum "
                f"revisions/{revision['revision_id']}/ altina arsivlendi")
        return record

    def _apply_transcript_corrections(self, artifact: dict, corrections) -> dict:
        """Re-derive the corrected text from the ORIGINAL recognition.

        Corrections are always applied to the untouched recognition rather than
        on top of a previous pass, so removing a correction really removes it
        instead of leaving its effect baked in.
        """
        base_words = [
            {k: v for k, v in word.items() if k != "corrected"}
            for word in (artifact.get("words") or [])
        ]
        # The v2 fields have to be carried through. Rebuilding without `asr`
        # makes the builder seed a FRESH snapshot from the already-corrected
        # words, so "restore the raw recognition" would hand back the
        # corrections instead of what was actually heard -- and the revision
        # history, the edited-segment set and the glossary context would vanish
        # with it. Every other caller passes these; this one did not.
        rebuilt = render_utils.build_moment_transcript_artifact(
            base_words, artifact.get("cues") or [],
            source=artifact.get("source"), language=artifact.get("language"),
            moment_rank=artifact.get("moment_rank"),
            moment_selection_sha256=artifact.get("moment_selection_sha256"),
            whisper_model_size=artifact.get("whisper_model_size"),
            initial_prompt_used=artifact.get("initial_prompt_used"),
            corrections=corrections, asr=artifact.get("asr"),
            revisions=artifact.get("revisions"),
            edited_segment_ids=artifact.get("edited_segment_ids"),
            glossary_context=artifact.get("glossary_context"))
        return rebuilt

    def set_transcript_corrections(self, *, corrections, edited_by: str,
                                 slot: int = 1) -> dict:
        """Replace this slot's correction list and re-derive the caption text.

        Any existing approval is dropped by construction: the approval hash
        covers the words, so changed words cannot stay approved.
        """
        slot = self._workflow.require_slot(slot)
        loaded = self._artifacts.load_artifact(
            "moment_transcript", required=True, slot=slot)
        if loaded["problem"]:
            raise ArtifactProblem(loaded["problem"])
        cleaned = []
        for item in (corrections or []):
            source = str((item or {}).get("from") or "").strip()
            target = str((item or {}).get("to") or "").strip()
            if not source or not target:
                continue
            if len(source) > 80 or len(target) > 80:
                raise PipelineServiceError("duzeltme kelimeleri 80 karakteri gecemez")
            cleaned.append({"from": source, "to": target})
        if len(cleaned) > 100:
            raise PipelineServiceError("en fazla 100 duzeltme tanimlanabilir")
        previous = transcript_edit.ensure_v2(loaded["data"])
        artifact = self._apply_transcript_corrections(previous, cleaned)
        # Recorded like any other edit, so the legacy word-level path shows up in
        # the same history as the segment editor instead of changing the text
        # invisibly.
        artifact = transcript_edit.append_revision(
            artifact, actor=edited_by, kind="corrections",
            previous_sha256=transcript_edit.content_sha256(previous))
        relative = self._artifacts.write_artifact(
            "moment_transcript", artifact, actor=edited_by, slot=slot)
        self._audit.record("artifact.written", actor=edited_by, artifact=relative,
                           detail={"corrections": len(cleaned), "slot": slot})
        return artifact
