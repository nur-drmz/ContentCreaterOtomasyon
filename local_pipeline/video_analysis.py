"""Reusable video-analysis / hot-moment detection logic shared by
01_video_analysis_hotmoment.ipynb and 00_youtube_automation_master.ipynb.

Notebook 01 keeps its own multi-candidate benchmarking UI (comparing several
Whisper model sizes, scene detectors, and optional LLM scorers side by side),
but the actual algorithms it benchmarks -- scene detection, audio-peak
picking, and the local multi-signal hot-moment scoring formula -- live here so
both notebooks call the exact same code instead of maintaining duplicate,
independently-drifting copies.

This module never uploads or publishes anything; it only reads a local video
file and writes local preview clips / the returned in-memory data structure.
Callers (notebooks) are responsible for writing hot_moments.json to disk.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json

import numpy as np

STATUS_VALUES = {
    "experimental", "development_placeholder", "review_required",
    "approved", "insufficient_data", "failed", "production_ready",
}

SUPPORTED_SOURCE_VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v",
}


def resolve_source_video(source_video_dir, source_video_path="") -> Path:
    """Resolve the input video without requiring a filename on every run.

    An explicit ``source_video_path`` wins when supplied. Otherwise the newest
    non-empty supported video in ``source_video_dir`` is selected. The newest
    rule is deterministic (mtime, then filename), and callers can print the
    returned path before analysis.
    """
    if source_video_path:
        explicit = Path(source_video_path)
        if not explicit.is_file():
            raise FileNotFoundError(f"Kaynak video bulunamadi: {explicit}")
        if explicit.stat().st_size <= 0:
            raise ValueError(f"Kaynak video bos: {explicit}")
        return explicit

    source_dir = Path(source_video_dir)
    if not source_dir.is_dir():
        raise FileNotFoundError(f"Kaynak video klasoru bulunamadi: {source_dir}")

    candidates = [
        path for path in source_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_SOURCE_VIDEO_EXTENSIONS
        and path.stat().st_size > 0
    ]
    if not candidates:
        supported = ", ".join(sorted(SUPPORTED_SOURCE_VIDEO_EXTENSIONS))
        raise FileNotFoundError(
            f"{source_dir} icinde kaynak video bulunamadi. Desteklenen uzantilar: {supported}"
        )

    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name.lower()))

# Same emotion-word heuristic as the original notebook 01 scoring cell.
EMOTION_WORDS = {
    "shock", "shocking", "betray", "betrayal", "kill", "killed", "death", "dead",
    "war", "fight", "secret", "truth", "never", "abomination",
    "love", "hate", "failure", "power", "danger", "blood", "dragon", "throne",
    "ihanet", "sok", "olum", "oldur", "savas", "gizli", "gercek", "asla",
    "tehlike", "kan", "ejderha", "taht",
}


def extract_clip_and_audio(ffmpeg_bin: str, raw_video_path, video_out_path, audio_out_path,
                            start_sec: float, duration_sec: float) -> None:
    """Cuts [start_sec, start_sec+duration_sec) out of raw_video_path into a
    normalized H.264/AAC clip, then extracts a 16kHz mono WAV from that clip
    for transcription/audio-analysis. Raises RuntimeError with truncated
    stderr on ffmpeg failure."""
    video_out_path = Path(video_out_path)
    audio_out_path = Path(audio_out_path)
    video_out_path.parent.mkdir(parents=True, exist_ok=True)
    audio_out_path.parent.mkdir(parents=True, exist_ok=True)

    clip_cmd = [
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts", "-i", str(raw_video_path),
        "-ss", str(start_sec), "-t", str(duration_sec),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart",
        str(video_out_path),
    ]
    proc = subprocess.run(clip_cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"video klibi olusturulamadi: {proc.stderr[-3000:]}")

    audio_cmd = [
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_out_path), "-vn", "-map", "0:a:0",
        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(audio_out_path),
    ]
    proc = subprocess.run(audio_cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"videodan ses cikarilamadi: {proc.stderr[-3000:]}")


def transcribe_audio(audio_path, model_size: str = "small", device: str = "cpu",
                      compute_type: str = "int8", return_info: bool = False):
    """Transcribes audio_path with faster-whisper. Returns (full_text, segments)
    where each segment is {'start_sec', 'end_sec', 'text', 'avg_logprob',
    'no_speech_prob', 'words': [{'start_sec','end_sec','word','probability'}]}.
    """
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    segments_iter, info = model.transcribe(
        str(audio_path), language=None, beam_size=5, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500}, word_timestamps=True,
        condition_on_previous_text=False, multilingual=True,
    )
    segments = list(segments_iter)
    text = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
    segment_rows = []
    for seg in segments:
        word_rows = []
        for word in (seg.words or []):
            if word.start is None or word.end is None:
                continue
            word_rows.append({
                "start_sec": round(float(word.start), 3),
                "end_sec": round(float(word.end), 3),
                "word": word.word.strip(),
                "probability": round(float(word.probability or 0.0), 4),
            })
        segment_rows.append({
            "start_sec": round(float(seg.start), 3),
            "end_sec": round(float(seg.end), 3),
            "text": seg.text.strip(),
            "avg_logprob": round(float(seg.avg_logprob), 4),
            "no_speech_prob": round(float(seg.no_speech_prob), 4),
            "words": word_rows,
        })
    if return_info:
        return text, segment_rows, {
            "language": str(getattr(info, "language", "") or "").strip() or None,
            "language_probability": round(
                float(getattr(info, "language_probability", 0.0) or 0.0), 4),
        }
    return text, segment_rows


def detect_scenes(video_path, detector: str = "content") -> list[tuple[float, float]]:
    """Runs PySceneDetect against video_path. detector: 'content' or 'adaptive'.
    Returns a list of (start_sec, end_sec) float tuples."""
    from scenedetect import SceneManager, open_video
    from scenedetect.detectors import AdaptiveDetector, ContentDetector

    detector_cls = {"content": ContentDetector, "adaptive": AdaptiveDetector}.get(detector)
    if detector_cls is None:
        raise ValueError(f"bilinmeyen sahne dedektoru: {detector!r} (beklenen: content, adaptive)")

    video = open_video(str(video_path))
    scene_manager = SceneManager()
    scene_manager.add_detector(detector_cls())
    scene_manager.detect_scenes(video)
    scene_list = scene_manager.get_scene_list()

    def _seconds(timecode):
        try:
            return float(timecode.get_seconds())
        except Exception:
            return float(getattr(timecode, "seconds", 0.0))

    return [(_seconds(start), _seconds(end)) for start, end in scene_list]


def compute_audio_peaks(audio_path, top_n: int = 10, min_distance_sec: float = 1.5,
                         hop_length: int = 512) -> dict:
    """Loads audio_path (resampled to 16kHz) and returns
    {'times': np.ndarray, 'rms': np.ndarray, 'peaks': [{'time_sec','rms'}, ...]}
    sorted by time, deduplicated so no two peaks are within min_distance_sec."""
    import librosa

    y, sr = librosa.load(str(audio_path), sr=16000)
    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
    times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop_length)

    peak_details = []
    for idx in np.argsort(rms)[::-1]:
        peak_time = float(times[idx])
        if all(abs(peak_time - item["time_sec"]) >= min_distance_sec for item in peak_details):
            peak_details.append({"time_sec": round(peak_time, 3), "rms": round(float(rms[idx]), 6)})
        if len(peak_details) == top_n:
            break
    peak_details.sort(key=lambda item: item["time_sec"])
    return {"times": times, "rms": rms, "peaks": peak_details}


def _minmax(values):
    values = np.asarray(values, dtype=float)
    low, high = float(values.min()), float(values.max())
    if high - low < 1e-12:
        return np.full(len(values), 0.5)
    return (values - low) / (high - low)


def expand_moment_boundaries(start_sec: float, end_sec: float,
                             overlapping_segments: list[dict],
                             scene_boundaries: list[float], analysis_duration: float,
                             max_scene_extension_sec: float = 6.0,
                             speech_padding_sec: float = 0.35,
                             fallback_padding_sec: float = 0.75) -> tuple[float, float, dict]:
    """Expand a scored window without ever trimming context from it.

    Speech that overlaps the window is included first. The start may then move
    only backward to a nearby scene boundary, while the end may move only
    forward. When there is no nearby boundary, a small fallback pad is used.
    This prevents the old nearest-boundary behaviour from cutting the opening
    or closing shot out of an otherwise good hot moment.
    """
    duration = max(0.0, float(analysis_duration))
    original_start = max(0.0, min(float(start_sec), duration))
    original_end = max(original_start, min(float(end_sec), duration))

    speech_starts = [
        max(0.0, float(segment["start_sec"]) - float(speech_padding_sec))
        for segment in overlapping_segments
        if segment.get("start_sec") is not None
    ]
    speech_ends = [
        min(duration, float(segment["end_sec"]) + float(speech_padding_sec))
        for segment in overlapping_segments
        if segment.get("end_sec") is not None
    ]
    context_start = min([original_start, *speech_starts])
    context_end = max([original_end, *speech_ends])

    boundaries = sorted({0.0, duration, *(
        max(0.0, min(float(boundary), duration)) for boundary in scene_boundaries
    )})
    previous_boundaries = [boundary for boundary in boundaries if boundary <= context_start]
    following_boundaries = [boundary for boundary in boundaries if boundary >= context_end]
    previous_boundary = max(previous_boundaries) if previous_boundaries else 0.0
    following_boundary = min(following_boundaries) if following_boundaries else duration

    if context_start - previous_boundary <= float(max_scene_extension_sec):
        aligned_start = previous_boundary
        start_strategy = "previous_scene_boundary"
    else:
        aligned_start = max(0.0, context_start - float(fallback_padding_sec))
        start_strategy = "fallback_padding"

    if following_boundary - context_end <= float(max_scene_extension_sec):
        aligned_end = following_boundary
        end_strategy = "next_scene_boundary"
    else:
        aligned_end = min(duration, context_end + float(fallback_padding_sec))
        end_strategy = "fallback_padding"

    # Defensive invariants: scene/speech alignment may only expand the scored
    # window. It must never move the start forward or the end backward.
    aligned_start = min(aligned_start, original_start)
    aligned_end = max(aligned_end, original_end)
    metadata = {
        "policy": "speech-context-directional-scene-v1",
        "original_start_sec": round(original_start, 3),
        "original_end_sec": round(original_end, 3),
        "speech_context_start_sec": round(context_start, 3),
        "speech_context_end_sec": round(context_end, 3),
        "start_strategy": start_strategy,
        "end_strategy": end_strategy,
    }
    return round(aligned_start, 3), round(aligned_end, 3), metadata


def score_hot_moments(transcript_segments: list[dict], times, rms, scene_boundaries: list[float],
                       analysis_duration: float, clip_start_sec: float, top_n: int = 5,
                       window_sec: float = 30.0, stride_sec: float = 10.0,
                       max_scene_extension_sec: float = 6.0,
                       speech_padding_sec: float = 0.35,
                       fallback_padding_sec: float = 0.75,
                       max_overlap_ratio: float = 0.25,
                       min_event_center_distance_sec: float = 35.0) -> list[dict]:
    """Core local multi-signal hot-moment scoring algorithm (identical to the
    'local-multisignal-v2' method notebook 01 benchmarks against optional LLM
    scorers). Combines speech density, scene-cut density, audio energy,
    emotion-word density, transcript confidence, and lexical diversity into a
    weighted 0-100 score per sliding window, then greedily selects up to
    top_n diverse, non-overlapping, scene-aligned windows.

    Returns a list of hot-moment dicts (rank, aligned_start_sec/end_sec,
    source_start_sec/end_sec, score, reason, transcript, component_scores).
    Does NOT generate preview clips -- see generate_preview_clips().
    """
    local_windows = []
    window_starts = np.arange(0.0, max(analysis_duration - window_sec, 0.0) + 0.001, stride_sec)
    if len(window_starts) == 0:
        window_starts = np.array([0.0])

    for start_sec in window_starts:
        end_sec = min(float(start_sec + window_sec), analysis_duration)
        overlapping_segments = [
            seg for seg in transcript_segments
            if seg["end_sec"] > start_sec and seg["start_sec"] < end_sec
        ]
        text = " ".join(seg["text"] for seg in overlapping_segments if seg["text"]).strip()
        words = re.findall(r"[A-Za-zÀ-ž']+", text.lower())
        word_probs = [
            word["probability"]
            for seg in overlapping_segments
            for word in seg.get("words", [])
            if word.get("probability") is not None
        ]
        audio_mask = (times >= start_sec) & (times < end_sec)
        window_rms = rms[audio_mask]
        duration_sec = max(end_sec - start_sec, 1.0)
        local_windows.append({
            "start_sec": round(float(start_sec), 3),
            "end_sec": round(float(end_sec), 3),
            "transcript": text,
            "overlapping_segments": overlapping_segments,
            "word_count": len(words),
            "speech_density": len(words) / duration_sec,
            "scene_density": sum(start_sec < cut < end_sec for cut in scene_boundaries) / duration_sec,
            "audio_energy": float(np.percentile(window_rms, 90)) if len(window_rms) else 0.0,
            "emotion_density": sum(word in EMOTION_WORDS for word in words) / max(len(words), 1),
            "transcript_confidence": float(np.mean(word_probs)) if word_probs else 0.0,
            "lexical_diversity": (len(set(words)) / max(len(words), 1)) * min(len(words) / 20.0, 1.0),
        })

    component_keys = [
        "audio_energy", "scene_density", "speech_density",
        "emotion_density", "transcript_confidence", "lexical_diversity",
    ]
    normalized = {key: _minmax([w[key] for w in local_windows]) for key in component_keys}
    weights = {
        "audio_energy": 0.30, "scene_density": 0.25, "speech_density": 0.15,
        "emotion_density": 0.15, "transcript_confidence": 0.10, "lexical_diversity": 0.05,
    }
    for index, window in enumerate(local_windows):
        component_scores = {key: round(float(normalized[key][index]) * 100, 2) for key in component_keys}
        window["component_scores"] = component_scores
        window["score"] = round(sum(weights[key] * component_scores[key] for key in weights), 2)

    def overlap_ratio(left, right):
        intersection = max(0.0, min(left["end_sec"], right["end_sec"]) - max(left["start_sec"], right["start_sec"]))
        shortest = min(left["end_sec"] - left["start_sec"], right["end_sec"] - right["start_sec"])
        return intersection / max(shortest, 1e-9)

    def window_center(item):
        return (item["start_sec"] + item["end_sec"]) / 2.0

    hot_moments = []
    for candidate in sorted(local_windows, key=lambda item: item["score"], reverse=True):
        is_diverse = all(
            overlap_ratio(candidate, chosen) < float(max_overlap_ratio)
            and abs(window_center(candidate) - window_center(chosen))
                >= float(min_event_center_distance_sec)
            for chosen in hot_moments
        )
        if is_diverse:
            aligned_start, aligned_end, boundary_context = expand_moment_boundaries(
                candidate["start_sec"], candidate["end_sec"],
                candidate.pop("overlapping_segments", []), scene_boundaries,
                analysis_duration, max_scene_extension_sec=max_scene_extension_sec,
                speech_padding_sec=speech_padding_sec,
                fallback_padding_sec=fallback_padding_sec,
            )
            candidate["rank"] = len(hot_moments) + 1
            candidate["aligned_start_sec"] = round(float(aligned_start), 3)
            candidate["aligned_end_sec"] = round(float(aligned_end), 3)
            candidate["source_start_sec"] = round(clip_start_sec + aligned_start, 3)
            candidate["source_end_sec"] = round(clip_start_sec + aligned_end, 3)
            candidate["boundary_context"] = boundary_context
            candidate["reason"] = (
                f"audio={candidate['component_scores']['audio_energy']}, "
                f"scene={candidate['component_scores']['scene_density']}, "
                f"speech={candidate['component_scores']['speech_density']}, "
                f"emotion={candidate['component_scores']['emotion_density']}"
            )
            hot_moments.append(candidate)
        if len(hot_moments) == top_n:
            break
    return hot_moments


def generate_preview_clips(ffmpeg_bin: str, video_path, hot_moments: list[dict], preview_dir,
                           progress=None) -> None:
    """Cuts a fast 720p preview clip per hot moment into preview_dir, mutating
    each moment dict in place with 'preview_path' (or 'preview_error')."""
    report = progress if progress is not None else _NullProgress()
    preview_dir = Path(preview_dir)
    preview_dir.mkdir(parents=True, exist_ok=True)
    total = len(hot_moments)
    report.count(0, total, unit="klip", message=f"0/{total} onizleme")
    for done, moment in enumerate(hot_moments, start=1):
        preview_path = preview_dir / f"hot_moment_{moment['rank']:02d}.mp4"
        moment["preview_path"] = None
        preview_duration = moment["aligned_end_sec"] - moment["aligned_start_sec"]
        cmd = [
            ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", str(moment["aligned_start_sec"]), "-i", str(video_path),
            "-t", str(preview_duration),
            "-vf", "scale=-2:720", "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-movflags", "+faststart", str(preview_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            moment["preview_path"] = str(preview_path)
        else:
            moment["preview_error"] = proc.stderr[-1000:]
        # A genuine count of finished clips -- the one place in analysis where a
        # real percentage exists.
        report.count(done, total, unit="klip", message=f"{done}/{total} onizleme")


class _NullProgress:
    """No-op observer, so the notebook call paths stay untouched."""

    def stage(self, key, message="") -> None: ...
    def count(self, completed, total, *, unit="", message="") -> None: ...
    def note(self, message: str) -> None: ...


def _sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hot_moments_content_sha256(hot_moments_data: dict) -> str:
    """Stable hash binding a human moment decision to one analysis result."""
    encoded = json.dumps(
        hot_moments_data, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_moment_selection(
    hot_moments_data: dict,
    selected_moment_rank: int,
    reviewed_by: str,
    rejected_moment_ranks=None,
    review_notes: str = "",
    require_preview: bool = True,
    reviewed_at: str | None = None,
) -> dict:
    """Creates the human-reviewed `moment_selection.json` artifact.

    The artifact is bound to both the full hot-moments manifest and the exact
    selected preview bytes. A plain boolean in a later cell therefore cannot
    approve an unseen or since-regenerated moment.
    """
    schema_errors = validate_hot_moments_schema(hot_moments_data)
    if schema_errors:
        raise ValueError(f"hot_moments.json semasi gecersiz: {schema_errors}")
    reviewer = str(reviewed_by or "").strip()
    if not reviewer:
        raise ValueError("MOMENT_REVIEWED_BY bos olamaz; moment secimi insan kararidir")
    try:
        selected_rank = int(selected_moment_rank)
    except (TypeError, ValueError) as exc:
        raise ValueError("SELECTED_MOMENT_RANK pozitif bir tam sayi olmali") from exc
    moments_by_rank = {int(moment["rank"]): moment for moment in hot_moments_data["hot_moments"]}
    if selected_rank not in moments_by_rank:
        raise ValueError(
            f"SELECTED_MOMENT_RANK={selected_rank} bulunamadi; "
            f"mevcut ranklar: {sorted(moments_by_rank)}"
        )
    rejected = sorted({int(rank) for rank in (rejected_moment_ranks or [])})
    unknown_rejected = [rank for rank in rejected if rank not in moments_by_rank]
    if unknown_rejected:
        raise ValueError(f"bilinmeyen reddedilmis ranklar: {unknown_rejected}")
    if selected_rank in rejected:
        raise ValueError("secilen moment ayni anda reddedilmis olamaz")

    selected = moments_by_rank[selected_rank]
    preview_path = selected.get("preview_path")
    preview_sha256 = None
    preview_size_bytes = None
    if preview_path and Path(preview_path).is_file():
        preview_size_bytes = Path(preview_path).stat().st_size
        if preview_size_bytes <= 0:
            raise ValueError(f"secilen moment preview dosyasi bos: {preview_path}")
        preview_sha256 = _sha256_file(preview_path)
    elif require_preview:
        raise ValueError(
            f"secilen moment icin izlenebilir preview bulunamadi: {preview_path!r}; "
            "Faz 1 analizini/preview uretimini yeniden calistir"
        )

    return {
        "schema_version": 1,
        "artifact_type": "moment_selection",
        "status": "approved",
        "status_reason": "preview insan tarafindan incelendi ve moment secildi",
        "selected_moment_rank": selected_rank,
        "selected_moment": {
            "rank": selected_rank,
            "source_start_sec": selected.get("source_start_sec"),
            "source_end_sec": selected.get("source_end_sec"),
            "aligned_start_sec": selected.get("aligned_start_sec"),
            "aligned_end_sec": selected.get("aligned_end_sec"),
            "score": selected.get("score"),
            "reason": selected.get("reason"),
            "transcript": selected.get("transcript"),
            "preview_path": str(preview_path) if preview_path else None,
            "preview_sha256": preview_sha256,
            "preview_size_bytes": preview_size_bytes,
        },
        "rejected_moment_ranks": rejected,
        "reviewed_by": reviewer,
        "reviewed_at": reviewed_at or datetime.now(timezone.utc).isoformat(),
        "review_notes": str(review_notes or "").strip() or None,
        "hot_moments_content_sha256": hot_moments_content_sha256(hot_moments_data),
        "hot_moments_status_at_review": hot_moments_data.get("status"),
        "source": dict(hot_moments_data.get("source") or {}),
        "human_review": {
            "preview_reviewed": True,
            "selection_confirmed": True,
            "automatic_selection": False,
        },
    }


def validate_moment_selection(
    selection: dict,
    hot_moments_data: dict,
    require_preview: bool = True,
) -> list[str]:
    """Returns reasons a persisted selection is missing, stale, or unsafe."""
    errors = []
    if not isinstance(selection, dict):
        return ["moment_selection nesnesi sozluk degil"]
    required = (
        "schema_version", "artifact_type", "status", "selected_moment_rank",
        "selected_moment", "reviewed_by", "reviewed_at",
        "hot_moments_content_sha256", "human_review",
    )
    for field in required:
        if field not in selection:
            errors.append(f"moment_selection icinde '{field}' eksik")
    if errors:
        return errors
    if selection.get("schema_version") != 1:
        errors.append("moment_selection schema_version desteklenmiyor")
    if selection.get("artifact_type") != "moment_selection":
        errors.append("artifact_type moment_selection degil")
    if selection.get("status") != "approved":
        errors.append("moment selection status approved degil")
    if not str(selection.get("reviewed_by") or "").strip():
        errors.append("reviewed_by bos")
    human_review = selection.get("human_review") or {}
    if human_review.get("preview_reviewed") is not True:
        errors.append("preview_reviewed True degil")
    if human_review.get("selection_confirmed") is not True:
        errors.append("selection_confirmed True degil")
    if human_review.get("automatic_selection") is not False:
        errors.append("automatic_selection False degil")

    schema_errors = validate_hot_moments_schema(hot_moments_data)
    if schema_errors:
        errors.extend(schema_errors)
        return errors
    expected_manifest_hash = hot_moments_content_sha256(hot_moments_data)
    if selection.get("hot_moments_content_sha256") != expected_manifest_hash:
        errors.append("hot_moments.json degisti; moment secimi yeniden incelenmeli")
    moments_by_rank = {int(moment["rank"]): moment for moment in hot_moments_data["hot_moments"]}
    try:
        selected_rank = int(selection.get("selected_moment_rank"))
    except (TypeError, ValueError):
        selected_rank = None
        errors.append("selected_moment_rank gecersiz")
    if selected_rank not in moments_by_rank:
        errors.append("selected_moment_rank mevcut hot moments icinde yok")
        return errors
    if selected_rank in (selection.get("rejected_moment_ranks") or []):
        errors.append("secilen rank reddedilenler listesinde")

    selected_summary = selection.get("selected_moment") or {}
    if selected_summary.get("rank") != selected_rank:
        errors.append("selected_moment.rank selected_moment_rank ile eslesmiyor")
    preview_path = selected_summary.get("preview_path")
    if preview_path and Path(preview_path).is_file():
        if Path(preview_path).stat().st_size <= 0:
            errors.append("secilen preview dosyasi bos")
        elif selected_summary.get("preview_sha256") != _sha256_file(preview_path):
            errors.append("secilen preview dosyasi incelemeden sonra degismis")
    elif require_preview:
        errors.append("secilen preview dosyasi bulunamadi")
    return errors


def build_hot_moments_manifest(hot_moments: list[dict], transcript_candidate: str, window_sec: float,
                                stride_sec: float, weights: dict, raw_video_path, analysis_clip_path,
                                clip_start_sec: float, clip_duration_sec: float,
                                candidate_request: dict | None = None) -> dict:
    """Builds the hot_moments.json contract dict. Status is always
    'review_required' -- this is a candidate-generation step, never
    auto-approved/production_ready."""
    for moment in hot_moments:
        for field in ("aligned_start_sec", "aligned_end_sec", "source_start_sec", "source_end_sec"):
            value = moment.get(field)
            if not isinstance(value, (int, float)):
                raise ValueError(f"hot moment rank={moment.get('rank')} icin '{field}' sayisal degil: {value!r}")
        if not (moment["aligned_end_sec"] > moment["aligned_start_sec"]):
            raise ValueError(f"hot moment rank={moment.get('rank')} icin aligned suresi pozitif degil")
        if not (moment["source_end_sec"] > moment["source_start_sec"]):
            raise ValueError(f"hot moment rank={moment.get('rank')} icin source suresi pozitif degil")

    import os as _os
    raw_video_exists = bool(raw_video_path) and _os.path.exists(raw_video_path)
    analysis_clip_exists = bool(analysis_clip_path) and _os.path.exists(analysis_clip_path)

    manifest = {
        "method": "local-multisignal-v2",
        "status": "review_required",
        "status_reason": "local heuristic candidate generation only; final selection requires LLM or human review before publish",
        "transcript_candidate": transcript_candidate,
        "window_sec": window_sec,
        "stride_sec": stride_sec,
        "weights": weights,
        "source": {
            "raw_video_path": str(raw_video_path) if raw_video_path else None,
            "raw_video_exists": raw_video_exists,
            "analysis_clip_path": str(analysis_clip_path) if analysis_clip_path else None,
            "analysis_clip_exists": analysis_clip_exists,
            "clip_start_sec": clip_start_sec,
            "clip_duration_sec": clip_duration_sec,
            "timestamp_usage_notes": (
                "source_start_sec/source_end_sec are absolute offsets into raw_video_path; "
                "aligned_start_sec/aligned_end_sec are 0-based offsets into analysis_clip_path "
                "(which is already trimmed to [clip_start_sec, clip_start_sec+clip_duration_sec] "
                "of the raw video); preview_path files are already cut to the moment and need no seek."
            ),
        },
        "hot_moments": hot_moments,
    }
    if candidate_request:
        manifest["candidate_request"] = dict(candidate_request)
    return manifest


DEFAULT_WEIGHTS = {
    "audio_energy": 0.30, "scene_density": 0.25, "speech_density": 0.15,
    "emotion_density": 0.15, "transcript_confidence": 0.10, "lexical_diversity": 0.05,
}


def run_video_analysis(ffmpeg_bin: str, raw_video_path, runtime_dir, clip_start_sec: float = 0.0,
                        clip_duration_sec: float = 240.0, whisper_model_size: str = "small",
                        whisper_device: str = "cpu", whisper_compute_type: str = "int8",
                        scene_detector: str = "content", top_n: int = 5, window_sec: float = 30.0,
                        stride_sec: float = 10.0, preview_dir=None, progress=None,
                        adaptive_candidate_windows: bool = False) -> dict:
    """High-level, single-default-candidate video analysis pipeline (no
    multi-model benchmarking): extracts the configured clip, transcribes it,
    detects scenes, computes audio peaks, scores hot moments, generates
    preview clips, and returns the hot_moments.json contract dict.

    This is the function 00_youtube_automation_master.ipynb calls directly
    for RUN_VIDEO_ANALYSIS; 01_video_analysis_hotmoment.ipynb calls the lower-
    level functions above (score_hot_moments, detect_scenes) for its own
    multi-candidate benchmarking loop so both notebooks share one algorithm.
    """
    # `progress` is an optional observer with .stage()/.count(). The notebooks
    # pass nothing and behave exactly as before; the console passes a tracker so
    # the UI can say which stage is running instead of only "still working".
    report = progress if progress is not None else _NullProgress()

    runtime_dir = Path(runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    video_path = runtime_dir / "source_clip.mp4"
    audio_path = runtime_dir / "source_clip.wav"

    report.stage("preparation", "Kaynak klip cikariliyor")
    extract_clip_and_audio(ffmpeg_bin, raw_video_path, video_path, audio_path, clip_start_sec, clip_duration_sec)
    report.stage("audio", "Ses akisi hazir")

    report.stage("whisper", f"faster-whisper-{whisper_model_size} calisiyor")
    text, segments, transcript_info = transcribe_audio(
        audio_path, model_size=whisper_model_size,
        device=whisper_device, compute_type=whisper_compute_type,
        return_info=True,
    )
    # An empty transcript is NOT fatal: a music-only / no-speech video still has
    # usable audio-energy and scene-cut signals, and notebook 01's original
    # scoring loop also tolerated empty window text. It IS recorded on the
    # manifest (transcript_available=False) so a downstream script-generation
    # phase can see that its prompt will have no dialogue to work from.
    transcript_available = bool(text.strip())
    if not transcript_available:
        print('UYARI: transkript bos dondu (konusma tespit edilemedi). Hot-moment skorlamasi '
              'sadece ses enerjisi + sahne kesme sinyalleriyle yapilacak; uretilecek script '
              'prompt\'unda diyalog metni olmayacak.')

    report.stage("scenes", "Sahne kesitleri bulunuyor")
    scenes = detect_scenes(video_path, detector=scene_detector)
    scene_boundaries = [end for _, end in scenes[:-1]] if scenes else []
    report.count(len(scenes), len(scenes), unit="sahne",
                 message=f"{len(scenes)} sahne bulundu")

    report.stage("scoring", "Ses enerjisi ve moment skorlari")
    audio_info = compute_audio_peaks(audio_path)
    times, rms = audio_info["times"], audio_info["rms"]
    analysis_duration = min(float(clip_duration_sec), float(times[-1]) if len(times) else 0.0)

    # The notebook benchmark keeps the historic 30s/10s scoring windows. The
    # Creator Console instead exposes "candidate count" as a user choice, so
    # its windows must adapt to the actually available clip. Otherwise a 20s
    # analysis with top_n=5 can mathematically create only one 30s window.
    requested_candidates = max(1, int(top_n))
    diversity_distance_sec = 35.0
    if adaptive_candidate_windows and analysis_duration > 0:
        window_sec = min(30.0, analysis_duration / requested_candidates)
        if requested_candidates > 1:
            stride_sec = (analysis_duration - window_sec) / (requested_candidates - 1)
        else:
            stride_sec = max(window_sec, analysis_duration)
        # Floating point drift at the last np.arange boundary should not turn a
        # requested fifth candidate into four. These values still select
        # non-overlapping windows distributed across the complete clip.
        window_sec = max(0.05, float(window_sec))
        stride_sec = max(0.05, float(stride_sec))
        diversity_distance_sec = max(0.01, stride_sec * 0.8)

    hot_moments = score_hot_moments(
        segments, times, rms, scene_boundaries, analysis_duration, clip_start_sec,
        top_n=top_n, window_sec=window_sec, stride_sec=stride_sec,
        min_event_center_distance_sec=diversity_distance_sec,
    )
    if not hot_moments:
        raise RuntimeError("hicbir hot-moment adayi bulunamadi (video cok kisa veya sessiz olabilir)")

    report.stage("previews", f"{len(hot_moments)} onizleme klibi uretiliyor")
    generate_preview_clips(ffmpeg_bin, video_path, hot_moments,
                           preview_dir or (runtime_dir / "hot_moment_previews"),
                           progress=report)

    manifest = build_hot_moments_manifest(
        hot_moments, transcript_candidate=f"faster-whisper-{whisper_model_size}",
        window_sec=window_sec, stride_sec=stride_sec, weights=DEFAULT_WEIGHTS,
        raw_video_path=raw_video_path, analysis_clip_path=video_path,
        clip_start_sec=clip_start_sec, clip_duration_sec=clip_duration_sec,
        candidate_request={
            "requested": requested_candidates,
            "produced": len(hot_moments),
            "policy": ("target-count-adaptive-v1" if adaptive_candidate_windows
                       else "fixed-window-diversity-v1"),
            "analysis_duration_sec": round(float(analysis_duration), 3),
            "window_sec": round(float(window_sec), 3),
            "stride_sec": round(float(stride_sec), 3),
            "complete": len(hot_moments) == requested_candidates,
        },
    )
    manifest["transcript_available"] = transcript_available
    manifest["transcript_language"] = transcript_info.get("language")
    manifest["transcript_language_probability"] = transcript_info.get("language_probability")
    if not transcript_available:
        manifest["status_reason"] += "; WARNING: transcript was empty (no speech detected)"
    return manifest


def validate_hot_moments_schema(data: dict) -> list[str]:
    """Returns a list of human-readable schema errors (empty if valid).
    Shared by any downstream phase (script generation) that loads
    hot_moments.json and must refuse to proceed on a malformed file."""
    errors = []
    for key in ("method", "status", "hot_moments"):
        if key not in data:
            errors.append(f"ust seviye alan eksik: '{key}'")
    moments = data.get("hot_moments")
    if not isinstance(moments, list) or len(moments) == 0:
        errors.append("'hot_moments' bos veya liste degil")
    else:
        required_moment_fields = ["rank", "aligned_start_sec", "aligned_end_sec", "score", "reason", "transcript"]
        for idx, moment in enumerate(moments):
            for field in required_moment_fields:
                if field not in moment:
                    errors.append(f"hot_moments[{idx}] icinde '{field}' alani eksik")
    return errors
