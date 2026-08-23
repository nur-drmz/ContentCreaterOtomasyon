"""Human-labelled audio examples for a future lyrics classifier.

This module is intentionally independent from the Console UI.  A label is
ground truth only when a person explicitly supplies it; heuristic/provider
suggestions are recorded as context but never promoted to labels themselves.

Raw source paths and recognised text are deliberately absent from the manifest.
The training clip is a small, mono 16 kHz PCM WAV and the split is assigned by
source group (normally the project/source video), preventing adjacent segments
from the same video leaking into both training and evaluation.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import wave
from datetime import datetime, timezone
from pathlib import Path

from . import render_utils

SCHEMA_VERSION = 1
LABELS = ("dialogue", "sung_lyrics", "music_without_words", "uncertain")
LABEL_ORIGINS = (
    "human_direct", "human_accept_suggestion", "human_reject_suggestion",
)
MIN_DURATION_SEC = 0.2
MAX_DURATION_SEC = 15.0
DEFAULT_CONTEXT_SEC = 0.5


class LyricsTrainingError(ValueError):
    """A rejected training example, phrased for the operator."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finite(value, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise LyricsTrainingError(f"{name} sayisal olmali") from exc
    if not math.isfinite(result):
        raise LyricsTrainingError(f"{name} sonlu olmali")
    return result


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _empty_manifest() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "lyrics_training_dataset",
        "labels": list(LABELS),
        "audio_format": {"container": "wav", "codec": "pcm_s16le",
                         "sample_rate_hz": 16000, "channels": 1},
        "privacy": {"stores_transcript_text": False,
                    "stores_absolute_source_path": False},
        "examples": [],
    }


def load_manifest(dataset_dir) -> dict:
    path = Path(dataset_dir) / "manifest.json"
    if not path.exists():
        return _empty_manifest()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LyricsTrainingError("lyrics egitim manifesti okunamadi") from exc
    if (not isinstance(data, dict)
            or data.get("schema_version") != SCHEMA_VERSION
            or not isinstance(data.get("examples"), list)):
        raise LyricsTrainingError("lyrics egitim manifesti desteklenmiyor")
    return data


def _split_for_group(source_group: str) -> str:
    """Stable 80/10/10 split by video/source, never by individual segment."""
    bucket = int(hashlib.sha256(source_group.encode("utf-8")).hexdigest()[:8], 16) % 100
    if bucket < 80:
        return "train"
    if bucket < 90:
        return "validation"
    return "test"


def _example_id(*, source_group: str, segment_id: str,
                transcript_sha256: str, start_sec: float, end_sec: float) -> str:
    identity = json.dumps(
        [source_group, segment_id, transcript_sha256,
         round(start_sec, 3), round(end_sec, 3)],
        ensure_ascii=False, separators=(",", ":"),
    )
    return "lyr_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _extract_clip(source_audio: Path, destination: Path, start_sec: float,
                  duration_sec: float) -> None:
    ffmpeg_bin, _ffprobe_bin = render_utils.resolve_ffmpeg_binaries()
    destination.parent.mkdir(parents=True, exist_ok=True)
    render_utils.run_ffmpeg(ffmpeg_bin, [
        "-ss", f"{start_sec:.3f}", "-t", f"{duration_sec:.3f}",
        "-i", str(source_audio), "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", str(destination),
    ])


def _validate_wav(path: Path, expected_duration: float) -> dict:
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            rate = handle.getframerate()
            width = handle.getsampwidth()
            frames = handle.getnframes()
    except (OSError, wave.Error) as exc:
        raise LyricsTrainingError("uretilen egitim klibi gecerli PCM WAV degil") from exc
    duration = frames / float(rate) if rate else 0.0
    if channels != 1 or rate != 16000 or width != 2 or duration < MIN_DURATION_SEC:
        raise LyricsTrainingError("egitim klibi mono 16 kHz PCM16 kosullarini saglamiyor")
    # Container rounding is expected, but a truncated/incorrect seek is not.
    if abs(duration - expected_duration) > max(0.12, expected_duration * 0.05):
        raise LyricsTrainingError("egitim klibinin suresi beklenen aralikta degil")
    return {"duration_sec": round(duration, 3), "sample_rate_hz": rate,
            "channels": channels, "sample_width_bytes": width}


def record_example(dataset_dir, *, source_audio, source_group: str,
                   segment_id: str, start_sec, end_sec, label: str,
                   transcript_sha256: str, label_origin: str = "human_direct",
                   suggested_label: str | None = None,
                   context_sec: float = DEFAULT_CONTEXT_SEC,
                   actor: str = "operator", extractor=None) -> dict:
    """Create/update one explicitly human-labelled training example.

    Re-labelling the same transcript segment updates its row instead of making
    contradictory duplicate examples.  The caller may inject ``extractor`` for
    offline verification; production uses ffmpeg.
    """
    if label not in LABELS:
        raise LyricsTrainingError(f"gecersiz lyrics etiketi: {label}")
    if label_origin not in LABEL_ORIGINS:
        raise LyricsTrainingError(f"gecersiz etiket kaynagi: {label_origin}")
    if suggested_label is not None and suggested_label not in LABELS:
        raise LyricsTrainingError("onerilen lyrics etiketi gecersiz")
    source_group = str(source_group or "").strip()
    segment_id = str(segment_id or "").strip()
    transcript_sha256 = str(transcript_sha256 or "").strip().lower()
    if not source_group or not segment_id:
        raise LyricsTrainingError("kaynak grubu ve segment kimligi zorunlu")
    if len(transcript_sha256) != 64 or any(ch not in "0123456789abcdef"
                                           for ch in transcript_sha256):
        raise LyricsTrainingError("transkript SHA-256 kimligi gecersiz")

    source = Path(source_audio)
    if not source.is_file():
        raise LyricsTrainingError("etiketlenecek kaynak ses bulunamadi")
    start = _finite(start_sec, "segment baslangici")
    end = _finite(end_sec, "segment bitisi")
    context = max(0.0, min(2.0, _finite(context_sec, "baglam suresi")))
    if start < 0 or end <= start:
        raise LyricsTrainingError("segment zaman araligi gecersiz")
    clip_start = max(0.0, start - context)
    clip_end = end + context
    duration = clip_end - clip_start
    if duration < MIN_DURATION_SEC or duration > MAX_DURATION_SEC:
        raise LyricsTrainingError(
            f"egitim klibi {MIN_DURATION_SEC}-{MAX_DURATION_SEC} saniye olmali")

    root = Path(dataset_dir)
    example_id = _example_id(
        source_group=source_group, segment_id=segment_id,
        transcript_sha256=transcript_sha256, start_sec=start, end_sec=end)
    relative_clip = Path("clips") / f"{example_id}.wav"
    clip_path = root / relative_clip
    clip_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = clip_path.with_name(clip_path.stem + ".tmp.wav")
    try:
        (extractor or _extract_clip)(source, temporary, clip_start, duration)
        audio = _validate_wav(temporary, duration)
        os.replace(temporary, clip_path)
    finally:
        if temporary.exists():
            temporary.unlink()

    manifest = load_manifest(root)
    previous = next((row for row in manifest["examples"]
                     if row.get("id") == example_id), None)
    created_at = previous.get("created_at") if previous else _now()
    row = {
        "id": example_id,
        "clip": relative_clip.as_posix(),
        "source_group_sha256": hashlib.sha256(
            source_group.encode("utf-8")).hexdigest(),
        "split": _split_for_group(source_group),
        "segment_id": segment_id,
        "segment_start_sec": round(start, 3),
        "segment_end_sec": round(end, 3),
        "clip_start_sec": round(clip_start, 3),
        "clip_end_sec": round(clip_end, 3),
        "transcript_sha256": transcript_sha256,
        "label": label,
        "label_origin": label_origin,
        "suggested_label": suggested_label,
        "human_confirmed": True,
        "actor": str(actor or "operator")[:80],
        "created_at": created_at,
        "updated_at": _now(),
        "audio": audio,
    }
    manifest["examples"] = [item for item in manifest["examples"]
                            if item.get("id") != example_id] + [row]
    manifest["examples"].sort(key=lambda item: item.get("id", ""))
    manifest["updated_at"] = _now()
    _atomic_write_json(root / "manifest.json", manifest)
    return row


def dataset_summary(dataset_dir) -> dict:
    manifest = load_manifest(dataset_dir)
    examples = manifest["examples"]
    return {
        "example_count": len(examples),
        "labels": {label: sum(row.get("label") == label for row in examples)
                   for label in LABELS},
        "splits": {split: sum(row.get("split") == split for row in examples)
                   for split in ("train", "validation", "test")},
        "source_group_count": len({row.get("source_group_sha256")
                                   for row in examples}),
        "ready_for_training": len(examples) >= 500,
    }
