"""Named narration styles: a voice from the registry plus a delivery direction.

WHY A PRESET AND NOT TWO FREE FIELDS
------------------------------------
Voice and delivery direction only make sense together. "Charon" with an excited
trailer direction is not the deep storyteller anyone asked for, and a careful
direction attached to `Fenrir` (documented characteristic: *Excitable*) fights
the voice instead of shaping it. Pairing them under one name makes the choice
reviewable, storable in the plan decision, and comparable across runs.

The preset lives in the plan DECISION, so changing it changes the plan's
approval hash and invalidates the voiceover and render approvals that were
granted for the previous delivery -- which is right: a Short approved with one
narration is not approved with another.

VOICE SELECTION IS NOT INVENTED
-------------------------------
Every `voice` here must already exist in `google_ai.GEMINI_TTS_VOICES`, which
mirrors the published Gemini TTS voice list. `VOICE_CHARACTERISTICS` records the
one-word characteristic Google documents for each candidate, so the choice below
can be audited rather than taken on trust. `validate_presets()` fails loudly if a
preset ever names a voice the API does not support.
"""
from __future__ import annotations

from .google_ai import GEMINI_TTS_VOICES

#: The documented characteristic for each voice considered for storytelling.
#: Copied from the Gemini TTS voice-options table; used here only to justify a
#: selection in writing, never to construct a voice id.
VOICE_CHARACTERISTICS = {
    "Charon": "Informative",
    "Gacrux": "Mature",
    "Algenib": "Gravelly",
    "Iapetus": "Clear",
    "Rasalgethi": "Informative",
    "Sadaltager": "Knowledgeable",
    "Alnilam": "Firm",
    "Orus": "Firm",
    "Schedar": "Even",
    "Enceladus": "Breathy",
    "Fenrir": "Excitable",
    "Kore": "Firm",
}

NARRATION_PRESETS = {
    "deep_storyteller": {
        "key": "deep_storyteller",
        "label": "Deep storyteller (EN)",
        "language_code": "en-US",
        "voice": "Charon",
        # Written down because "why this voice?" is a real question a reviewer
        # will ask, and the honest answer is a documented characteristic plus a
        # ruled-out shortlist -- not a preference.
        "voice_rationale": (
            "Charon is documented as *Informative*: a measured, explanatory adult "
            "male delivery, which is what a controlled cinematic storyteller needs. "
            "Ruled out: Fenrir (*Excitable*) is the trailer energy this preset "
            "exists to avoid; Algenib (*Gravelly*) trades naturalness for texture; "
            "Gacrux (*Mature*) is the closest runner-up but carries no documented "
            "narrative or depth quality; Kore/Orus/Alnilam (*Firm*) read as "
            "assertive rather than deep."
        ),
        "runner_up": "Gacrux",
        # The direction is prepended to the script as a style instruction. Stated
        # as concrete, checkable behaviours -- "be cinematic" is not an
        # instruction a model can follow or a reviewer can verify.
        "narration_direction": (
            "Read this as a deep, adult male storyteller narrating a documentary. "
            "Speak about 5-8% slower than a normal conversational pace: noticeably "
            "measured, but not laboured. Keep the delivery calm, controlled and "
            "cinematic while still sounding natural and human. Let emphasis fall "
            "where it naturally would at sentence level; do not deliberately stress "
            "every important word or proper name. Keep pauses at commas and full "
            "stops short and unforced, with only a slight beat before the final "
            "sentence. Do not rush, do not shout, and avoid exaggerated "
            "movie-trailer delivery."
        ),
        "description": (
            "Derin, yetiskin erkek anlatici; normalden ~%5-8 daha yavas, kontrollu ve "
            "sinematik ama dogal. Vurgu cumle duzeyinde dogal akisina birakilir "
            "(kelime kelime vurgulanmaz), duraklamalar kisa ve zorlamasiz. Abartili "
            "fragman tonu YOK."
        ),
        # Which review questions this preset makes checkable. The checklist reads
        # these so the reviewer is asked about what was actually requested.
        "review_focus": ("pronunciation", "tone"),
    },
    "default": {
        "key": "default",
        "label": "Varsayilan (yonergesiz)",
        "language_code": None,      # usable in any language
        "voice": "Kore",
        "voice_rationale": "Kore is the pipeline's historical default (*Firm*).",
        "runner_up": None,
        "narration_direction": "",
        "description": "Ses ve yonerge elle ayarlanir; hazir bir stil uygulanmaz.",
        "review_focus": (),
    },
}

DEFAULT_NARRATION_PRESET = "default"
NARRATION_PRESET_KEYS = tuple(NARRATION_PRESETS)


def validate_presets() -> list:
    """Reasons a preset is unusable. Empty list means every voice is supported."""
    problems = []
    for key, preset in NARRATION_PRESETS.items():
        if preset["voice"] not in GEMINI_TTS_VOICES:
            problems.append(
                f"{key}: '{preset['voice']}' Gemini TTS ses listesinde yok "
                f"(uydurulmus ses kimligi)")
        runner_up = preset.get("runner_up")
        if runner_up and runner_up not in GEMINI_TTS_VOICES:
            problems.append(f"{key}: yedek ses '{runner_up}' listede yok")
    return problems


def resolve_narration_preset(key, *, language_code=None) -> dict:
    """The stored narration block for a preset, or raise.

    Returned as a plain dict because it goes into the plan decision and is
    therefore hashed -- it must be JSON-stable, not an object reference.
    """
    name = str(key or DEFAULT_NARRATION_PRESET).strip()
    preset = NARRATION_PRESETS.get(name)
    if preset is None:
        raise ValueError(
            f"bilinmeyen anlatim on ayari: {name!r} "
            f"(beklenen: {list(NARRATION_PRESET_KEYS)})")
    if preset["voice"] not in GEMINI_TTS_VOICES:
        raise ValueError(f"on ayar desteklenmeyen bir ses tasiyor: {preset['voice']!r}")
    if (preset["language_code"] and language_code
            and preset["language_code"] != language_code):
        raise ValueError(
            f"{name} on ayari {preset['language_code']} icindir; "
            f"secilen icerik dili {language_code}")
    return {
        "preset": preset["key"],
        "voice": preset["voice"],
        "narration_direction": preset["narration_direction"],
        "language_code": preset["language_code"] or language_code,
    }


def preset_public_list(language_code=None) -> list:
    """Display-ready presets, filtered to those valid for a language."""
    items = []
    for preset in NARRATION_PRESETS.values():
        if (language_code and preset["language_code"]
                and preset["language_code"] != language_code):
            continue
        items.append({
            "key": preset["key"], "label": preset["label"],
            "voice": preset["voice"], "language_code": preset["language_code"],
            "description": preset["description"],
            "voice_characteristic": VOICE_CHARACTERISTICS.get(preset["voice"]),
            "voice_rationale": preset["voice_rationale"],
        })
    return items
