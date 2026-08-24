"""Per-slot render presentation settings the operator may change after Phase 0.

WHY THIS EXISTS
---------------
Phase 0 decides the STRATEGY: which mode, which language, how many outputs. Those
are content decisions and changing one has to re-open the plan approval. But
"render it at 1080p instead of a preview", "use the Cinzel caption theme", "pull
the crop left a bit" are not strategy -- they are presentation, and forcing a
Phase 0 re-approval for them made the console effectively read-only after the
first render.

    results/render_profiles/output_NN.json

So this artifact holds the presentation half, per output slot, and
`check_config_drift` keeps guarding the content half. A profile can only
override keys in `OVERRIDABLE_RENDER_KEYS`; `subtitle_whisper_model_size` and
`subtitle_language` are NOT there, because they change which WORDS appear on
screen, not how they look.

NO RAW ENCODER INPUT
--------------------
The client picks a named preset. Width, height, CRF and the x264 preset are
resolved HERE, on the server. A browser cannot send a codec string, an FFmpeg
argument or a font file path -- `resolve_quality`/`resolve_resolution` accept
only names from the tables below, and an unknown name raises rather than
falling back to a default that nobody chose.

WHAT THIS MODULE MUST NOT DO
----------------------------
It never renders, never approves, never touches a video, and never decides that
a change is safe -- `PipelineService` owns the archive-and-invalidate step.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from local_pipeline import render_utils

SCHEMA_VERSION = 1
RENDER_PROFILE_ARTIFACT_TYPE = "render_profile"

#: Presentation keys a profile may override. Everything else in the Phase 0
#: render decision stays plan-bound and keeps raising drift.
OVERRIDABLE_RENDER_KEYS = ("render_mode", "preview_mode", "caption_style",
                           "caption_theme")

#: (scale, crf, x264 preset). `preview` and `final` reproduce exactly what
#: `preview_mode=True/False` already did, so switching to named presets changes
#: no existing output. `draft` is new: a fast look at framing and captions.
QUALITY_PRESETS = {
    "draft":   {"scale": 0.35, "crf": 32, "encode_preset": "ultrafast"},
    "preview": {"scale": 0.5,  "crf": 30, "encode_preset": "ultrafast"},
    "final":   {"scale": 1.0,  "crf": 18, "encode_preset": "slow"},
}

#: Only `final` is delivery quality; the other two carry a visible caveat so a
#: draft is never mistaken for the finished Short.
DELIVERY_QUALITY = "final"

RESOLUTION_PRESETS = {
    "vertical_1080x1920": {"width": 1080, "height": 1920},
    "vertical_720x1280": {"width": 720, "height": 1280},
}

RENDER_MODES = ("blurred_background", "center_crop")

#: `active_word_yellow` is what `render_utils` branches on for the dynamic ASS
#: path; anything else falls back to plain cues. Modelled as a boolean here so
#: the UI offers a switch instead of a magic string.
CAPTION_STYLE_DYNAMIC = "active_word_yellow"
CAPTION_STYLE_PLAIN = "static_block"

#: Fonts come from the shared caption themes, so a profile can never name a font
#: file the renderer cannot load.
def allowed_themes() -> tuple:
    return tuple(sorted(render_utils.CAPTION_THEME_PROFILES))


ACTIVE_COLORS = {
    "yellow": "&H0000FFFF",
    "white": "&H00FFFFFF",
    "cyan": "&H00FFFF00",
}

FONT_SIZE_MIN, FONT_SIZE_MAX = 28, 120
MARGIN_V_MIN, MARGIN_V_MAX = 40, 600
ZOOM_MIN, ZOOM_MAX = 1.0, 2.0


class RenderProfileError(ValueError):
    """An operator-facing rejection. Carries no secret and no stack detail."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_profile(plan_decision: dict | None = None, *, slot: int = 1) -> dict:
    """Phase 0's render decision, expressed as a profile.

    This is the STARTING POINT, not a second source of truth: every field here
    is read from the approved plan when the plan has one, so a project that
    never opens Render Studio renders exactly as Phase 0 said it would.
    """
    render = ((plan_decision or {}).get("render") or {})
    preview = render.get("preview_mode")
    return {
        "resolution_preset": "vertical_1080x1920",
        "quality_preset": "preview" if preview is not False else "final",
        "render_mode": render.get("render_mode") or "blurred_background",
        "caption_theme": render.get("caption_theme") or "auto",
        "caption_font_size": None,      # None = follow the theme
        "caption_margin_v": None,
        "active_word_highlight": (render.get("caption_style")
                                  or CAPTION_STYLE_DYNAMIC) == CAPTION_STYLE_DYNAMIC,
        "active_word_color": "yellow",
        "frame_zoom": 1.0,
        "frame_focus_x": 0.5,
        "frame_focus_y": 0.5,
        "output_slot": int(slot or 1),
    }


def _clamped(value, low, high, label):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RenderProfileError(f"{label} bir sayi olmali") from exc
    if not (low <= number <= high):
        raise RenderProfileError(
            f"{label} {low} ile {high} arasinda olmali (girilen: {number})")
    return number


def validate(values: dict, *, slot: int = 1, plan_decision: dict | None = None) -> dict:
    """A normalised profile, or a rejection that names the field.

    Unknown fields are REFUSED rather than dropped: silently ignoring a setting
    the operator believed they changed is the failure this whole artifact exists
    to prevent.
    """
    base = default_profile(plan_decision, slot=slot)
    data = dict(values or {})
    unknown = sorted(set(data) - set(base) - {"reason"})
    if unknown:
        raise RenderProfileError(
            "bilinmeyen render profile alani: " + ", ".join(unknown))

    merged = {**base, **{k: v for k, v in data.items() if k in base}}

    if merged["resolution_preset"] not in RESOLUTION_PRESETS:
        raise RenderProfileError(
            "cozunurluk preset'i su seceneklerden biri olmali: "
            + ", ".join(sorted(RESOLUTION_PRESETS)))
    if merged["quality_preset"] not in QUALITY_PRESETS:
        raise RenderProfileError(
            "kalite preset'i su seceneklerden biri olmali: "
            + ", ".join(sorted(QUALITY_PRESETS)))
    if merged["render_mode"] not in RENDER_MODES:
        raise RenderProfileError(
            "render modu su seceneklerden biri olmali: " + ", ".join(RENDER_MODES))
    theme = merged["caption_theme"]
    if theme != "auto" and theme not in render_utils.CAPTION_THEME_PROFILES:
        raise RenderProfileError(
            "altyazi temasi su seceneklerden biri olmali: auto, "
            + ", ".join(allowed_themes()))
    if merged["active_word_color"] not in ACTIVE_COLORS:
        raise RenderProfileError(
            "vurgu rengi su seceneklerden biri olmali: "
            + ", ".join(sorted(ACTIVE_COLORS)))

    if merged["caption_font_size"] is not None:
        merged["caption_font_size"] = int(_clamped(
            merged["caption_font_size"], FONT_SIZE_MIN, FONT_SIZE_MAX, "font boyutu"))
    if merged["caption_margin_v"] is not None:
        merged["caption_margin_v"] = int(_clamped(
            merged["caption_margin_v"], MARGIN_V_MIN, MARGIN_V_MAX, "altyazi konumu"))
    merged["frame_zoom"] = round(_clamped(
        merged["frame_zoom"], ZOOM_MIN, ZOOM_MAX, "kadraj zoom"), 4)
    merged["frame_focus_x"] = round(_clamped(
        merged["frame_focus_x"], 0.0, 1.0, "odak x"), 4)
    merged["frame_focus_y"] = round(_clamped(
        merged["frame_focus_y"], 0.0, 1.0, "odak y"), 4)
    merged["active_word_highlight"] = bool(merged["active_word_highlight"])
    merged["output_slot"] = int(slot or 1)
    return merged


def content_sha256(profile: dict) -> str:
    """Digest of the SETTINGS only.

    Timestamps, actor and reason are excluded on purpose: saving the same
    settings twice must not look like a change, or every save would archive a
    render that is still perfectly valid.
    """
    material = {key: profile.get(key) for key in sorted(default_profile())}
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_artifact(profile: dict, *, slot: int, base_plan_hash: str | None,
                   actor: str, reason: str = "",
                   previous_sha256: str | None = None) -> dict:
    settings = dict(profile)
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": RENDER_PROFILE_ARTIFACT_TYPE,
        "output_slot": int(slot or 1),
        "base_plan_hash": base_plan_hash,
        "content_sha256": content_sha256(settings),
        "previous_content_sha256": previous_sha256,
        "updated_at": _now(),
        "updated_by": str(actor or "console")[:120],
        "reason": str(reason or "")[:2000],
        "not_uploaded": True,
        "settings": settings,
    }


def from_render_adjustment(adjustment: dict | None,
                           plan_decision: dict | None = None,
                           *, slot: int = 1) -> dict:
    """Carry the old framing-only artifact into a full profile.

    `render_adjustment` stored exactly three values. Migrating them keeps a
    project that used the revision-note framing commands looking the same after
    this change instead of silently snapping back to centre.
    """
    profile = default_profile(plan_decision, slot=slot)
    source = adjustment or {}
    for key in ("frame_zoom", "frame_focus_x", "frame_focus_y"):
        if source.get(key) is not None:
            profile[key] = source[key]
    return validate(profile, slot=slot, plan_decision=plan_decision)


def resolve_quality(profile: dict) -> dict:
    """Named preset -> real encode options. The client never sends these."""
    preset = QUALITY_PRESETS[profile["quality_preset"]]
    resolution = RESOLUTION_PRESETS[profile["resolution_preset"]]
    scale = preset["scale"]
    return {
        "quality_preset": profile["quality_preset"],
        "is_delivery_quality": profile["quality_preset"] == DELIVERY_QUALITY,
        "target_width": resolution["width"],
        "target_height": resolution["height"],
        # Even width/height: x264 refuses odd dimensions with yuv420p.
        "render_width": max(2, int(resolution["width"] * scale) // 2 * 2),
        "render_height": max(2, int(resolution["height"] * scale) // 2 * 2),
        "crf": preset["crf"],
        "encode_preset": preset["encode_preset"],
        # `render_utils.resolve_encode_profile` reads this to size the encode.
        # It is the SAME number `render_width`/`render_height` were derived
        # from, so the reported dimensions and the real ones cannot drift.
        "scale": scale,
    }


def effective_render_config(plan_decision: dict | None, profile: dict) -> dict:
    """The settings a render will actually use.

    Phase 0 supplies every key; the profile replaces ONLY those in
    `OVERRIDABLE_RENDER_KEYS`. The content-bearing keys pass through untouched,
    which is what keeps `check_config_drift` meaningful.
    """
    planned = dict((plan_decision or {}).get("render") or {})
    planned.update({
        "render_mode": profile["render_mode"],
        # A named quality preset still has to speak the old boolean, because
        # every render entry point downstream branches on `preview_mode`.
        "preview_mode": profile["quality_preset"] != DELIVERY_QUALITY,
        "caption_style": (CAPTION_STYLE_DYNAMIC if profile["active_word_highlight"]
                          else CAPTION_STYLE_PLAIN),
        "caption_theme": profile["caption_theme"],
    })
    return planned


def changed_fields(before: dict | None, after: dict) -> list:
    """Which settings differ. Field NAMES only -- values may be operator text."""
    old = dict(before or {})
    return sorted(key for key in default_profile()
                  if old.get(key) != after.get(key))
