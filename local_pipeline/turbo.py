"""Turbo Studio: the workflow's shape, rules and estimates. No side effects.

This module is the counterpart of `long_form.py` -- it decides WHAT should
happen and in what order, and it calls no provider, opens no socket and touches
no file. `creator_console/turbo_studio.py` is what actually runs the stages.
Keeping the split means the whole workflow can be reasoned about, and tested,
without a network.

WHY THE INVALIDATION IS A GRAPH AND NOT A POSITION
--------------------------------------------------
`long_form.invalidate_from` resets stage k and everything after it, which is
right when the stages form a straight line. Turbo's do not. Regenerating one
narration chunk must invalidate the timeline (position 10) while PRESERVING the
downloaded stock clips (position 7) -- those cost bandwidth and a provider quota
and have nothing to do with the narration. A positional reset cannot express
that, so `STAGE_INPUTS` records what each stage actually consumes and
`stale_stages` walks the closure.
"""
from __future__ import annotations

import hashlib
import json
import re
from math import ceil

from . import stage_machine

SCHEMA_VERSION = 1

ARTIFACT_ROOT = "results/turbo"
WORKFLOW_ARTIFACT = "results/turbo/workflow.json"
BRIEF_ARTIFACT = "results/turbo/brief.json"
SCRIPT_ARTIFACT = "results/turbo/script.json"
MEDIA_PLAN_ARTIFACT = "results/turbo/media_plan.json"
TIMELINE_ARTIFACT = "results/turbo/timeline.json"
CHUNK_ARTIFACT = "results/turbo/tts_chunks.json"
NARRATION_PATH = "results/turbo/narration.wav"
SUBTITLE_PATH = "results/turbo/subtitles.srt"

#: (key, label, automatic). Automatic means an unattended runner may start it.
#: `final_render` is deliberately NOT automatic: it costs minutes of CPU and it
#: must never fire on its own the moment a preview finishes. A person watches
#: the preview and then asks for the final -- that request IS the approval.
STAGES = (
    ("script",          "Script ve sahne plani",             True),
    ("script_review",   "Script incelemesi",                 False),
    ("tts",             "Anlatim parcalari",                 True),
    ("audio_concat",    "Anlatim birlestirme",               True),
    ("audio_review",    "Ses incelemesi",                    False),
    ("media_plan",      "Klip slotlari",                     True),
    ("media",           "Stok materyal (ara / sec / indir)", False),
    ("music",           "Muzik secimi ve lisans",            False),
    ("subtitles",       "Altyazi zamanlamasi",               True),
    ("timeline",        "Zaman cizelgesi manifesti",         True),
    ("timeline_review", "Zaman cizelgesi incelemesi",        False),
    ("preview_render",  "Onizleme render",                   True),
    ("final_render",    "Final render",                      False),
    ("archive",         "Arsiv / disa aktarim",              False),
)
STAGE_KEYS = tuple(key for key, _label, _auto in STAGES)
STAGE_LABELS = {key: label for key, label, _auto in STAGES}
AUTOMATIC_STAGES = frozenset(key for key, _label, auto in STAGES if auto)
HUMAN_STAGES = tuple(key for key in STAGE_KEYS if key not in AUTOMATIC_STAGES)

#: What each stage consumes. Names that are not stage keys ("brief") are
#: external inputs -- things a revision can change directly.
STAGE_INPUTS = {
    "script":          ("brief",),
    "script_review":   ("script",),
    "tts":             ("script",),
    "audio_concat":    ("tts",),
    "audio_review":    ("audio_concat",),
    # Slots come from the script beats alone. Durations are assigned by the
    # `timeline` packer, so a longer narration re-packs the timeline instead
    # of discarding footage that is already downloaded and still relevant.
    # A shortfall of clips is covered by reuse, not by re-fetching.
    "media_plan":      ("script",),
    "media":           ("media_plan",),
    "music":           (),
    "subtitles":       ("audio_concat",),
    "timeline":        ("brief", "audio_concat", "subtitles", "media_plan",
                        "media", "music"),
    "timeline_review": ("timeline",),
    "preview_render":  ("timeline",),
    "final_render":    ("timeline",),
    "archive":         ("final_render",),
}


class TurboError(ValueError):
    """A rejected Turbo operation, phrased for the operator who caused it."""


_MACHINE = stage_machine.StageMachine(
    STAGES, artifact_type="turbo_workflow", schema_version=SCHEMA_VERSION,
    error_cls=TurboError)

# The state vocabulary is shared with long-form; re-exported for callers.
NOT_STARTED = stage_machine.NOT_STARTED
QUEUED = stage_machine.QUEUED
RUNNING = stage_machine.RUNNING
WAITING_RATE_LIMIT = stage_machine.WAITING_RATE_LIMIT
WAITING_DAILY_QUOTA = stage_machine.WAITING_DAILY_QUOTA
BLOCKED_BILLING = stage_machine.BLOCKED_BILLING
REVIEW_REQUIRED = stage_machine.REVIEW_REQUIRED
COMPLETED = stage_machine.COMPLETED
FAILED = stage_machine.FAILED
CANCELLED = stage_machine.CANCELLED
WAITING_STATES = stage_machine.WAITING_STATES
STAGE_STATES = stage_machine.STAGE_STATES


# ------------------------------------------------------------------- brief
ASPECT_RATIOS = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1":  (1080, 1080),
}
ORDERINGS = ("sequential", "seeded_random")
FIT_MODES = ("crop", "fit")
TRANSITIONS = ("none", "fade")
CONTENT_TYPES = ("explainer", "listicle", "story", "news_recap", "tutorial")

MIN_DURATION_SEC = 15
MAX_DURATION_SEC = 1800          # 30 min; beyond this the clip count explodes
MIN_CLIP_SEC = 2.0
MAX_CLIP_SEC = 15.0
#: Above this the ffmpeg invocation approaches Windows' 32,768-character
#: command-line limit even with the graph in a file, so the brief refuses here
#: rather than letting the render fail obscurely half an hour later.
MAX_CLIPS = 240
#: Words per minute a synthesised voice actually delivers, taken from the
#: long-form pilot's measured narration rather than guessed.
WORDS_PER_MINUTE = 150
#: One constant for BOTH the up-front estimate and the actual chunk planner.
#: When they disagreed, a 60-second brief promised "1 TTS request" and then
#: planned 12 -- the estimate exists precisely so the operator knows the quota
#: cost before spending it, so an estimate that undercounts is worse than none.
TTS_WORDS_PER_CHUNK = 180


def _clean(value, limit: int = 400) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def new_brief(**fields) -> dict:
    brief = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "turbo_brief",
        "topic": "",
        "content_type": "explainer",
        "language": "tr",
        "duration_sec": 60,
        "aspect_ratio": "9:16",
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "clip_duration_sec": 5.0,
        "fit_mode": "crop",
        "transition": "none",
        "transition_sec": 0.4,
        "ordering": "sequential",
        "seed_input": "",
        "voice": "",
        "subtitles_enabled": True,
        "music_enabled": False,
        "notes": "",
        "not_uploaded": True,
    }
    brief.update(fields)
    return brief


def estimate_clip_count(duration_sec, clip_duration_sec) -> int:
    if float(clip_duration_sec or 0) <= 0:
        return 0
    return int(ceil(float(duration_sec) / float(clip_duration_sec)))


def estimates(brief: dict) -> dict:
    """Up-front numbers the operator sees BEFORE anything is spent.

    Labelled as estimates because they are: the real narration length is
    whatever the TTS actually produces, and the timeline is packed against that
    measured duration, never against this guess.
    """
    duration = float(brief.get("duration_sec") or 0)
    clip_sec = float(brief.get("clip_duration_sec") or 5.0)
    words = int(round(duration / 60.0 * WORDS_PER_MINUTE))
    clips = estimate_clip_count(duration, clip_sec)
    return {
        "narration_words": words,
        "clip_count": clips,
        "tts_chunks_estimate": max(1, int(ceil(words / float(TTS_WORDS_PER_CHUNK)))),
        "stock_searches_estimate": clips,
        "note": ("tahmini degerler; gercek anlatim suresi TTS ciktisindan "
                 "olculur ve zaman cizelgesi o olcume gore paketlenir"),
    }


def brief_sha256(brief: dict) -> str:
    """Hash only the fields that change the OUTPUT, so a cosmetic edit (notes)
    does not invalidate work that is still perfectly valid."""
    material = {key: brief.get(key) for key in (
        "topic", "content_type", "language", "duration_sec", "aspect_ratio",
        "fps", "clip_duration_sec", "fit_mode", "transition", "transition_sec",
        "ordering", "seed_input", "voice", "subtitles_enabled", "music_enabled")}
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def validate_brief(brief: dict) -> dict:
    """Return a normalised brief, or raise TurboError naming the bad field.

    Every message names the field and its accepted range, because the operator
    reads this text and has to know what to change.
    """
    data = dict(brief or {})
    topic = _clean(data.get("topic"), 200)
    if len(topic) < 8:
        raise TurboError("konu en az 8 karakter olmali")

    content_type = _clean(data.get("content_type"), 40) or "explainer"
    if content_type not in CONTENT_TYPES:
        raise TurboError("icerik turu su secenklerden biri olmali: "
                         + ", ".join(CONTENT_TYPES))

    try:
        duration = int(float(data.get("duration_sec") or 0))
    except (TypeError, ValueError):
        raise TurboError("sure saniye cinsinden bir sayi olmali")
    if not (MIN_DURATION_SEC <= duration <= MAX_DURATION_SEC):
        raise TurboError(
            "sure {0}-{1} saniye araliginda olmali (girilen: {2})".format(
                MIN_DURATION_SEC, MAX_DURATION_SEC, duration))

    aspect = _clean(data.get("aspect_ratio"), 10) or "9:16"
    if aspect not in ASPECT_RATIOS:
        raise TurboError("en-boy orani su secenklerden biri olmali: "
                         + ", ".join(ASPECT_RATIOS))

    try:
        clip_sec = round(float(data.get("clip_duration_sec") or 5.0), 2)
    except (TypeError, ValueError):
        raise TurboError("klip suresi bir sayi olmali")
    if not (MIN_CLIP_SEC <= clip_sec <= MAX_CLIP_SEC):
        raise TurboError(
            "klip suresi {0}-{1} saniye araliginda olmali (girilen: {2})".format(
                MIN_CLIP_SEC, MAX_CLIP_SEC, clip_sec))

    clip_count = estimate_clip_count(duration, clip_sec)
    if clip_count > MAX_CLIPS:
        raise TurboError(
            "bu sure ve klip suresi {0} klip gerektiriyor; ust sinir {1}. "
            "Klip suresini uzatin veya videoyu kisaltin".format(
                clip_count, MAX_CLIPS))

    fit_mode = _clean(data.get("fit_mode"), 10) or "crop"
    if fit_mode not in FIT_MODES:
        raise TurboError("kadraj modu su secenklerden biri olmali: "
                         + ", ".join(FIT_MODES))

    transition = _clean(data.get("transition"), 10) or "none"
    if transition not in TRANSITIONS:
        raise TurboError("gecis su secenklerden biri olmali: "
                         + ", ".join(TRANSITIONS))

    ordering = _clean(data.get("ordering"), 20) or "sequential"
    if ordering not in ORDERINGS:
        raise TurboError("siralama su secenklerden biri olmali: "
                         + ", ".join(ORDERINGS))

    try:
        fps = int(data.get("fps") or 30)
    except (TypeError, ValueError):
        raise TurboError("fps bir tamsayi olmali")
    if fps not in (24, 25, 30, 60):
        raise TurboError("fps 24, 25, 30 veya 60 olmali")

    try:
        transition_sec = round(float(data.get("transition_sec") or 0.4), 2)
    except (TypeError, ValueError):
        raise TurboError("gecis suresi bir sayi olmali")
    if transition == "fade":
        # A fade longer than half a clip would overlap the clip's own fade-in.
        ceiling = min(1.5, clip_sec / 2.0)
        if not (0.1 <= transition_sec <= ceiling):
            raise TurboError(
                "gecis suresi 0.1 ile {0:.2f} saniye arasinda olmali "
                "(klip suresinin yarisini gecemez)".format(ceiling))

    width, height = ASPECT_RATIOS[aspect]
    result = new_brief(
        topic=topic, content_type=content_type,
        language=_clean(data.get("language"), 10) or "tr",
        duration_sec=duration, aspect_ratio=aspect, width=width, height=height,
        fps=fps, clip_duration_sec=clip_sec, fit_mode=fit_mode,
        transition=transition, transition_sec=transition_sec, ordering=ordering,
        seed_input=_clean(data.get("seed_input"), 80),
        voice=_clean(data.get("voice"), 80),
        subtitles_enabled=bool(data.get("subtitles_enabled", True)),
        music_enabled=bool(data.get("music_enabled", False)),
        notes=_clean(data.get("notes"), 1000),
    )
    result["estimates"] = estimates(result)
    result["brief_sha256"] = brief_sha256(result)
    return result


# ---------------------------------------------------------------- workflow
def new_workflow(*, brief_digest: str = "", ownership=None) -> dict:
    return _MACHINE.new_workflow(ownership=ownership, brief_sha256=brief_digest)


def stage_state(record, key: str) -> str:
    return _MACHINE.stage_state(record, key)


def set_stage(record: dict, key: str, state: str, *, error=None, detail=None,
              attempt_delta: int = 0, resume_at=None) -> dict:
    return _MACHINE.set_stage(record, key, state, error=error, detail=detail,
                              attempt_delta=attempt_delta, resume_at=resume_at)


def invalidate_from(record: dict, key: str, *, reason: str = "") -> dict:
    """Positional reset, kept for exactly one case: the brief changed, so
    everything from `script` onward describes a video nobody asked for."""
    return _MACHINE.invalidate_from(record, key, reason=reason)


def next_stage(record):
    return _MACHINE.next_stage(record)


def runnable_stage(record, *, now=None) -> dict:
    return _MACHINE.runnable_stage(record, now=now)


def overall_status(record) -> str:
    return _MACHINE.overall_status(record)


def progress_summary(record) -> dict:
    return _MACHINE.progress_summary(record)


# ------------------------------------------------------- graph invalidation
def stale_stages(changed) -> tuple:
    """Every stage whose inputs are no longer what it was built from.

    `changed` names artifacts or stages that just changed. The closure follows
    STAGE_INPUTS forward: a stage is stale when anything it consumes is stale.
    Returned in STAGE_KEYS order so a caller can reset them top-down.
    """
    dirty = {str(name) for name in (changed or ())}
    if not dirty:
        return ()
    grew = True
    while grew:
        grew = False
        for key in STAGE_KEYS:
            if key in dirty:
                continue
            if any(source in dirty for source in STAGE_INPUTS.get(key, ())):
                dirty.add(key)
                grew = True
    return tuple(key for key in STAGE_KEYS if key in dirty)


def invalidate_stages(record: dict, keys, *, reason: str = "") -> dict:
    """Reset exactly the named stages, leaving every other stage alone."""
    record = dict(record or new_workflow())
    for key in keys:
        if key not in STAGE_KEYS:
            raise TurboError("bilinmeyen asama: {0!r}".format(key))
        if stage_state(record, key) != NOT_STARTED:
            record = set_stage(record, key, NOT_STARTED,
                               detail=(reason or "girdi degisti"))
    return record


#: What each revision kind changes, and therefore what goes stale. The stages
#: NOT listed are what the revision deliberately PRESERVES -- that is the whole
#: point: a crop tweak must not cost a re-download, and re-recording one
#: narration chunk must not cost the other chunks or any stock footage.
#:
#: `manifest_only` revisions edit timeline.json in place and never re-run the
#: `timeline` stage, so the clip choices and their downloads survive untouched.
REVISION_KINDS = {
    "frame":           {"changed": (), "manifest_only": True,
                        "label": "Kadraj / zoom degisikligi"},
    "retime":          {"changed": (), "manifest_only": True,
                        "label": "Klip suresi degisikligi"},
    "subtitle_style":  {"changed": (), "manifest_only": True,
                        "label": "Altyazi stili"},
    "music_volume":    {"changed": (), "manifest_only": True,
                        "label": "Muzik ses duzeyi"},
    "music_remove":    {"changed": (), "manifest_only": True,
                        "label": "Muzigi kaldir"},
    "replace_clip":    {"changed": ("media",), "manifest_only": False,
                        "label": "Klibi degistir"},
    "recue":           {"changed": ("subtitles",), "manifest_only": False,
                        "label": "Altyaziyi yeniden olustur"},
    "narration_chunk": {"changed": ("tts",), "manifest_only": False,
                        "label": "Tek anlatim parcasini yenile"},
    "script_edit":     {"changed": ("script",), "manifest_only": False,
                        "label": "Script duzenlemesi"},
    "brief_edit":      {"changed": ("brief",), "manifest_only": False,
                        "label": "Brief degisikligi"},
}
#: A manifest-only revision changes pixels but no upstream artifact, so exactly
#: the two render stages are stale.
RENDER_STAGES = ("preview_render", "final_render")


def revision_effect(kind: str) -> dict:
    """What a revision would invalidate -- computed BEFORE it is applied, so the
    UI can tell the operator what they are about to lose and what survives."""
    spec = REVISION_KINDS.get(str(kind or ""))
    if spec is None:
        raise TurboError("bilinmeyen revizyon turu: {0!r}".format(kind))
    if spec["manifest_only"]:
        stale = RENDER_STAGES
    else:
        stale = stale_stages(spec["changed"])
    return {
        "kind": kind,
        "label": spec["label"],
        "manifest_only": spec["manifest_only"],
        "stale_stages": stale,
        "stale_labels": [STAGE_LABELS[key] for key in stale],
        "preserved_stages": tuple(key for key in STAGE_KEYS if key not in stale),
    }
