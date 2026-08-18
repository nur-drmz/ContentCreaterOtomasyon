"""Render-specific half of the human review checklist.

The RULES (digest binding, staleness, revision records) live in
`media_review.py`, shared with voice review. Only the questions, the artifact
path and the fix tabs are render-specific, and only those are here.
sisted human review checklist for a rendered Short.

WHY THIS EXISTS
---------------
The hash gate proves the reviewer saw *some* bytes. It cannot prove they checked
the things that actually go wrong in this pipeline: a clip that starts mid-word,
a crop that decapitates the subject, captions drifting out of sync, the
active-word highlight landing on the wrong token, an unreadable font, or audio
that cuts. So final approval now additionally requires an explicit, itemised
review -- and that review is an ARTIFACT, not a browser checkbox.

    results/render_review.json

BOUND TO THE VIDEO, LIKE THE APPROVAL
-------------------------------------
Every checklist entry records the digest of the video it was ticked against.
Re-rendering produces a different digest, so the checklist RESETS instead of
silently carrying a verdict about a video nobody watched. This is the same rule
`approval.check_approval` applies, deliberately.

WHAT THIS MODULE MUST NOT DO
----------------------------
It cannot approve anything, it never touches the video, and "needs revision" is
a note plus a pointer to the tab that owns the fix -- it does not re-run a phase
or mutate any artifact.
"""
from __future__ import annotations

from . import media_review
from .media_review import (  # re-exported: callers use these unqualified
    VERDICT_NEEDS_REVISION,
    VERDICT_PASS,
    VERDICT_PENDING,
)

#: The six failure modes this pipeline actually produces. Each is a separate
#: decision because "looks fine" is not a review.
CHECKLIST_ITEMS = (
    {"key": "clip_boundaries", "label": "Klip sinirlari",
     "question": "Klip cumle ortasinda baslamiyor/bitmiyor; giris ve cikis dogal mi?",
     "fix_tab": "moments",
     "fix_hint": "Moment Explorer'da klip baslangicini/suresini degistirip yeniden analiz et."},
    {"key": "crop", "label": "Kadraj / crop",
     "question": "9:16 kadraj onemli yuz veya aksiyonu kesmiyor mu?",
     "fix_tab": "phase0",
     "fix_hint": "Faz 0 planinda render_mode'u degistir (blurred_background / center_crop) ve yeniden onayla."},
    {"key": "subtitle_sync", "label": "Altyazi senkronu",
     "question": "Altyazilar konusmayla kabul edilebilir olcude senkron mu?",
     "fix_tab": "render",
     "fix_hint": "Render Studio'da daha buyuk bir Whisper modeliyle yeniden render et."},
    {"key": "active_word_highlight", "label": "Aktif kelime vurgusu",
     "question": "Sari vurgu dogru kelimenin uzerinde mi?",
     "fix_tab": "render",
     "fix_hint": "Kelime hizalamasi fallback'e dusmus olabilir; render manifestindeki altyazi kaynagini kontrol et."},
    {"key": "font_readability", "label": "Font ve okunabilirlik",
     "question": "Yazi tipi, boyutu ve kontrasti mobilde okunabilir mi? Guvenli alan disina tasmiyor mu?",
     "fix_tab": "phase0",
     "fix_hint": "Faz 0 planinda caption temasini degistir ve yeniden onayla."},
    {"key": "audio_continuity", "label": "Ses surekliligi",
     "question": "Orijinal ses kesintisiz mi; seviye ve kirpilma kabul edilebilir mi?",
     "fix_tab": "render",
     "fix_hint": "Kaynak klip araligini Moment Explorer'dan degistirip yeniden render et."},
)

CHECKLIST_KEYS = tuple(item["key"] for item in CHECKLIST_ITEMS)
RENDER_REVIEW_ARTIFACT = "results/render_review.json"

SPEC = media_review.ReviewSpec(
    kind="render",
    artifact_path=RENDER_REVIEW_ARTIFACT,
    artifact_type="render_review",
    items=CHECKLIST_ITEMS,
    fix_tabs=("moments", "script", "voice", "render", "phase0"),
    missing_media_blocker="Onaylanacak bir render videosu yok.",
    revision_blocker=("Bu render icin acik bir 'revizyon gerekli' kaydi var: "
                      "{notes}. Duzeltip yeniden render et."),
    stale_blocker=("{count} kontrol maddesi BASKA bir videoya isaretlenmis. Video "
                   "yeniden uretildi; maddeleri bu video icin tekrar isaretle."),
)


def new_review(ownership, video_sha256: str | None = None) -> dict:
    return media_review.new_review(SPEC, ownership, video_sha256)


def evaluate(review: dict | None, video_sha256: str | None, ownership) -> dict:
    state = media_review.evaluate(SPEC, review, video_sha256, ownership)
    # Legacy aliases: the render UI and its tests read these names.
    state["video_sha256"] = state["media_sha256"]
    state["recorded_video_sha256"] = state["recorded_media_sha256"]
    state["binds_current_video"] = state["binds_current_media"]
    return state


def set_item(review: dict, key: str, *, checked: bool, video_sha256: str,
             reviewed_by: str, note: str = "") -> dict:
    return media_review.set_item(SPEC, review, key, checked=checked,
                                  media_sha256=video_sha256,
                                  reviewed_by=reviewed_by, note=note)


def request_revision(review: dict, *, video_sha256: str, requested_by: str,
                     notes: str, target_tab: str = "moments") -> dict:
    return media_review.request_revision(
        SPEC, review, media_sha256=video_sha256, requested_by=requested_by,
        notes=notes, target_tab=target_tab)


def clear_revision(review: dict, *, video_sha256: str, resolved_by: str) -> dict:
    return media_review.clear_revision(SPEC, review, media_sha256=video_sha256,
                                        resolved_by=resolved_by)


def technical_summary(manifest: dict | None) -> dict:
    """Flat, display-ready technical facts. Pure read; derives nothing new."""
    if not isinstance(manifest, dict):
        return {"available": False}
    output = manifest.get("output") or {}
    validation = output.get("validation") or {}
    details = validation.get("details") or {}
    config = manifest.get("config") or {}
    subtitles = manifest.get("subtitles") or {}
    resolution = details.get("resolution") or f"{config.get('render_width')}x{config.get('render_height')}"
    width = height = None
    if isinstance(resolution, str) and "x" in resolution:
        try:
            width, height = (int(part) for part in resolution.split("x", 1))
        except ValueError:
            width = height = None
    return {
        "available": True,
        "status": manifest.get("status"),
        "status_reason": manifest.get("status_reason"),
        "duration_sec": details.get("duration_sec"),
        "resolution": resolution,
        "vertical": bool(width and height and height > width),
        "probe_backend": details.get("probe_backend"),
        "video_codec": details.get("video_codec") or "h264",
        "audio_present": bool(validation.get("checks", {}).get("has_audio_stream")),
        "original_audio_preserved": config.get("original_audio_preserved"),
        "tts_used": config.get("tts_used"),
        "pipeline_mode": config.get("pipeline_mode"),
        "render_mode": config.get("render_mode"),
        "frame_zoom": config.get("frame_zoom"),
        "frame_focus_x": config.get("frame_focus_x"),
        "frame_focus_y": config.get("frame_focus_y"),
        "preview_mode": config.get("preview_mode"),
        "subtitle_source": subtitles.get("source"),
        "subtitle_cues": subtitles.get("cue_count"),
        "subtitle_words": subtitles.get("word_count"),
        "excluded_subtitle_cues": subtitles.get("excluded_cue_count"),
        "all_subtitles_suppressed": subtitles.get("all_subtitles_suppressed"),
        # A render that produced zero subtitle words did half of what this mode
        # promises. Carried to the panel so the reviewer is told, rather than
        # discovering it by watching a silent-captioned Short.
        "no_speech_detected": subtitles.get("no_speech_detected"),
        "subtitle_warning": subtitles.get("subtitle_warning"),
        "dynamic_word_highlight": subtitles.get("dynamic_word_highlight"),
        "active_word_color": subtitles.get("active_word_color"),
        "caption_theme": config.get("caption_theme_resolved"),
        "caption_font": config.get("caption_font_name"),
        "caption_font_size": config.get("caption_font_size"),
        "validation_passed": validation.get("passed"),
        "validation_checks": validation.get("checks") or {},
        "video_sha256_16": output.get("final_video_sha256_16"),
        "not_uploaded": manifest.get("not_uploaded"),
    }
