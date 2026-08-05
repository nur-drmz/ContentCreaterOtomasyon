"""Voice-specific half of the human review checklist.

The RULES (digest binding, staleness, revision records) live in
`media_review.py`, shared with render review. Only the questions, the artifact
path and the fix tabs are voice-specific, and only those are here.

WHY A VOICE CHECKLIST AT ALL
----------------------------
`validate_audio_file` proves the WAV is a well-formed 24 kHz mono PCM file of
plausible length. It cannot hear it. Every failure mode that actually matters
for a Turkish narration -- a mispronounced name, a robotic cadence, a tone that
fights the footage, audio that says something other than the approved script --
is inaudible to a container check. Both TTS models are PREVIEW, which makes this
more likely, not less.

So voice approval now additionally requires an explicit, itemised listen, bound
to the digest of the audio that was listened to. Regenerating the voiceover
produces a different digest and resets the list, exactly as a re-render does.
"""
from __future__ import annotations

from . import media_review
from .media_review import (  # re-exported: callers use these unqualified
    VERDICT_NEEDS_REVISION,
    VERDICT_PASS,
    VERDICT_PENDING,
)

#: Six things a human ear decides and no validator can. Each is separate because
#: "sounds fine" is not a review.
CHECKLIST_ITEMS = (
    {"key": "pronunciation", "label": "Telaffuz",
     "question": "Ozel isimler ve terimler dogru okunuyor mu? (Targaryen, Rhaenyra, "
                 "yer adlari, sayilar)",
     "fix_tab": "script",
     "fix_hint": "Script Studio'da yazimi degistir (or. 'Rhaenyra' yerine okunusa yakin "
                 "bir yazim), scripti yeniden onayla ve sesi yeniden uret."},
    {"key": "naturalness", "label": "Dil dogalligi",
     "question": "Vurgu ve tonlama bu dile dogal geliyor mu; makine gibi ya da yabanci "
                 "aksanli duyulmuyor mu?",
     "fix_tab": "voice",
     "fix_hint": "Baska bir ses dene (Kore/Puck/Zephyr...) ve seslendirmeyi yeniden uret."},
    {"key": "pacing", "label": "Tempo",
     "question": "Hiz Short icin uygun mu; cumleler arasi duraklamalar cok kisa ya da "
                 "cok uzun degil mi?",
     "fix_tab": "voice",
     "fix_hint": "Anlatim yonergesine tempo talimati ekle (or. 'biraz daha yavas oku') "
                 "ve yeniden uret."},
    {"key": "tone", "label": "Ton ve duygu",
     "question": "Ton icerikle uyumlu mu; gerilim/merak isteniyorsa gercekten oyle mi "
                 "duyuluyor?",
     "fix_tab": "voice",
     "fix_hint": "Anlatim yonergesini degistir (or. 'gergin, merak uyandiran bir tonda') "
                 "ve yeniden uret."},
    {"key": "script_match", "label": "Script ile birebir mi",
     "question": "Seslendirme ONAYLI scriptin aynisini soyluyor mu; eksik/eklenmis cumle "
                 "yok mu?",
     "fix_tab": "script",
     "fix_hint": "Asagidaki script metnini dinlerken takip et. Uyusmuyorsa sesi yeniden "
                 "uret; sorun surerse scripti sadelestir."},
    {"key": "audio_quality", "label": "Ses kalitesi",
     "question": "Kirpilma, cizirti, ani seviye degisimi veya bosluk var mi?",
     "fix_tab": "voice",
     "fix_hint": "Seslendirmeyi yeniden uret; tekrar ediyorsa daha kisa bir scriptle dene."},
)

CHECKLIST_KEYS = tuple(item["key"] for item in CHECKLIST_ITEMS)
VOICE_REVIEW_ARTIFACT = "results/voice_review.json"

SPEC = media_review.ReviewSpec(
    kind="voice",
    artifact_path=VOICE_REVIEW_ARTIFACT,
    artifact_type="voice_review",
    items=CHECKLIST_ITEMS,
    fix_tabs=("voice", "script"),
    missing_media_blocker="Onaylanacak bir seslendirme sesi yok.",
    revision_blocker=("Bu seslendirme icin acik bir 'revizyon gerekli' kaydi var: "
                      "{notes}. Duzeltip sesi yeniden uret."),
    stale_blocker=("{count} kontrol maddesi BASKA bir ses dosyasina isaretlenmis. "
                   "Seslendirme yeniden uretildi; maddeleri bu ses icin tekrar isaretle."),
)


def new_review(ownership, audio_sha256: str | None = None) -> dict:
    return media_review.new_review(SPEC, ownership, audio_sha256)


#: Per-language wording for the one item whose question genuinely depends on the
#: language being spoken. Everything else (pronunciation, pacing, tone, script
#: match, audio quality) asks the same thing whatever the language.
_NATURALNESS_BY_LANGUAGE = {
    "en-US": {
        "label": "English naturalness",
        "question": "Does the stress and intonation sound like natural American "
                    "English, rather than robotic or foreign-accented?",
    },
    "tr-TR": {
        "label": "Turkce dogalligi",
        "question": "Vurgu ve ekler Turkce'ye dogal geliyor mu; makine gibi ya da "
                    "yabanci aksanli duyulmuyor mu?",
    },
}


#: Extra wording for the two questions a narration PRESET makes specific. A
#: preset that asks for deliberate emphasis and pauses turns "does the tone fit?"
#: into a checkable claim, and the reviewer should be asked the checkable one.
_PRESET_QUESTIONS = {
    "deep_storyteller": {
        "pronunciation": {
            "label": "Ozel isim telaffuzu",
            "question": "Are the proper names pronounced correctly and consistently "
                        "(Daemon, Rhaenyra, Targaryen)? Listen for each one "
                        "separately, not just the first.",
        },
        # Tracks the preset's own wording. When the preset asked for deliberate
        # per-word stress this asked about that; it now asks about NATURAL
        # sentence-level emphasis, because certifying a delivery the preset no
        # longer requests is worse than not asking at all.
        "tone": {
            "label": "Vurgu ve tempo",
            "question": "Is the delivery a controlled, cinematic storyteller: "
                        "slightly slower than normal speech and measured but not "
                        "laboured, with emphasis falling naturally at sentence "
                        "level rather than pushed onto every important word -- and "
                        "WITHOUT tipping into exaggerated movie-trailer delivery?",
        },
    },
}


def items_for(content_language: str | None = None,
              narration_preset: str | None = None) -> tuple:
    """The checklist, worded for what was actually asked for.

    Asking "does this sound natural in Turkish?" about an English narration is
    not a harder question -- it is the WRONG question, and a reviewer who ticks
    it has certified nothing. The same applies to asking "does the tone fit?"
    when a preset requested a specific, checkable delivery.

    Keys never change, so re-wording can never invalidate an existing tick.
    """
    language_override = _NATURALNESS_BY_LANGUAGE.get(content_language or "")
    preset_overrides = _PRESET_QUESTIONS.get(narration_preset or "", {})
    if not language_override and not preset_overrides:
        return CHECKLIST_ITEMS

    items = []
    for item in CHECKLIST_ITEMS:
        merged = dict(item)
        if language_override and item["key"] == "naturalness":
            merged.update(language_override)
        if item["key"] in preset_overrides:
            merged.update(preset_overrides[item["key"]])
        items.append(merged)
    return tuple(items)


#: Kept for callers that only care about language.
def items_for_language(content_language: str | None) -> tuple:
    return items_for(content_language=content_language)


def evaluate(review: dict | None, audio_sha256: str | None, ownership,
             content_language: str | None = None,
             narration_preset: str | None = None) -> dict:
    spec = SPEC
    if content_language or narration_preset:
        # Same keys, same rules, different wording -- so a language or preset
        # change never invalidates a tick, it only re-labels the question.
        spec = media_review.ReviewSpec(
            **{**SPEC.__dict__,
               "items": items_for(content_language, narration_preset)})
    state = media_review.evaluate(spec, review, audio_sha256, ownership)
    state["content_language"] = content_language
    state["narration_preset"] = narration_preset
    return state


def set_item(review: dict, key: str, *, checked: bool, audio_sha256: str,
             reviewed_by: str, note: str = "") -> dict:
    return media_review.set_item(SPEC, review, key, checked=checked,
                                  media_sha256=audio_sha256,
                                  reviewed_by=reviewed_by, note=note)


def request_revision(review: dict, *, audio_sha256: str, requested_by: str,
                     notes: str, target_tab: str = "voice") -> dict:
    return media_review.request_revision(
        SPEC, review, media_sha256=audio_sha256, requested_by=requested_by,
        notes=notes, target_tab=target_tab)


def clear_revision(review: dict, *, audio_sha256: str, resolved_by: str) -> dict:
    return media_review.clear_revision(SPEC, review, media_sha256=audio_sha256,
                                        resolved_by=resolved_by)


def technical_summary(voiceover: dict | None, script: dict | None = None) -> dict:
    """Flat, display-ready facts about the produced audio. Pure read.

    Includes the script-hash link in BOTH directions, because "this audio was
    made from that script" is the one claim a listener cannot verify by ear.
    """
    if not isinstance(voiceover, dict):
        return {"available": False}
    validation = voiceover.get("audio_validation") or {}
    details = validation.get("details") or {}
    checks = validation.get("checks") or {}
    script_hash = voiceover.get("source_script_hash")
    script_text = (script or {}).get("script_text")
    from local_pipeline import google_ai

    expected = google_ai.sha256_text(script_text)[:16] if script_text else None
    return {
        "available": True,
        "status": voiceover.get("status"),
        "status_reason": voiceover.get("status_reason"),
        "provider": voiceover.get("provider"),
        "model": voiceover.get("model"),
        "preview_model": voiceover.get("preview_model"),
        "voice_id": voiceover.get("voice_id"),
        "language_code": voiceover.get("language_code"),
        "narration_direction": voiceover.get("narration_direction"),
        "duration_sec": voiceover.get("duration_sec") or details.get("duration_sec"),
        "format": ("WAV / PCM s16le" if checks.get("codec_pcm_s16le")
                   else details.get("codec") or "bilinmiyor"),
        "sample_rate": details.get("sample_rate"),
        "channels": details.get("channels"),
        "bits_per_sample": details.get("bits_per_sample"),
        "size_bytes": details.get("size_bytes"),
        "audio_sha256_16": (voiceover.get("output_sha256")
                            or voiceover.get("output_audio_sha256") or "")[:16] or None,
        "source_script_hash": script_hash,
        # None when there is no script to compare against; True/False otherwise,
        # so "unknown" is never displayed as "matches".
        "script_hash_matches": (None if not expected or not script_hash
                                else expected == script_hash),
        "script_status": voiceover.get("source_script_status"),
        "validation_passed": validation.get("passed"),
        "validation_checks": checks,
        "chunks": voiceover.get("chunks"),
        "not_uploaded": voiceover.get("not_uploaded"),
    }
