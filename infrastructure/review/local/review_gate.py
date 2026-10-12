"""Local implementation of `application.ports.ReviewGate`.

WHY THIS WRAPS THE CONSOLE REVIEW MODULES
-----------------------------------------
`creator_console.{copyright_review,render_review,voice_review}` already contain
the deterministic checklist logic: which items exist, what makes a list
complete, how a digest change resets it. Those modules are unchanged and remain
canonical.

What moved here is the ORCHESTRATION that used to sit on `PipelineService`:
loading the stored checklist, computing the content digest it must be bound to,
and assembling the state dict. It moved rather than being copied so there is
exactly one implementation -- `ApprovalService` consults this gate, and the
façade's public `copyright_review_state()` / `render_review_state()` /
`voice_review_state()` methods delegate to it. A second copy would eventually
disagree with the first about whether a checklist is complete, which is the one
thing a precondition must never be wrong about.

This is an infrastructure adapter because it imports `creator_console`; the same
documented transitional direction as `LocalArtifactRepository` and
`LocalProgressFactory`.
"""
from __future__ import annotations

from application.artifact_catalog import RESULTS_DIRNAME
from creator_console import copyright_review as copyright_review_module
from creator_console import render_review as render_review_module
from creator_console import voice_review as voice_review_module
from local_pipeline import approval


class LocalReviewGate:
    """The three human review checklists for one project namespace."""

    def __init__(self, artifacts, ownership, source):
        #: `ports.ArtifactRepository` -- checklists live beside the artifacts
        #: they review, and are read by relative path.
        self._artifacts = artifacts
        self._ownership = ownership
        #: `ports.SourceQuery` -- the copyright checklist binds to the source
        #: fingerprint, so a replaced source resets the answers.
        self._source = source

    # ----------------------------------------------------------- digests
    def render_video_digest(self, slot: int = 1):
        """SHA-256 of the render currently on disk, or None.

        The SAME digest the approval gate binds to, so a checklist entry and an
        approval can never disagree about which video was reviewed.
        """
        loaded = self._artifacts.load_artifact("render_manifest", slot=slot)
        if not loaded["data"] or loaded["problem"]:
            return None
        try:
            return approval.content_hash_for("render", loaded["data"])
        except ValueError:
            return None

    def voice_audio_digest(self):
        """SHA-256 of the voiceover currently on disk, or None.

        The SAME digest the approval gate binds to, so a checklist entry and an
        approval can never disagree about which audio was listened to.
        """
        loaded = self._artifacts.load_artifact("selected_voiceover")
        if not loaded["data"] or loaded["problem"]:
            return None
        try:
            return approval.content_hash_for("voiceover", loaded["data"])
        except ValueError:
            return None

    def source_fingerprint(self):
        """The digest the copyright answers are bound to.

        Deliberately the SOURCE fingerprint, not a render: the question is about
        the material that was taken, so swapping the source has to reset the
        answers even if every later artifact still looks fine.
        """
        return self._source.get_fingerprint()

    # ------------------------------------------------------------- paths
    def render_review_relative(self, slot: int = 1) -> str:
        """Where THIS slot's checklist lives.

        Each Short is watched separately, so each Short owns its own checklist.
        Sharing one file would let ticks earned on slot 1 unlock the approval
        gate of a slot 2 nobody has watched -- the exact failure the checklist
        exists to prevent.
        """
        index = int(slot or 1)
        if index == 1:
            return render_review_module.RENDER_REVIEW_ARTIFACT
        name = render_review_module.RENDER_REVIEW_ARTIFACT.rsplit("/", 1)[-1]
        return f"{RESULTS_DIRNAME}/render/output_{index:02d}/{name}"

    # ------------------------------------------------------------ states
    def render_review_state(self, slot: int = 1) -> dict:
        review = self._artifacts.load_artifact_path(
            self.render_review_relative(slot))
        technical = render_review_module.technical_summary(
            self._artifacts.load_artifact("render_manifest", slot=slot)["data"])
        state = render_review_module.evaluate(
            review, self.render_video_digest(slot), self._ownership,
            current_snapshot=technical)
        state["technical"] = technical
        state["slot"] = int(slot or 1)
        return state

    def voice_review_state(self) -> dict:
        review = self._artifacts.load_artifact_path(
            voice_review_module.VOICE_REVIEW_ARTIFACT)
        decision = ((self._artifacts.load_artifact("processing_plan")["data"] or {})
                    .get("decision") or {})
        voiceover = self._artifacts.load_artifact("selected_voiceover")["data"]
        script = self._artifacts.load_artifact("selected_script")["data"]
        technical = voice_review_module.technical_summary(voiceover, script)
        state = voice_review_module.evaluate(
            review, self.voice_audio_digest(), self._ownership,
            content_language=decision.get("content_language"),
            narration_preset=(decision.get("narration") or {}).get("preset"),
            current_snapshot=technical)
        state["technical"] = technical
        # The exact words being spoken, so the listener can follow along instead
        # of judging "does this sound right" from memory.
        state["script_text"] = (script or {}).get("script_text")
        state["script_status"] = (script or {}).get("status")
        state["audio_relative_path"] = (voiceover or {}).get("audio_path_relative")
        # A voiceover produced BEFORE a language change is not merely stale, it
        # is in the wrong language -- said plainly rather than left to the ear.
        spoken = (voiceover or {}).get("language_code")
        planned = decision.get("content_language")
        state["language_mismatch"] = bool(spoken and planned and spoken != planned)
        state["spoken_language"] = spoken
        state["planned_language"] = planned
        state["planned_voice"] = (decision.get("narration") or {}).get("voice")
        state["spoken_voice"] = (voiceover or {}).get("voice_id")
        # A preset switch makes existing audio the wrong DELIVERY, not merely
        # stale -- named here so the panel can say which.
        state["preset_mismatch"] = bool(
            state["planned_voice"] and state["spoken_voice"]
            and state["planned_voice"] != state["spoken_voice"])
        return state

    def copyright_review_state(self) -> dict:
        review = self._artifacts.load_artifact_path(
            copyright_review_module.COPYRIGHT_REVIEW_ARTIFACT)
        state = copyright_review_module.evaluate(
            review, self.source_fingerprint(), self._ownership)
        project = self._artifacts.load_artifact("project")["data"] or {}
        source = project.get("source") or {}
        # Shown next to the questions so the operator answers about THIS file
        # rather than whichever source they last had open.
        state["source"] = {
            "display_name": source.get("display_name"),
            "registered_at": source.get("registered_at"),
            "license_type": source.get("license_type"),
        }
        return state

    # ------------------------------------- checklist records for mutation
    # The mutation methods still live on the façade; they reuse these so the
    # "new checklist bound to the current digest" rule has one definition.
    def load_or_new_render_review(self, slot: int = 1) -> dict:
        existing = self._artifacts.load_artifact_path(
            self.render_review_relative(slot))
        if isinstance(existing, dict):
            return existing
        return render_review_module.new_review(
            self._ownership, self.render_video_digest(slot))

    def load_or_new_voice_review(self) -> dict:
        existing = self._artifacts.load_artifact_path(
            voice_review_module.VOICE_REVIEW_ARTIFACT)
        if isinstance(existing, dict):
            return existing
        return voice_review_module.new_review(self._ownership,
                                              self.voice_audio_digest())

    def load_or_new_copyright_review(self) -> dict:
        existing = self._artifacts.load_artifact_path(
            copyright_review_module.COPYRIGHT_REVIEW_ARTIFACT)
        if isinstance(existing, dict):
            return existing
        return copyright_review_module.new_review(self._ownership,
                                                  self.source_fingerprint())
