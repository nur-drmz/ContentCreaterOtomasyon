"""Conservative song/lyrics suggestions for subtitle human review.

Whisper is a speech recogniser, not a dialogue-vs-singing classifier. Sung
words can therefore have excellent token confidence.  This module combines
independent evidence and only SUGGESTS exclusions; it never changes a
transcript. A person remains the authority because dialogue over a musical
score is common and must not disappear from captions.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import transcript_edit

SCHEMA_VERSION = 1
METHOD = "conservative-text-audio-v1"
PROVIDER_LABELS = ("dialogue", "sung_lyrics", "music_without_words", "uncertain")

_TOKEN_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
_EXPLICIT_MUSIC_RE = re.compile(
    r"(?:[♪♫♬]|\[(?:music|singing|song|lyrics|instrumental|müzik|şarkı|şarki)\]|"
    r"\((?:music|singing|song|lyrics|instrumental|müzik|şarkı|şarki)\))",
    re.IGNORECASE,
)
_VOCALISATIONS = frozenset({
    "ah", "aah", "oh", "ooh", "uh", "huh", "hey", "yeah", "yo",
    "la", "na", "da", "doo", "whoa", "woah", "ha",
})


class LyricsReviewError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finite(value, default=0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _normalise_text(value) -> str:
    return " ".join(token.casefold() for token in _TOKEN_RE.findall(str(value or "")))


def transcript_sha256(artifact: dict | None) -> str:
    cues = [{"start": cue.get("start"), "end": cue.get("end"),
             "text": cue.get("text"),
             "excluded": bool(cue.get("excluded_from_subtitles"))}
            for cue in ((artifact or {}).get("cues") or [])]
    payload = json.dumps(cues, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _pcm_samples(path) -> tuple[np.ndarray, int]:
    path = Path(path)
    if not path.is_file():
        raise LyricsReviewError("lyrics incelemesi icin WAV dosyasi bulunamadi")
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            rate = handle.getframerate()
            width = handle.getsampwidth()
            frames = handle.readframes(handle.getnframes())
    except (wave.Error, OSError) as exc:
        raise LyricsReviewError("lyrics incelemesi PCM WAV bekliyor") from exc
    if rate <= 0 or channels <= 0 or width not in {1, 2, 4}:
        raise LyricsReviewError("WAV ses bicimi desteklenmiyor")
    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}[width]
    samples = np.frombuffer(frames, dtype=dtype)
    if width == 1:
        mono = (samples.astype(np.float32) - 128.0) / 128.0
    else:
        mono = samples.astype(np.float32) / float(2 ** (width * 8 - 1))
    if channels > 1:
        usable = len(mono) - (len(mono) % channels)
        mono = mono[:usable].reshape(-1, channels).mean(axis=1)
    return mono, rate


def _window(samples: np.ndarray, rate: int, start_sec=None, end_sec=None) -> np.ndarray:
    start = max(0, int(_finite(start_sec) * rate)) if start_sec is not None else 0
    end = (min(len(samples), int(_finite(end_sec) * rate))
           if end_sec is not None else len(samples))
    return samples[start:max(start, end)]


def audio_music_profile(path, *, start_sec=None, end_sec=None) -> dict:
    """Extract lightweight tonal/rhythmic evidence from a PCM WAV.

    This is deliberately not labelled a music classifier. It is a reproducible
    local signal used together with text evidence and human review.
    """
    samples, rate = _pcm_samples(path)
    samples = _window(samples, rate, start_sec, end_sec)
    duration = len(samples) / float(rate)
    if len(samples) < max(512, int(rate * 0.12)):
        return {"available": False, "duration_sec": round(duration, 3),
                "reason": "ses penceresi analiz icin cok kisa"}
    peak = float(np.max(np.abs(samples))) or 1.0
    samples = samples / peak
    frame_size = min(2048, max(512, 2 ** int(math.log2(max(512, rate // 16)))))
    hop = max(128, frame_size // 2)
    if len(samples) < frame_size:
        samples = np.pad(samples, (0, frame_size - len(samples)))
    starts = range(0, len(samples) - frame_size + 1, hop)
    frames = np.stack([samples[index:index + frame_size] for index in starts])
    rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
    active_threshold = max(0.012, float(np.percentile(rms, 90)) * 0.12)
    active = rms > active_threshold
    active_ratio = float(np.mean(active))

    spectrum = np.abs(np.fft.rfft(frames * np.hanning(frame_size), axis=1)) ** 2
    spectrum[:, 0] = 0.0
    total = np.sum(spectrum, axis=1) + 1e-12
    top_bins = np.partition(spectrum, -8, axis=1)[:, -8:]
    tonal_raw = np.sum(top_bins, axis=1) / total
    tonal_raw = float(np.median(tonal_raw[active])) if np.any(active) else 0.0
    tonal_strength = float(np.clip((tonal_raw - 0.04) / 0.42, 0.0, 1.0))

    envelope = rms - float(np.mean(rms))
    norm = float(np.dot(envelope, envelope))
    pulse_regularity = 0.0
    if norm > 1e-9 and len(envelope) > 8:
        frames_per_sec = rate / float(hop)
        low_lag = max(1, int(0.25 * frames_per_sec))
        high_lag = min(len(envelope) - 2, int(1.5 * frames_per_sec))
        if high_lag >= low_lag:
            correlations = [float(np.dot(envelope[:-lag], envelope[lag:]) / norm)
                            for lag in range(low_lag, high_lag + 1)]
            pulse_regularity = float(np.clip(max(correlations or [0.0]), 0.0, 1.0))

    # A conservative evidence score, not a probability. Sustained speech alone
    # stays low; tonal and periodic energy must agree before the score is high.
    music_evidence = float(np.clip(
        0.48 * tonal_strength + 0.34 * pulse_regularity
        + 0.18 * active_ratio, 0.0, 1.0))
    return {
        "available": True, "duration_sec": round(duration, 3),
        "active_ratio": round(active_ratio, 4),
        "tonal_strength": round(tonal_strength, 4),
        "pulse_regularity": round(pulse_regularity, 4),
        "music_evidence": round(music_evidence, 4),
        "method": "pcm-spectral-envelope-v1",
    }


def _repeat_evidence(cues: list[dict]) -> dict[int, bool]:
    normalised = [_normalise_text(cue.get("text")) for cue in cues]
    counts = {text: normalised.count(text) for text in set(normalised) if text}
    return {index: bool(text and len(text.split()) >= 2 and counts.get(text, 0) >= 2)
            for index, text in enumerate(normalised)}


def _vocalisation_ratio(text) -> float:
    tokens = _normalise_text(text).split()
    return (sum(token in _VOCALISATIONS for token in tokens) / len(tokens)
            if tokens else 0.0)


def analyse(artifact: dict, *, audio_path=None) -> dict:
    """Return review suggestions without modifying subtitle visibility."""
    cues = [dict(cue) for cue in (artifact or {}).get("cues") or []]
    if not cues:
        raise LyricsReviewError("lyrics incelemesi icin altyazi segmenti yok")
    overall_audio = (audio_music_profile(audio_path) if audio_path else
                     {"available": False, "reason": "ses analizi yapilmadi"})
    repeated = _repeat_evidence(cues)
    rows = []
    for index, cue in enumerate(cues):
        identifier = transcript_edit.segment_id(index)
        text = str(cue.get("text") or "")
        reasons = []
        score = 0.0
        explicit = bool(_EXPLICIT_MUSIC_RE.search(text))
        vocal_ratio = _vocalisation_ratio(text)
        if explicit:
            score += 0.9
            reasons.append("acik muzik/sarki etiketi")
        if repeated.get(index):
            score += 0.28
            reasons.append("ayni satir tekrarlaniyor")
        if vocal_ratio >= 0.5:
            score += 0.32
            reasons.append("yogun vokalizasyon heceleri")

        cue_audio = ({"available": False, "reason": "ses analizi yapilmadi"})
        if audio_path:
            cue_audio = audio_music_profile(
                audio_path, start_sec=cue.get("start"), end_sec=cue.get("end"))
            evidence = max(_finite(overall_audio.get("music_evidence")),
                           _finite(cue_audio.get("music_evidence")))
            if evidence >= 0.72:
                score += 0.38
                reasons.append("guclu tonal/ritmik ses kaniti")
            elif evidence >= 0.52:
                score += 0.2
                reasons.append("orta tonal/ritmik ses kaniti")
        score = min(1.0, score)

        already_excluded = bool(cue.get("excluded_from_subtitles"))
        if already_excluded:
            recommendation = "already_excluded"
        elif explicit or score >= 0.7:
            recommendation = "exclude_suggested"
        elif score >= 0.38:
            recommendation = "review"
        else:
            recommendation = "dialogue_likely"
        rows.append({
            "id": identifier, "index": index,
            "start_sec": cue.get("start"), "end_sec": cue.get("end"),
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "recommendation": recommendation, "score": round(score, 4),
            "reasons": reasons, "vocalisation_ratio": round(vocal_ratio, 4),
            "audio": cue_audio,
            "manually_excluded": already_excluded,
        })
    suggested = [row["id"] for row in rows
                 if row["recommendation"] == "exclude_suggested"]
    review = [row["id"] for row in rows if row["recommendation"] == "review"]
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "subtitle_lyrics_review",
        "status": "review_required",
        "status_reason": ("oneriler otomatik uygulanmadi; insan karari gerekiyor"),
        "method": METHOD, "created_at": _now(), "not_uploaded": True,
        "transcript_sha256": transcript_sha256(artifact),
        "segment_count": len(rows), "segments": rows,
        "suggested_segment_ids": suggested,
        "review_segment_ids": review,
        "already_excluded_count": sum(row["manually_excluded"] for row in rows),
        "overall_audio": overall_audio,
        "automatic_exclusion_applied": False,
        "human_confirmation_required": True,
        "limitations": [
            "Whisper guveni sarki ile diyalogu ayiramaz.",
            "Fon muzigi altindaki gercek diyalog yanlis pozitif olabilir.",
            "Oneriler dinlenmeden topluca uygulanmamalidir.",
        ],
    }


def provider_prompt(artifact: dict) -> str:
    """Prompt an audio-capable model without copying caption/lyric text.

    Segment IDs and timestamps are enough: the model receives the audio in the
    same request. Avoiding recognised text prevents the classifier from merely
    trusting Whisper's potentially wrong words.
    """
    cues = (artifact or {}).get("cues") or []
    if not cues:
        raise LyricsReviewError("saglayici incelemesi icin segment yok")
    timeline = "\n".join(
        f"- {transcript_edit.segment_id(index)}: "
        f"{_finite(cue.get('start')):.3f}-{_finite(cue.get('end')):.3f}s"
        for index, cue in enumerate(cues))
    return (
        "Classify the audible vocal content inside each timestamped segment.\n"
        "Use exactly one label: dialogue, sung_lyrics, music_without_words, "
        "or uncertain. Dialogue spoken over background score is dialogue. "
        "Chanting or melodically sung words are sung_lyrics. Instrumental music "
        "or non-lexical background sound with no audible words is "
        "music_without_words. Do not infer from a transcript; listen to the "
        "audio. Return only the requested structured result. Never reproduce "
        "or quote lyrics.\nSegments:\n" + timeline)


def provider_response_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string", "enum": list(PROVIDER_LABELS)},
                        "confidence": {"type": "number", "minimum": 0,
                                       "maximum": 1},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "label", "confidence", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["segments"], "additionalProperties": False,
    }


def merge_provider_result(review: dict, artifact: dict, result: dict, *,
                          provider: str, model: str) -> dict:
    """Validate and merge a model classification; never apply exclusions."""
    if is_stale(review, artifact):
        raise LyricsReviewError("transkript degisti; lyrics incelemesini yeniden calistir")
    known = {row.get("id") for row in (review or {}).get("segments") or []}
    incoming = (result or {}).get("segments")
    if not isinstance(incoming, list) or not incoming:
        raise LyricsReviewError("saglayici lyrics sonucu segment icermiyor")
    parsed = {}
    for item in incoming:
        if not isinstance(item, dict):
            raise LyricsReviewError("saglayici lyrics segmenti nesne olmali")
        identifier = str(item.get("id") or "")
        label = str(item.get("label") or "")
        confidence = _finite(item.get("confidence"), -1.0)
        if identifier not in known:
            raise LyricsReviewError(f"saglayici bilinmeyen segment dondurdu: {identifier}")
        if identifier in parsed:
            raise LyricsReviewError(f"saglayici segmenti iki kez dondurdu: {identifier}")
        if label not in PROVIDER_LABELS or not 0.0 <= confidence <= 1.0:
            raise LyricsReviewError("saglayici lyrics etiketi veya guveni gecersiz")
        parsed[identifier] = {
            "label": label, "confidence": round(confidence, 4),
            "reason": str(item.get("reason") or "")[:240],
        }
    if set(parsed) != known:
        missing = sorted(known - set(parsed))
        raise LyricsReviewError(f"saglayici tum segmentleri siniflandirmadi: {missing}")

    merged = dict(review)
    rows = []
    for raw in review.get("segments") or []:
        row = dict(raw)
        assessment = parsed[row["id"]]
        row["provider_assessment"] = assessment
        # Manual exclusions stay authoritative. A confident singing result is
        # a suggestion, never an action. Instrumental/no-words is kept in review
        # because a model can miss quiet dialogue under loud score.
        if row.get("manually_excluded"):
            row["recommendation"] = "already_excluded"
        elif assessment["label"] == "sung_lyrics" and assessment["confidence"] >= 0.8:
            row["recommendation"] = "exclude_suggested"
        elif assessment["label"] == "dialogue" and assessment["confidence"] >= 0.75:
            row["recommendation"] = "dialogue_likely"
        else:
            row["recommendation"] = "review"
        rows.append(row)
    merged["segments"] = rows
    merged["suggested_segment_ids"] = [
        row["id"] for row in rows if row["recommendation"] == "exclude_suggested"]
    merged["review_segment_ids"] = [
        row["id"] for row in rows if row["recommendation"] == "review"]
    merged["provider_review"] = {
        "provider": str(provider or "")[:80], "model": str(model or "")[:120],
        "reviewed_at": _now(), "segment_count": len(rows),
        "automatic_exclusion_applied": False,
    }
    merged["automatic_exclusion_applied"] = False
    merged["human_confirmation_required"] = True
    return merged


def is_stale(review: dict | None, artifact: dict | None) -> bool:
    return bool(review) and review.get("transcript_sha256") != transcript_sha256(artifact)
