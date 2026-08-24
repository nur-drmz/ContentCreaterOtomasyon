"""Phase 0 -- canonical source assessment and processing-strategy planning.

WHY THIS MODULE EXISTS
----------------------
Before this module the master notebook jumped straight from "a source video
exists" to "run analysis, then maybe script, then maybe TTS", with the actual
production strategy encoded only in a loose `PIPELINE_MODE` string. That made it
impossible to (a) tell a human WHY one strategy suits this particular source,
(b) bind that decision to the source it was made for, and (c) guarantee that a
clip-only run never touches a paid/quota-consuming API.

Phase 0 is therefore MANDATORY and produces two persistent artifacts:

    results/source_assessment.json   -- what we measured + what we RECOMMEND
    results/processing_plan.json     -- what the human actually CHOSE

Two processing modes exist. There is no third "placeholder" mode:

  direct_clip_series
      Multiple hot moments -> multiple Shorts. Original audio is preserved and
      the existing dynamic active-word caption + render pipeline is used.
      Script generation and TTS are STRUCTURALLY disabled: `resolve_phase_gates`
      denies those phases, so this mode cannot make a single Gemini/OpenAI/
      Anthropic call.

  narrated_theory
      Selected source moments become visual evidence / B-roll for a human-supplied
      theory, interpretation or analysis angle. A new script is generated through
      the EXISTING Gemini route (`google_ai` / `script_generation`), then flows
      through the existing script -> TTS -> render approval chain unchanged.

POLICY
------
- The recommendation is ADVISORY ONLY. `assess_source()` always returns
  `selected_mode=None` and `auto_selected=False`; nothing here can choose a mode.
- The plan is born `review_required` and can only become `approved` through the
  existing hash-bound flow (`approval.approve_artifact(plan, "processing_plan", ...)`).
- Everything that changes what the pipeline would DO lives in `plan["decision"]`,
  which is exactly what the approval hash covers. Changing the source, the mode,
  the moments, the theory angle, the output count, the target duration or the
  render configuration therefore invalidates the approval automatically.
- No `production_ready`, no auto-approval, no upload, no publish, no secret is
  ever read, printed or stored here.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .approval import check_approval, sha256_canonical_json
from .narration_presets import (
    DEFAULT_NARRATION_PRESET,
    NARRATION_PRESETS,
    resolve_narration_preset,
)

STATUS_VALUES = {
    "experimental", "development_placeholder", "review_required",
    "approved", "insufficient_data", "failed", "production_ready",
}

MODE_DIRECT_CLIP_SERIES = "direct_clip_series"
MODE_NARRATED_THEORY = "narrated_theory"
PROCESSING_MODES = (MODE_DIRECT_CLIP_SERIES, MODE_NARRATED_THEORY)

#: Phase 0 decides which downstream phases may run at all.
#:
#: `transcript` is the ORIGINAL audio written down for review -- no model writes
#: it and no provider is called. It exists only where the caption comes from the
#: source itself; a narrated plan draws its captions from the script it already
#: approved, so transcribing there would review the wrong text.
GATED_PHASES = ("video_analysis", "moment_selection", "transcript",
                "script_generation", "tts", "render")

#: Mode -> the render pipeline this project already implements. Phase 0 does not
#: invent a new render path; it selects between the two that exist.
MODE_PIPELINE_MODE = {
    MODE_DIRECT_CLIP_SERIES: "original_audio_subtitles",
    MODE_NARRATED_THEORY: "narrated_recap",
}

#: Phases each mode is allowed to run, and WHY the others are denied.
MODE_PHASE_POLICY = {
    MODE_DIRECT_CLIP_SERIES: {
        "video_analysis": (True, ""),
        "moment_selection": (True, ""),
        # The altyazi step of this mode: the source's OWN words, transcribed
        # locally and read by a human before they are burned in.
        "transcript": (True, ""),
        "script_generation": (
            False,
            "mod=direct_clip_series: altyazi orijinal sesin transkriptinden gelir "
            "(Transkript fazi), model yeni metin YAZMAZ; hicbir script API cagrisi yapilmaz",
        ),
        "tts": (
            False,
            "mod=direct_clip_series: orijinal ses korunur, sentetik seslendirme "
            "uretilmez; hicbir TTS API cagrisi yapilmaz",
        ),
        "render": (True, ""),
    },
    MODE_NARRATED_THEORY: {
        "video_analysis": (True, ""),
        "moment_selection": (True, ""),
        "transcript": (
            False,
            "mod=narrated_theory: altyazi onaylanan anlatim scriptinden uretilir, "
            "kaynak sesin transkriptinden degil",
        ),
        "script_generation": (True, ""),
        "tts": (True, ""),
        "render": (True, ""),
    },
}

DEFAULT_TARGET_DURATION_SEC = 60
MAX_OUTPUT_COUNT = 10
MIN_THEORY_ANGLE_CHARS = 20

#: Fingerprint sampling window. A full-file digest of a multi-GB Colab/Drive
#: source would dominate Phase 0's runtime, so the fingerprint is taken over
#: (name, size, head, tail). That still changes whenever the source file is
#: swapped, re-encoded, trimmed or re-uploaded, which is all the approval gate
#: needs to detect.
FINGERPRINT_EDGE_BYTES = 1024 * 1024


# ---------------------------------------------------------------------------
# Source fingerprint + probing
# ---------------------------------------------------------------------------

def source_fingerprint_sha256(path, edge_bytes: int = FINGERPRINT_EDGE_BYTES) -> str:
    """Deterministic fingerprint of a source video file.

    Digest input is `basename|size|<head bytes>|<tail bytes>`. Deterministic for
    the same file and different for a different/edited one -- see
    FINGERPRINT_EDGE_BYTES for why this is not a full-file hash.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"kaynak video bulunamadi: {path}")
    size = path.stat().st_size
    digest = hashlib.sha256()
    digest.update(f"{path.name}|{size}|".encode("utf-8"))
    with open(path, "rb") as handle:
        digest.update(handle.read(edge_bytes))
        if size > edge_bytes:
            handle.seek(max(0, size - edge_bytes))
            digest.update(handle.read(edge_bytes))
    return digest.hexdigest()


def probe_source(ffmpeg_bin, ffprobe_bin, path, probe_fn=None) -> dict:
    """Reads duration/resolution/audio presence for the source video.

    `probe_fn(ffmpeg_bin, ffprobe_bin, path) -> (info, backend)` is injected so
    tests can exercise the scoring logic without a media file; by default it is
    `render_utils.probe_media`, which already falls back from ffprobe to
    `ffmpeg -i` parsing. A probe failure is NOT fatal: it produces
    `probe_ok=False` and the assessment says so, instead of guessing.
    """
    path = Path(path)
    record = {
        "path": str(path),
        "basename": path.name,
        "size_bytes": path.stat().st_size if path.is_file() else None,
        "duration_sec": None,
        "width": None,
        "height": None,
        "has_video": False,
        "has_audio": False,
        "probe_backend": None,
        "probe_ok": False,
        "probe_error": None,
    }
    if probe_fn is None:
        from .render_utils import probe_media as probe_fn  # lazy: keeps tests SDK-free
    try:
        info, backend = probe_fn(ffmpeg_bin, ffprobe_bin, path)
    except Exception as exc:
        record["probe_error"] = f"{type(exc).__name__}: {exc}"
        return record

    record["probe_backend"] = backend
    record["probe_ok"] = True
    streams = (info or {}).get("streams") or []
    for stream in streams:
        codec_type = stream.get("codec_type")
        if codec_type == "video" and not record["has_video"]:
            record["has_video"] = True
            record["width"] = stream.get("width")
            record["height"] = stream.get("height")
        elif codec_type == "audio":
            record["has_audio"] = True
    raw_duration = ((info or {}).get("format") or {}).get("duration")
    try:
        record["duration_sec"] = round(float(raw_duration), 3) if raw_duration else None
    except (TypeError, ValueError):
        record["duration_sec"] = None
    return record


def summarize_moment_signals(hot_moments_data) -> dict:
    """Extracts the local signals Phase 0 scores on, from an existing analysis.

    Phase 0 deliberately runs BEFORE Phase 1 is required, so `hot_moments_data`
    may legitimately be None. In that case `available=False` and both mode scores
    fall back to source-level signals only -- they are never fabricated.
    """
    signals = {
        "available": False,
        "moment_count": 0,
        "top_score": None,
        "mean_score": None,
        "speech_chars": 0,
        "speech_density_cps": None,
        "hot_moments_sha256": None,
        "note": "hot_moments.json yok; skorlar yalnizca kaynak sinyallerine dayanir",
    }
    if not isinstance(hot_moments_data, dict):
        return signals
    moments = hot_moments_data.get("hot_moments")
    if not isinstance(moments, list) or not moments:
        signals["note"] = "hot_moments.json bos; skorlar yalnizca kaynak sinyallerine dayanir"
        return signals

    scores = [m.get("score") for m in moments if isinstance(m.get("score"), (int, float))]
    speech_chars = sum(len(str(m.get("transcript") or "")) for m in moments)
    covered_sec = 0.0
    for moment in moments:
        start = moment.get("source_start_sec", moment.get("aligned_start_sec"))
        end = moment.get("source_end_sec", moment.get("aligned_end_sec"))
        try:
            covered_sec += max(0.0, float(end) - float(start))
        except (TypeError, ValueError):
            continue

    signals.update({
        "available": True,
        "moment_count": len(moments),
        "top_score": round(max(scores), 4) if scores else None,
        "mean_score": round(sum(scores) / len(scores), 4) if scores else None,
        "speech_chars": speech_chars,
        "speech_density_cps": round(speech_chars / covered_sec, 3) if covered_sec > 0 else None,
        "hot_moments_sha256": sha256_canonical_json(hot_moments_data),
        "note": "",
    })
    return signals


# ---------------------------------------------------------------------------
# Deterministic suitability scoring
# ---------------------------------------------------------------------------

def _score_direct_clip_series(source: dict, signals: dict) -> dict:
    """Clip-series suitability. Favors: real audio, enough runtime, several
    strong moments, dialogue-dense speech (captions have something to show)."""
    score, reasons, blockers = 50, [], []

    if source.get("has_audio"):
        score += 25
        reasons.append("kaynakta ses akisi var; orijinal ses korunabilir (+25)")
    else:
        blockers.append("kaynakta ses akisi YOK; orijinal-ses klip serisi uretilemez")
        reasons.append("ses akisi yok (-)")

    duration = source.get("duration_sec") or 0
    if duration >= 180:
        score += 15
        reasons.append(f"sure {duration:.0f}s >= 180s; birden fazla klip cikarilabilir (+15)")
    elif duration >= 60:
        score += 10
        reasons.append(f"sure {duration:.0f}s >= 60s; en az bir klip cikarilabilir (+10)")
    elif duration > 0:
        reasons.append(f"sure {duration:.0f}s kisa; klip serisi icin sinirli malzeme (+0)")
    else:
        reasons.append("sure olculemedi (+0)")

    if signals.get("available"):
        count = signals.get("moment_count", 0)
        if count >= 4:
            score += 15
            reasons.append(f"{count} aday moment; seri icin bol secenek (+15)")
        elif count >= 2:
            score += 10
            reasons.append(f"{count} aday moment; kisa bir seri mumkun (+10)")
        else:
            reasons.append(f"{count} aday moment; seri yerine tek klip (+0)")
        density = signals.get("speech_density_cps")
        if density is not None and density >= 6.0:
            score += 5
            reasons.append(f"konusma yogunlugu {density:.1f} kar/sn; dinamik altyazi guclu (+5)")
    else:
        reasons.append("moment sinyali yok; Faz 1 sonrasi Faz 0'i tekrar calistirmak skoru netlestirir")

    return {
        "score": max(0, min(100, score)),
        "reasons": reasons,
        "blockers": blockers,
        "feasible": not blockers,
        "consumes_generation_api": False,
    }


def _score_narrated_theory(source: dict, signals: dict, google_credential_configured: bool) -> dict:
    """Narrated-theory suitability. Favors: long sources with material to
    interpret, and REQUIRES a configured Gemini credential -- without it the mode
    is not feasible, and saying otherwise would be inventing a capability."""
    score, reasons, blockers = 40, [], []

    duration = source.get("duration_sec") or 0
    if duration >= 600:
        score += 20
        reasons.append(f"sure {duration:.0f}s >= 600s; teori icin genis malzeme (+20)")
    elif duration >= 120:
        score += 10
        reasons.append(f"sure {duration:.0f}s >= 120s; teori icin yeterli malzeme (+10)")
    else:
        reasons.append(f"sure {duration:.0f}s kisa; teori icin sinirli gorsel kanit (+0)")

    if signals.get("available") and signals.get("moment_count", 0) >= 1:
        score += 10
        reasons.append(f"{signals['moment_count']} moment B-roll kaniti olarak kullanilabilir (+10)")
        density = signals.get("speech_density_cps")
        if density is not None and density < 6.0:
            score += 5
            reasons.append(f"konusma yogunlugu dusuk ({density:.1f} kar/sn); anlatim deger katar (+5)")
        if signals.get("speech_chars", 0) > 0:
            score += 5
            reasons.append("transcript mevcut; teori promptu kanita baglanabilir (+5)")
    else:
        reasons.append("moment sinyali yok; gorsel kanit secimi Faz 1'den sonra netlesir")

    if google_credential_configured:
        score += 15
        reasons.append("Gemini kimlik bilgisi yapilandirilmis; script rotasi kullanilabilir (+15)")
    else:
        blockers.append(
            "Gemini kimlik bilgisi yok (GeminiKey / GOOGLE_API_KEY); script uretimi yapilamaz"
        )
        reasons.append("Gemini kimlik bilgisi yok (-)")

    if not source.get("has_video"):
        blockers.append("kaynakta video akisi yok; B-roll uretilemez")

    return {
        "score": max(0, min(100, score)),
        "reasons": reasons,
        "blockers": blockers,
        "feasible": not blockers,
        "consumes_generation_api": True,
    }


def _suggest_output_count(source: dict, signals: dict) -> int:
    """Advisory clip count: one Short per ~90s of source, capped by the number of
    candidate moments actually available."""
    duration = source.get("duration_sec") or 0
    by_duration = max(1, int(duration // 90))
    if signals.get("available"):
        by_moments = max(1, int(signals.get("moment_count") or 1))
        return max(1, min(MAX_OUTPUT_COUNT, by_duration, by_moments))
    return max(1, min(MAX_OUTPUT_COUNT, by_duration))


def assess_source(source_video_path, ffmpeg_bin=None, ffprobe_bin=None, *,
                  hot_moments_data=None, google_credential_configured: bool = False,
                  probe_fn=None, assessed_at: str | None = None) -> dict:
    """Builds `source_assessment.json`.

    Measures the source, scores BOTH modes with the deterministic heuristics
    above, and records a recommendation. The recommendation is advisory: this
    function can never select or approve a mode -- `selected_mode` is always None
    and `auto_selected` is always False.
    """
    source = probe_source(ffmpeg_bin, ffprobe_bin, source_video_path, probe_fn=probe_fn)
    source["fingerprint_sha256"] = source_fingerprint_sha256(source_video_path)
    signals = summarize_moment_signals(hot_moments_data)

    scores = {
        MODE_DIRECT_CLIP_SERIES: _score_direct_clip_series(source, signals),
        MODE_NARRATED_THEORY: _score_narrated_theory(source, signals,
                                                      google_credential_configured),
    }

    feasible = [mode for mode in PROCESSING_MODES if scores[mode]["feasible"]]
    recommendation_reasons = []
    if feasible:
        # Deterministic tie-break: the mode that consumes NO generation API wins,
        # because the cheaper option should never lose a coin flip.
        recommended = max(
            feasible,
            key=lambda mode: (scores[mode]["score"], mode == MODE_DIRECT_CLIP_SERIES),
        )
        recommendation_reasons.append(
            f"{recommended} skoru {scores[recommended]['score']}/100 ile uygulanabilir modlar arasinda en yuksek"
        )
        for mode in PROCESSING_MODES:
            if mode != recommended and not scores[mode]["feasible"]:
                recommendation_reasons.append(
                    f"{mode} su an uygulanabilir degil: {'; '.join(scores[mode]['blockers'])}"
                )
    else:
        recommended = None
        recommendation_reasons.append(
            "hicbir mod su an uygulanabilir degil; engelleri gider ve Faz 0'i tekrar calistir"
        )

    return {
        "schema_version": 1,
        "artifact_type": "source_assessment",
        "status": "review_required",
        "status_reason": (
            "kaynak degerlendirildi ve iki mod icin yerel uygunluk skorlari uretildi; "
            "MOD SECIMI INSAN KARARIDIR (Faz 0a)"
        ),
        "assessed_at": assessed_at or datetime.now(timezone.utc).isoformat(),
        "source": source,
        "moment_signals": signals,
        "mode_scores": scores,
        "recommended_mode": recommended,
        "recommendation_reasons": recommendation_reasons,
        # The three flags below are the machine-readable promise that Phase 0
        # never decides for the user.
        "recommendation_is_advisory": True,
        "auto_selected": False,
        "selected_mode": None,
        "suggested_output_count": _suggest_output_count(source, signals),
        "suggested_target_duration_sec": DEFAULT_TARGET_DURATION_SEC,
        "not_uploaded": True,
    }


def assessment_content_sha256(assessment: dict) -> str:
    """Binds a plan to the exact assessment it was made from."""
    subset = {
        "source": (assessment or {}).get("source"),
        "moment_signals": (assessment or {}).get("moment_signals"),
        "mode_scores": (assessment or {}).get("mode_scores"),
    }
    return sha256_canonical_json(subset)


# ---------------------------------------------------------------------------
# Processing plan
# ---------------------------------------------------------------------------

def resolve_phase_gates(mode: str) -> dict:
    """Which downstream phases this mode may run, and the reason for each denial.

    This is the single source of truth for "direct_clip_series makes zero
    generation-API calls": the gate denies the phases, the notebook never builds
    a client, and the regression suite asserts the call count is 0.
    """
    if mode not in MODE_PHASE_POLICY:
        raise ValueError(f"bilinmeyen isleme modu: {mode!r} (beklenen: {list(PROCESSING_MODES)})")
    policy = MODE_PHASE_POLICY[mode]
    return {
        phase: {"allowed": policy[phase][0], "reason": policy[phase][1]}
        for phase in GATED_PHASES
    }


def build_output_slots(mode: str, output_count: int, moment_ranks, render_root) -> list:
    """Isolated output slot per Short.

    Slot 1 ALWAYS keeps the historical `render/` path -- including in multi-output
    plans -- so a project's first Short, its manifest, its subtitles and every
    existing test path stay exactly where they have always been. Slots 2..N each
    get their own `render/output_NN/` directory, so one Short can never overwrite
    another's video, subtitles or manifest.

    Slot 1 was previously mapped to `output_01/` whenever `output_count > 1`.
    Nothing was ever rendered there (the engine only ever wrote the historical
    path), and `outputs` sits OUTSIDE the hashed `decision` block, so changing
    this mapping orphans no artifact and invalidates no approval.
    """
    render_root = Path(render_root)
    ranks = list(moment_ranks or [])
    slots = []
    for index in range(int(output_count)):
        directory = render_root if index == 0 else render_root / f"output_{index + 1:02d}"
        slots.append({
            "index": index + 1,
            "label": f"{mode}_{index + 1:02d}",
            "moment_rank": ranks[index] if index < len(ranks) else None,
            "output_dir": str(directory),
            "render_manifest_path": str(Path(directory) / "render_manifest.json"),
            "final_video_path": str(Path(directory) / "final_short.mp4"),
        })
    return slots


#: Content languages a narrated plan may choose. Deliberately a CLOSED set: the
#: value flows into the script prompt, the TTS `language_code` and the voice
#: review checklist, so a free-form string would silently produce a script in
#: one language, narration in another, and a checklist asking about a third.
CONTENT_LANGUAGES = {
    "en-US": {"code": "en-US", "label": "English (US)", "short": "en",
              "prompt_name": "English",
              "prompt_rule": "Write the script and the hook in natural, idiomatic "
                             "American English. Do not use any other language."},
    "tr-TR": {"code": "tr-TR", "label": "Turkce", "short": "tr",
              "prompt_name": "Turkish",
              "prompt_rule": "Scripti ve hook'u dogal, akici Turkce yaz. "
                             "Baska bir dil kullanma."},
}

#: What a narrated plan gets when the caller does not choose. Explicit rather
#: than implicit: the console REQUIRES a choice, this is only the API-level
#: default for callers that predate the field.
DEFAULT_CONTENT_LANGUAGE = "en-US"

# Defaults are part of render semantics even for processing plans created
# before these two fields existed. Normalising them in drift checks preserves
# backward compatibility without allowing a quality setting to change silently.
RENDER_CONFIG_DEFAULTS = {
    "subtitle_whisper_model_size": "small",
    "subtitle_language": "auto",
}


def resolve_content_language(value, mode: str):
    """Validated language for this mode, or None for modes that have no narration.

    `direct_clip_series` keeps the original audio, so it has no content language
    at all -- returning None keeps that decision block byte-identical to what it
    was before this field existed, and therefore keeps its approval hash stable.
    """
    if mode != MODE_NARRATED_THEORY:
        return None
    code = str(value or DEFAULT_CONTENT_LANGUAGE).strip()
    if code not in CONTENT_LANGUAGES:
        raise ValueError(
            f"desteklenmeyen icerik dili: {code!r} "
            f"(beklenen: {sorted(CONTENT_LANGUAGES)})")
    return code


def build_processing_plan(assessment: dict, selected_mode: str, selected_by: str, *,
                          output_count: int | None = None,
                          target_duration_sec: int | None = None,
                          moment_ranks=None, theory_angle: str = "",
                          theory_angle_auto: bool = False,
                          content_language: str | None = None,
                          narration_preset: str | None = None,
                          render_config: dict | None = None,
                          narration_config: dict | None = None,
                          render_root="results/render", notes: str = "",
                          selected_at: str | None = None) -> dict:
    """Builds `processing_plan.json` from an EXPLICIT human choice.

    `selected_mode` and `selected_by` are mandatory and have no defaults --
    there is deliberately no way to call this function and get a mode "picked
    for you". The returned plan is always `review_required`; only
    `approval.approve_artifact(plan, "processing_plan", <hash>, <who>)` can
    promote it.
    """
    if not isinstance(assessment, dict) or assessment.get("artifact_type") != "source_assessment":
        raise ValueError("gecerli bir source_assessment artefakti gerekli")
    if selected_mode not in PROCESSING_MODES:
        raise ValueError(
            f"gecersiz isleme modu: {selected_mode!r} (beklenen: {list(PROCESSING_MODES)}); "
            "yer tutucu/otomatik mod yoktur"
        )
    reviewer = str(selected_by or "").strip()
    if not reviewer:
        raise ValueError("PLAN_SELECTED_BY bos olamaz; mod secimi insan kararidir")

    mode_score = (assessment.get("mode_scores") or {}).get(selected_mode) or {}
    if mode_score.get("blockers"):
        raise ValueError(
            f"{selected_mode} su an uygulanabilir degil: {'; '.join(mode_score['blockers'])}"
        )

    resolved_count = int(output_count or assessment.get("suggested_output_count") or 1)
    if not 1 <= resolved_count <= MAX_OUTPUT_COUNT:
        raise ValueError(f"output_count 1-{MAX_OUTPUT_COUNT} araliginda olmali: {resolved_count}")
    resolved_duration = int(
        target_duration_sec or assessment.get("suggested_target_duration_sec")
        or DEFAULT_TARGET_DURATION_SEC
    )
    if not 5 <= resolved_duration <= 180:
        raise ValueError(f"target_duration_sec 5-180 saniye araliginda olmali: {resolved_duration}")

    ranks = sorted({int(rank) for rank in (moment_ranks or [])})
    if len(ranks) > resolved_count:
        raise ValueError(
            f"{len(ranks)} moment secildi ama output_count={resolved_count}; "
            "sayilari esitle ya da moment listesini bosalt (Faz 1 en iyi adaylari kullanilir)"
        )

    angle = str(theory_angle or "").strip()
    auto_angle = bool(theory_angle_auto)
    if selected_mode == MODE_NARRATED_THEORY:
        # The plan is approved BEFORE the analysis runs, so at this point nobody
        # -- human or model -- has seen the moment the theory will be about.
        # `theory_angle_auto` defers the angle to script generation, where the
        # moment's own transcript is finally available. It is an explicit flag,
        # not an empty string or a magic value, so "I have not decided yet" can
        # never be confused with "I forgot to type it".
        if auto_angle and angle:
            raise ValueError(
                "teori acisi ya elle yazilir ya da otomatik turetilir; ikisi birden olmaz")
        if not auto_angle and len(angle) < MIN_THEORY_ANGLE_CHARS:
            raise ValueError(
                f"narrated_theory icin teori/yorum acisi en az {MIN_THEORY_ANGLE_CHARS} karakter "
                "olmali; script bu aciya gore uretilir (veya otomatik turetmeyi sec)"
            )
        if resolved_count != 1:
            raise ValueError(
                "narrated_theory su an tek anlatimli cikti uretir; output_count=1 olmali"
            )
    else:
        if angle:
            raise ValueError(
                "direct_clip_series modunda teori acisi kullanilmaz; script/TTS cagrisi yapilmaz"
            )
        angle = ""

    # Only decision-relevant fields go in `decision`: this dict IS the approval
    # hash input (see approval.content_hash_for / PROCESSING_PLAN_DECISION_KEY).
    source = assessment.get("source") or {}
    decision = {
        "mode": selected_mode,
        "source_fingerprint_sha256": source.get("fingerprint_sha256"),
        "source_basename": source.get("basename"),
        "output_count": resolved_count,
        "target_duration_sec": resolved_duration,
        "moment_ranks": ranks,
        "theory_angle": angle or None,
        "theory_angle_auto": auto_angle,
        # Part of the DECISION, so changing it changes the plan's approval hash
        # and invalidates every downstream approval -- which is exactly right:
        # a script approved in one language does not authorise another.
        "content_language": resolve_content_language(content_language, selected_mode),
        "render": dict(render_config or {}),
        # A resolved preset (voice + delivery direction), not free-form config.
        # Inside the DECISION, so switching preset changes the plan hash and
        # invalidates the voiceover/render approved for the previous delivery.
        "narration": (
            resolve_narration_preset(
                (narration_config or {}).get("preset") or narration_preset
                or DEFAULT_NARRATION_PRESET,
                language_code=resolve_content_language(content_language, selected_mode))
            if selected_mode == MODE_NARRATED_THEORY else None
        ),
    }

    gates = resolve_phase_gates(selected_mode)
    return {
        "schema_version": 1,
        "artifact_type": "processing_plan",
        "status": "review_required",
        "status_reason": (
            "isleme plani olusturuldu; INSAN INCELEMESI BEKLIYOR (onay ayri adimda, "
            "approval.approve_artifact ile verilir)"
        ),
        "decision": decision,
        "pipeline_mode": MODE_PIPELINE_MODE[selected_mode],
        "phase_gates": gates,
        "enabled_phases": [p for p, g in gates.items() if g["allowed"]],
        "disabled_phases": [p for p, g in gates.items() if not g["allowed"]],
        "consumes_generation_api": selected_mode == MODE_NARRATED_THEORY,
        "outputs": build_output_slots(selected_mode, resolved_count, ranks, render_root),
        "selected_by": reviewer,
        "selected_at": selected_at or datetime.now(timezone.utc).isoformat(),
        "notes": str(notes or "").strip() or None,
        "recommended_mode": assessment.get("recommended_mode"),
        "followed_recommendation": selected_mode == assessment.get("recommended_mode"),
        "mode_score_at_selection": mode_score.get("score"),
        "source_assessment_sha256": assessment_content_sha256(assessment),
        "human_approval": {
            "approved": False, "approved_by": None, "approved_at": None,
            "notes": None, "approved_content_sha256": None,
        },
        "not_uploaded": True,
    }


def validate_processing_plan(plan, assessment=None, source_fingerprint: str | None = None,
                             hot_moments_data=None) -> list:
    """Reasons a persisted plan is malformed, stale, or unsafe to act on.

    Returns [] when the plan is structurally sound AND still matches the source /
    assessment it was made for. This is the check that catches "the plan was
    approved yesterday, then the source video was replaced".
    """
    errors: list[str] = []
    if not isinstance(plan, dict):
        return ["processing_plan nesnesi sozluk degil"]
    if plan.get("artifact_type") != "processing_plan":
        errors.append("artifact_type 'processing_plan' degil")
    for field in ("schema_version", "status", "decision", "pipeline_mode",
                  "phase_gates", "outputs", "selected_by", "human_approval"):
        if field not in plan:
            errors.append(f"zorunlu alan eksik: {field}")
    if plan.get("status") == "production_ready":
        errors.append("processing_plan asla production_ready olamaz")

    decision = plan.get("decision")
    if not isinstance(decision, dict) or not decision:
        errors.append("karar blogu (decision) yok")
        return errors

    mode = decision.get("mode")
    if mode not in PROCESSING_MODES:
        errors.append(f"bilinmeyen isleme modu: {mode!r}")
    else:
        if plan.get("pipeline_mode") != MODE_PIPELINE_MODE[mode]:
            errors.append(
                f"pipeline_mode ({plan.get('pipeline_mode')!r}) mod ile tutarsiz ({mode})"
            )
        expected_gates = resolve_phase_gates(mode)
        if plan.get("phase_gates") != expected_gates:
            errors.append("phase_gates mod politikasiyla eslesmiyor (plan elle degistirilmis olabilir)")
        if mode == MODE_NARRATED_THEORY:
            language = decision.get("content_language")
            if not language:
                errors.append("narrated_theory icin content_language secilmeli")
            elif language not in CONTENT_LANGUAGES:
                errors.append(f"desteklenmeyen icerik dili: {language!r}")
        if mode == MODE_DIRECT_CLIP_SERIES and decision.get("content_language"):
            errors.append("direct_clip_series icerik dili tasimaz "
                          "(orijinal ses korunur)")
        if mode == MODE_NARRATED_THEORY:
            narration = decision.get("narration") or {}
            preset = narration.get("preset")
            if preset not in NARRATION_PRESETS:
                errors.append(f"bilinmeyen anlatim on ayari: {preset!r}")
            elif narration.get("voice") != NARRATION_PRESETS[preset]["voice"]:
                errors.append(
                    f"anlatim sesi on ayarla uyusmuyor: {narration.get('voice')!r} "
                    f"!= {NARRATION_PRESETS[preset]['voice']!r}")
        if (mode == MODE_NARRATED_THEORY and not decision.get("theory_angle")
                and not decision.get("theory_angle_auto")):
            errors.append("narrated_theory plani teori acisi icermiyor")
        if mode == MODE_DIRECT_CLIP_SERIES and decision.get("theory_angle"):
            errors.append("direct_clip_series plani teori acisi tasiyamaz")

    outputs = plan.get("outputs")
    if not isinstance(outputs, list) or len(outputs) != int(decision.get("output_count") or 0):
        errors.append("outputs listesi output_count ile eslesmiyor")
    elif len({slot.get("output_dir") for slot in outputs}) != len(outputs):
        errors.append("cikti dizinleri benzersiz degil; ciktilar birbirini ezebilir")

    if source_fingerprint and decision.get("source_fingerprint_sha256") != source_fingerprint:
        errors.append(
            "kaynak video plandan sonra degismis (fingerprint uyusmuyor); "
            "Faz 0'i tekrar calistirip plani yenile"
        )
    if assessment is not None:
        current = assessment_content_sha256(assessment)
        if plan.get("source_assessment_sha256") != current:
            errors.append(
                "kaynak degerlendirmesi plandan sonra degismis; Faz 0'i tekrar calistir"
            )
    if hot_moments_data is not None and decision.get("moment_ranks"):
        available = {
            int(m.get("rank")) for m in (hot_moments_data.get("hot_moments") or [])
            if isinstance(m.get("rank"), int)
        }
        unknown = [rank for rank in decision["moment_ranks"] if rank not in available]
        if unknown:
            errors.append(f"planda olmayan moment ranklari: {unknown}")
    return errors


#: Render keys that change WHICH WORDS end up on screen. A render profile may
#: never override these -- doing so would produce subtitles the human approved
#: the plan for but never actually read.
PLAN_BOUND_RENDER_KEYS = ("subtitle_language", "subtitle_whisper_model_size")

#: Render keys that change only how the same content LOOKS. Render Studio may
#: override these per output slot, because re-approving Phase 0 to switch a
#: caption theme was the friction that made the console read-only after the
#: first render.
PRESENTATION_RENDER_KEYS = ("caption_style", "caption_theme", "preview_mode",
                            "render_mode")


def check_config_drift(plan, render_config=None, narration_config=None,
                       *, presentation_overridden: bool = False) -> list:
    """Reasons the LIVE configuration no longer matches the approved plan.

    The render/narration settings are part of `decision`, so editing them after
    approval already changes the approval hash. But the notebook defines those
    cells BELOW Phase 0, so a user can legitimately approve a plan and then edit
    `RENDER_MODE` further down. This check catches exactly that case and stops
    the render instead of quietly producing something the human never approved.

    `presentation_overridden` says a render profile deliberately supplied the
    keys in `PRESENTATION_RENDER_KEYS`, so a difference there is a recorded
    choice rather than drift. It exempts ONLY that fixed list: a key nobody
    listed still raises, so adding a new render setting fails closed instead of
    silently becoming overridable.
    """
    errors: list[str] = []
    if not isinstance(plan, dict):
        return ["isleme plani yok"]
    decision = plan.get("decision") or {}
    for label, live, planned in (
        ("render", render_config, decision.get("render")),
        ("narration", narration_config, decision.get("narration")),
    ):
        if live is None:
            continue
        planned = planned or {}
        if label == "render":
            planned = {**RENDER_CONFIG_DEFAULTS, **planned}
            live = {**RENDER_CONFIG_DEFAULTS, **live}
        keys = sorted(set(live) | set(planned))
        if label == "render" and presentation_overridden:
            keys = [key for key in keys if key not in PRESENTATION_RENDER_KEYS]
        for key in keys:
            if live.get(key) != planned.get(key):
                errors.append(
                    f"{label}.{key}: plan={planned.get(key)!r} ama su anki ayar={live.get(key)!r}; "
                    "Faz 0a'yi tekrar calistirip plani yenile ve yeniden onayla"
                )
    return errors


def plan_allows_phase(plan, phase: str, *, require_approved: bool = True) -> tuple:
    """(allowed, reason) for one downstream phase.

    A missing plan, an unapproved plan, or a plan whose approval no longer binds
    its current decision all deny the phase. Phase 0 is mandatory precisely
    because this returns False until a human has approved a plan.
    """
    if phase not in GATED_PHASES:
        raise ValueError(f"bilinmeyen faz: {phase!r} (beklenen: {list(GATED_PHASES)})")
    if not isinstance(plan, dict):
        return False, (
            "Faz 0 isleme plani yok. Faz 0 -> Faz 0a -> Faz 0b hucrelerini calistirip "
            "processing_plan.json uret ve onayla."
        )
    errors = validate_processing_plan(plan)
    if errors:
        return False, f"isleme plani gecersiz: {errors}"
    if require_approved:
        if plan.get("status") != "approved":
            return False, (
                f"isleme plani onaylanmamis (status={plan.get('status')!r}); "
                "Faz 0b onay hucresini calistir"
            )
        # status='approved' is NOT sufficient. The recorded approval must still
        # BIND the plan's current decision -- otherwise editing the decision after
        # approval (or hand-editing the file) would keep every phase open with an
        # approval nobody gave for this content.
        binding = check_approval(plan, "processing_plan")
        if not binding["valid"]:
            return False, (
                f"isleme plani onayi artik gecerli degil: {binding['reason']} "
                "Faz 0a'yi tekrar calistirip plani yenile ve Faz 0c'de yeniden onayla."
            )
    gate = (plan.get("phase_gates") or {}).get(phase) or {}
    if not gate.get("allowed"):
        return False, gate.get("reason") or f"{phase} bu isleme modunda calistirilmaz"
    return True, ""


def describe_phase_plan(plan) -> dict:
    """Sanitized per-phase allow/deny map for the notebook's execution summary."""
    return {
        phase: dict(zip(("allowed", "reason"), plan_allows_phase(plan, phase)))
        for phase in GATED_PHASES
    }


# ---------------------------------------------------------------------------
# Human-facing panels (no secret, no auto-selection)
# ---------------------------------------------------------------------------

def _fmt(value, suffix=""):
    return "-" if value in (None, "") else f"{value}{suffix}"


def format_assessment_panel(assessment: dict) -> str:
    """The Phase 0 panel: source facts, both mode scores, reasons, suggested
    output count/duration, and mode-specific options -- with an explicit
    reminder that nothing has been selected yet."""
    source = assessment.get("source") or {}
    signals = assessment.get("moment_signals") or {}
    lines = [
        "=" * 78,
        "FAZ 0 -- KAYNAK ISLEME STRATEJISI (degerlendirme)",
        "=" * 78,
        f"Kaynak dosya      : {_fmt(source.get('basename'))}",
        f"Sure / boyut      : {_fmt(source.get('duration_sec'), ' sn')} / "
        f"{_fmt(round((source.get('size_bytes') or 0) / 1024 ** 2, 1), ' MB')}",
        f"Cozunurluk        : {_fmt(source.get('width'))}x{_fmt(source.get('height'))}"
        f"  | video={source.get('has_video')} ses={source.get('has_audio')}",
        f"Probe             : {_fmt(source.get('probe_backend'))} (ok={source.get('probe_ok')})",
        f"Kaynak parmak izi : {_fmt((source.get('fingerprint_sha256') or '')[:16])}...",
        f"Moment sinyalleri : {'var' if signals.get('available') else 'YOK'}"
        f" | aday={signals.get('moment_count')} en_iyi_skor={_fmt(signals.get('top_score'))}"
        f" konusma={_fmt(signals.get('speech_density_cps'), ' kar/sn')}",
    ]
    if signals.get("note"):
        lines.append(f"  NOT: {signals['note']}")
    lines.append("")

    for mode in PROCESSING_MODES:
        score = (assessment.get("mode_scores") or {}).get(mode) or {}
        flag = "UYGULANABILIR" if score.get("feasible") else "ENGELLI"
        api = "API cagrisi YAPAR" if score.get("consumes_generation_api") else "API cagrisi YAPMAZ"
        lines.append(f"[{mode}] skor={score.get('score')}/100  {flag}  ({api})")
        for reason in score.get("reasons", []):
            lines.append(f"    + {reason}")
        for blocker in score.get("blockers", []):
            lines.append(f"    ! ENGEL: {blocker}")
        lines.append("")

    lines += [
        f"ONERI (yalnizca tavsiye): {_fmt(assessment.get('recommended_mode'))}",
    ]
    for reason in assessment.get("recommendation_reasons", []):
        lines.append(f"    - {reason}")
    lines += [
        "",
        f"Onerilen cikti sayisi : {assessment.get('suggested_output_count')}",
        f"Onerilen hedef sure   : {assessment.get('suggested_target_duration_sec')} sn",
        "",
        "MOD SECENEKLERI:",
        "  direct_clip_series : coklu hot moment -> coklu Short, ORIJINAL SES korunur,",
        "                       mevcut dinamik aktif-kelime altyazi + render hatti kullanilir,",
        "                       script/TTS API'leri CAGRILMAZ.",
        "  narrated_theory    : secilen momentler gorsel kanit/B-roll olur, kullanicinin",
        "                       yazdigi teori/yorum acisina gore mevcut Gemini rotasindan yeni",
        "                       script uretilir, script onayi -> TTS onayi -> render onayi",
        "                       zinciri aynen isler.",
        "",
        f"SECIM YAPILMADI (selected_mode={assessment.get('selected_mode')!r}, "
        f"auto_selected={assessment.get('auto_selected')}).",
        "Oneri bir secim veya onay DEGILDIR. Faz 0a'da modu sen sec, Faz 0b'de hash ile onayla.",
        "=" * 78,
    ]
    return "\n".join(lines)


def format_plan_panel(plan: dict) -> str:
    """Post-selection panel: what will run, what will not, and why."""
    decision = plan.get("decision") or {}
    lines = [
        "=" * 78,
        "FAZ 0a -- ISLEME PLANI (henuz ONAYLANMADI)",
        "=" * 78,
        f"Secilen mod       : {decision.get('mode')}",
        f"Render hatti      : {plan.get('pipeline_mode')}",
        f"Cikti sayisi      : {decision.get('output_count')}",
        f"Hedef sure        : {decision.get('target_duration_sec')} sn",
        f"Moment ranklari   : {decision.get('moment_ranks') or '(Faz 1 en iyi adaylari)'}",
        f"Icerik dili       : {_fmt(decision.get('content_language'))}",
        f"Anlatim on ayari  : {_fmt((decision.get('narration') or {}).get('preset'))}"
        f" (ses: {_fmt((decision.get('narration') or {}).get('voice'))})",
        f"Teori acisi       : "
        + ("secilen momentten OTOMATIK turetilecek"
           if decision.get("theory_angle_auto")
           else _fmt(decision.get("theory_angle"))),
        f"Secen kisi        : {plan.get('selected_by')}",
        f"Oneriye uyuldu mu : {plan.get('followed_recommendation')} "
        f"(oneri={plan.get('recommended_mode')})",
        f"Uretim API kullanir: {plan.get('consumes_generation_api')}",
        "",
        "FAZ KAPILARI:",
    ]
    for phase, gate in (plan.get("phase_gates") or {}).items():
        mark = "ACIK " if gate.get("allowed") else "KAPALI"
        lines.append(f"  {mark} {phase}" + (f" -- {gate['reason']}" if gate.get("reason") else ""))
    lines.append("")
    lines.append("CIKTI YUVALARI (birbirini ezmez):")
    for slot in plan.get("outputs") or []:
        lines.append(f"  #{slot['index']} rank={_fmt(slot.get('moment_rank'))} -> {slot['output_dir']}")
    lines += [
        "",
        f"status={plan.get('status')} | human_approval.approved="
        f"{(plan.get('human_approval') or {}).get('approved')}",
        "Plan onaylanmadan hicbir alt faz calismaz. Faz 0b'de gosterilen SHA-256'yi kullan.",
        "=" * 78,
    ]
    return "\n".join(lines)
