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

FOCUS_REGIONS = ("start", "middle", "end", "custom")


def resolve_analysis_range(*, source_duration_sec, focus_region=None,
                           clip_duration_min=None, custom_start_min=None,
                           clip_start_sec=0, clip_duration_sec=240) -> tuple[float, float]:
    """Resolve a human-friendly long-video region to canonical seconds.

    Existing notebook callers keep their second-based arguments. The Creator
    Console can instead send ``focus_region`` plus minutes. The range is always
    clamped to the actual source so "last 10 minutes" cannot seek past EOF.
    """
    if focus_region in (None, ""):
        start = float(clip_start_sec or 0)
        duration = float(clip_duration_sec or 240)
        if start < 0 or duration <= 0:
            raise ValueError("clip_start_sec >= 0 ve clip_duration_sec > 0 olmali")
        return start, duration
    if focus_region not in FOCUS_REGIONS:
        raise ValueError(f"bilinmeyen focus_region: {focus_region!r}")
    try:
        source_duration = float(source_duration_sec)
        requested_duration = float(clip_duration_min) * 60.0
    except (TypeError, ValueError) as exc:
        raise ValueError("video suresi ve analiz suresi sayisal olmali") from exc
    if source_duration <= 0:
        raise ValueError("uzun video odagi icin kaynak video suresi bilinmiyor")
    if requested_duration <= 0:
        raise ValueError("analiz suresi dakika olarak 0'dan buyuk olmali")
    duration = min(source_duration, requested_duration)
    if focus_region == "start":
        start = 0.0
    elif focus_region == "middle":
        start = (source_duration - duration) / 2.0
    elif focus_region == "end":
        start = source_duration - duration
    else:
        try:
            start = float(custom_start_min) * 60.0
        except (TypeError, ValueError) as exc:
            raise ValueError("ozel baslangic dakikasi sayisal olmali") from exc
        if start < 0:
            raise ValueError("ozel baslangic dakikasi negatif olamaz")
        start = min(start, max(0.0, source_duration - duration))
    return round(max(0.0, start), 3), round(duration, 3)


def _raise_if_cancelled(progress) -> None:
    checker = getattr(progress, "raise_if_cancelled", None)
    if checker:
        checker()


def _run_process_checked(cmd, progress):
    """Run a child process while honoring the console's cooperative stop flag."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while proc.poll() is None:
            _raise_if_cancelled(progress)
            time.sleep(0.1)
        stdout, stderr = proc.communicate()
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except BaseException:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        proc.communicate()
        raise


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
                            start_sec: float, duration_sec: float, progress=None) -> None:
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
    report = progress if progress is not None else _NullProgress()
    proc = _run_process_checked(clip_cmd, report)
    if proc.returncode != 0:
        raise RuntimeError(f"video klibi olusturulamadi: {proc.stderr[-3000:]}")

    audio_cmd = [
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_out_path), "-vn", "-map", "0:a:0",
        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(audio_out_path),
    ]
    proc = _run_process_checked(audio_cmd, report)
    if proc.returncode != 0:
        raise RuntimeError(f"videodan ses cikarilamadi: {proc.stderr[-3000:]}")


def transcribe_audio(audio_path, model_size: str = "small", device: str = "cpu",
                      compute_type: str = "int8", return_info: bool = False,
                      progress=None):
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
    report = progress if progress is not None else _NullProgress()
    segments = []
    for segment in segments_iter:
        _raise_if_cancelled(report)
        segments.append(segment)
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
                           progress=None, time_field: str = "aligned") -> None:
    """Cuts a fast 720p preview clip per hot moment into preview_dir, mutating
    each moment dict in place with 'preview_path' (or 'preview_error').

    ``time_field`` selects which timeline ``video_path`` is on. The default
    "aligned" keeps every existing caller unchanged: offsets are read as 0-based
    positions inside the trimmed analysis clip. Full-source analysis has no
    single analysis clip, so it passes the raw video with "source" and the
    absolute ``source_*_sec`` offsets are used instead.
    """
    if time_field not in ("aligned", "source"):
        raise ValueError(f"bilinmeyen time_field: {time_field!r} (beklenen: aligned, source)")
    start_key, end_key = f"{time_field}_start_sec", f"{time_field}_end_sec"
    report = progress if progress is not None else _NullProgress()
    preview_dir = Path(preview_dir)
    preview_dir.mkdir(parents=True, exist_ok=True)
    total = len(hot_moments)
    report.count(0, total, unit="klip", message=f"0/{total} onizleme")
    for done, moment in enumerate(hot_moments, start=1):
        preview_path = preview_dir / f"hot_moment_{moment['rank']:02d}.mp4"
        moment["preview_path"] = None
        preview_duration = moment[end_key] - moment[start_key]
        cmd = [
            ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", str(moment[start_key]), "-i", str(video_path),
            "-t", str(preview_duration),
            "-vf", "scale=-2:720", "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-movflags", "+faststart", str(preview_path),
        ]
        proc = _run_process_checked(cmd, report)
        if proc.returncode == 0:
            moment["preview_path"] = str(preview_path)
        else:
            moment["preview_error"] = proc.stderr[-1000:]
        # A genuine count of finished clips -- the one place in analysis where a
        # real percentage exists.
        report.count(done, total, unit="klip", message=f"{done}/{total} onizleme")


#: Full-source scan tuning. A region is the unit pass 2 transcribes, so it has
#: to be long enough to contain a whole moment plus its scene context, and short
#: enough that transcribing a handful of them stays far cheaper than
#: transcribing the entire film.
FULL_SCAN_REGION_DURATION_SEC = 180.0
#: Scan more regions than the caller wants moments: the loudest region does not
#: always contain the best moment once speech and scene signals are known.
FULL_SCAN_REGION_MULTIPLIER = 2
FULL_SCAN_MAX_REGIONS = 12
FULL_SCAN_MOMENTS_PER_REGION = 3
#: Weight of the globally comparable energy signal when merging candidates that
#: were each scored inside their own region. See `_merge_region_moments`.
FULL_SCAN_GLOBAL_ENERGY_WEIGHT = 0.4


def extract_full_audio(ffmpeg_bin: str, raw_video_path, audio_out_path, progress=None) -> None:
    """Extract the WHOLE source's audio as 16kHz mono PCM in one pass.

    Pass 1 of a full-source scan needs to see the entire film, and audio is the
    only signal cheap enough to compute over hours of video. Raises RuntimeError
    with truncated stderr when the source has no usable audio stream.
    """
    audio_out_path = Path(audio_out_path)
    audio_out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(raw_video_path), "-vn", "-map", "0:a:0",
        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(audio_out_path),
    ]
    report = progress if progress is not None else _NullProgress()
    proc = _run_process_checked(cmd, report)
    if proc.returncode != 0:
        raise RuntimeError(f"kaynak sesi cikarilamadi: {proc.stderr[-3000:]}")


def scan_audio_energy_profile(audio_path, frame_sec: float = 1.0) -> dict:
    """Coarse RMS energy per `frame_sec` across an entire WAV, read in blocks.

    Deliberately does NOT use librosa: loading a two-hour 16kHz track as one
    float array costs hundreds of megabytes, and pass 1 only needs a
    one-sample-per-second envelope. Reading block by block keeps memory flat
    regardless of source length.
    """
    import wave

    if float(frame_sec) <= 0:
        raise ValueError("frame_sec 0'dan buyuk olmali")
    times: list[float] = []
    energies: list[float] = []
    with wave.open(str(audio_path), "rb") as stream:
        channels = stream.getnchannels()
        width = stream.getsampwidth()
        rate = stream.getframerate()
        if width != 2:
            raise ValueError(f"beklenen 16-bit PCM, bulunan {width * 8}-bit")
        if rate <= 0:
            raise ValueError("gecersiz ornekleme hizi")
        block_frames = max(1, int(rate * float(frame_sec)))
        consumed = 0
        while True:
            raw = stream.readframes(block_frames)
            if not raw:
                break
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            if channels > 1:
                usable = (samples.size // channels) * channels
                samples = samples[:usable].reshape(-1, channels).mean(axis=1)
            if samples.size == 0:
                break
            times.append(round(consumed / float(rate), 3))
            energies.append(float(np.sqrt(np.mean(np.square(samples)))))
            consumed += samples.size
    return {
        "times": np.asarray(times, dtype=float),
        "rms": np.asarray(energies, dtype=float),
        "frame_sec": float(frame_sec),
        "duration_sec": round(float(times[-1] + frame_sec), 3) if times else 0.0,
    }


def _mean_energy(profile: dict, start_sec: float, end_sec: float):
    """Mean coarse energy over an absolute source range, or None when unknown."""
    times, rms = profile.get("times"), profile.get("rms")
    if times is None or rms is None or len(times) == 0:
        return None
    mask = (times >= float(start_sec)) & (times < float(end_sec))
    if not bool(mask.any()):
        return None
    return float(np.mean(rms[mask]))


def select_candidate_regions(profile: dict, *, source_duration_sec: float,
                             region_duration_sec: float = FULL_SCAN_REGION_DURATION_SEC,
                             region_count: int = 4) -> list[dict]:
    """Pick the regions pass 2 will transcribe, loudest first, non-overlapping.

    Falls back to regions spread evenly across the film when there is no usable
    energy profile (silent or audio-less source). The fallback is REPORTED on
    every region as `selection_policy`, never silently mistaken for a signal.
    """
    source_duration = float(source_duration_sec or 0.0)
    if source_duration <= 0:
        raise ValueError("tam kaynak taramasi icin video suresi bilinmiyor")
    region_count = max(1, int(region_count))
    region_duration = min(float(region_duration_sec), source_duration)
    if region_duration <= 0:
        raise ValueError("bolge suresi 0'dan buyuk olmali")

    times, rms = profile.get("times"), profile.get("rms")
    has_energy = (times is not None and rms is not None and len(times) > 0
                  and float(np.ptp(rms)) > 1e-9)

    if not has_energy:
        slots = min(region_count, max(1, int(source_duration // region_duration) or 1))
        span = source_duration / slots
        regions = []
        for index in range(slots):
            start = min(index * span, max(0.0, source_duration - region_duration))
            regions.append({
                "index": index + 1,
                "start_sec": round(max(0.0, start), 3),
                "duration_sec": round(min(region_duration, source_duration - start), 3),
                "mean_rms": None,
                "selection_policy": "uniform_fallback",
            })
        return regions

    stride = max(region_duration / 2.0, 1.0)
    windows = []
    start = 0.0
    while start < source_duration:
        end = min(start + region_duration, source_duration)
        if end - start >= min(region_duration, source_duration) * 0.5:
            energy = _mean_energy(profile, start, end)
            if energy is not None:
                windows.append({"start_sec": start, "end_sec": end, "mean_rms": energy})
        if end >= source_duration:
            break
        start += stride

    if not windows:
        windows = [{"start_sec": 0.0,
                    "end_sec": min(region_duration, source_duration),
                    "mean_rms": _mean_energy(profile, 0.0, region_duration)}]

    chosen: list[dict] = []
    for candidate in sorted(windows, key=lambda item: item["mean_rms"], reverse=True):
        overlaps = any(
            candidate["start_sec"] < picked["end_sec"] and picked["start_sec"] < candidate["end_sec"]
            for picked in chosen
        )
        if overlaps:
            continue
        chosen.append(candidate)
        if len(chosen) == region_count:
            break

    chosen.sort(key=lambda item: item["start_sec"])
    return [
        {
            "index": index + 1,
            "start_sec": round(float(item["start_sec"]), 3),
            "duration_sec": round(float(item["end_sec"] - item["start_sec"]), 3),
            "mean_rms": round(float(item["mean_rms"]), 6),
            "selection_policy": "audio_energy",
        }
        for index, item in enumerate(chosen)
    ]


def _merge_region_moments(region_moments: list[dict], profile: dict, top_n: int) -> list[dict]:
    """Rank candidates that were each scored INSIDE their own region.

    `score_hot_moments` min-max normalizes its components against the other
    windows of the same clip, so a 92 from a quiet region and a 92 from the
    film's loudest region do not mean the same thing. Comparing them directly
    would silently favour whichever region happened to be most internally
    varied. The merge therefore blends each candidate's region score with the
    one signal that IS absolute across the whole film -- its coarse energy from
    pass 1 -- and records both inputs on the moment so the ranking stays
    auditable. With no usable energy profile the region score is used alone and
    `global_energy_score` stays None rather than being invented.
    """
    energies = []
    for moment in region_moments:
        energies.append(_mean_energy(profile, moment["source_start_sec"], moment["source_end_sec"]))
    known = [value for value in energies if value is not None]
    spread = (max(known) - min(known)) if known else 0.0
    low = min(known) if known else 0.0

    for moment, energy in zip(region_moments, energies):
        if energy is None or spread <= 1e-12:
            moment["global_energy_score"] = None
            moment["global_score"] = round(float(moment["score"]), 2)
        else:
            energy_score = (energy - low) / spread * 100.0
            moment["global_energy_score"] = round(float(energy_score), 2)
            moment["global_score"] = round(
                (1.0 - FULL_SCAN_GLOBAL_ENERGY_WEIGHT) * float(moment["score"])
                + FULL_SCAN_GLOBAL_ENERGY_WEIGHT * float(energy_score), 2)
        moment["region_score"] = round(float(moment["score"]), 2)

    ordered = sorted(region_moments, key=lambda item: item["global_score"], reverse=True)

    selected: list[dict] = []
    for candidate in ordered:
        overlaps = any(
            candidate["source_start_sec"] < picked["source_end_sec"]
            and picked["source_start_sec"] < candidate["source_end_sec"]
            for picked in selected
        )
        if overlaps:
            continue
        selected.append(candidate)
        if len(selected) == int(top_n):
            break

    for rank, moment in enumerate(selected, start=1):
        moment["rank"] = rank
    return selected


def run_full_source_analysis(ffmpeg_bin: str, raw_video_path, runtime_dir, *,
                             source_duration_sec: float,
                             whisper_model_size: str = "large-v3-turbo",
                             whisper_device: str = "cpu", whisper_compute_type: str = "int8",
                             scene_detector: str = "content", top_n: int = 5,
                             region_duration_sec: float = FULL_SCAN_REGION_DURATION_SEC,
                             max_regions: int | None = None,
                             preview_dir=None, progress=None) -> dict:
    """Find hot moments across an ENTIRE long source, in two passes.

    `run_video_analysis` examines one contiguous window (240s by default), so on
    a two-hour film it can only ever propose moments from the few minutes the
    caller happened to point it at. Transcribing the whole film instead would be
    the obvious fix and is not affordable: Whisper dominates analysis cost and
    scales linearly with duration.

    So pass 1 scans the complete film with the cheap signal (coarse audio
    energy, one value per second, streamed) and picks the regions worth a closer
    look. Pass 2 runs the EXISTING per-window pipeline -- clip extraction,
    Whisper with word timestamps, scene detection, multi-signal scoring -- on
    those regions only. Candidates are then merged on the absolute source
    timeline, so the returned manifest describes moments from anywhere in the
    film while the expensive work stays bounded by `max_regions`.

    Returns the same hot_moments.json contract dict as `run_video_analysis`,
    plus an `analysis_scope: "full_source"` block describing what was scanned.
    """
    report = progress if progress is not None else _NullProgress()
    runtime_dir = Path(runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    source_duration = float(source_duration_sec or 0.0)
    if source_duration <= 0:
        raise ValueError("tam kaynak taramasi icin video suresi bilinmeli")
    requested_candidates = max(1, int(top_n))
    region_budget = int(max_regions or min(
        FULL_SCAN_MAX_REGIONS, max(2, requested_candidates * FULL_SCAN_REGION_MULTIPLIER)))

    # ---------------------------------------------------------- pass 1
    report.stage("full_scan", "Tum kaynak sesi taraniyor")
    _raise_if_cancelled(report)
    full_audio_path = runtime_dir / "full_source.wav"
    profile = {"times": np.asarray([]), "rms": np.asarray([]),
               "frame_sec": 1.0, "duration_sec": 0.0}
    scan_problem = None
    try:
        extract_full_audio(ffmpeg_bin, raw_video_path, full_audio_path, progress=report)
        profile = scan_audio_energy_profile(full_audio_path)
    except (RuntimeError, ValueError, OSError) as exc:
        # A source with no readable audio still deserves an analysis; it just
        # cannot be guided by loudness. Regions fall back to even coverage and
        # the manifest says so.
        scan_problem = f"{type(exc).__name__}: {exc}"
        report.note(f"ses tabanli tarama yapilamadi, bolgeler esit dagitilacak ({scan_problem})")
    finally:
        # The full-length WAV is scratch: hundreds of MB that nothing downstream
        # reads. Region clips carry their own audio.
        try:
            full_audio_path.unlink(missing_ok=True)
        except OSError:
            pass

    regions = select_candidate_regions(
        profile, source_duration_sec=source_duration,
        region_duration_sec=region_duration_sec, region_count=region_budget)
    report.count(0, len(regions), unit="bolge",
                 message=f"{len(regions)} aday bolge secildi")

    # ---------------------------------------------------------- pass 2
    region_dir = runtime_dir / "regions"
    region_dir.mkdir(parents=True, exist_ok=True)
    collected: list[dict] = []
    transcript_available = False
    transcript_language = None
    transcript_language_probability = None
    region_reports: list[dict] = []

    for done, region in enumerate(regions, start=1):
        _raise_if_cancelled(report)
        report.stage("region_analysis",
                     f"Bolge {done}/{len(regions)} inceleniyor "
                     f"({int(region['start_sec'] // 60)}. dakika)")
        clip_path = region_dir / f"region_{region['index']:02d}.mp4"
        audio_path = region_dir / f"region_{region['index']:02d}.wav"
        entry = dict(region)
        try:
            extract_clip_and_audio(ffmpeg_bin, raw_video_path, clip_path, audio_path,
                                   region["start_sec"], region["duration_sec"], progress=report)
            text, segments, transcript_info = transcribe_audio(
                audio_path, model_size=whisper_model_size, device=whisper_device,
                compute_type=whisper_compute_type, return_info=True, progress=report)
            if text.strip():
                transcript_available = True
                if transcript_language is None:
                    transcript_language = transcript_info.get("language")
                    transcript_language_probability = transcript_info.get("language_probability")
            _raise_if_cancelled(report)
            scenes = detect_scenes(clip_path, detector=scene_detector)
            scene_boundaries = [end for _, end in scenes[:-1]] if scenes else []
            audio_info = compute_audio_peaks(audio_path)
            times, rms = audio_info["times"], audio_info["rms"]
            analysis_duration = min(float(region["duration_sec"]),
                                    float(times[-1]) if len(times) else 0.0)
            moments = score_hot_moments(
                segments, times, rms, scene_boundaries, analysis_duration,
                region["start_sec"], top_n=FULL_SCAN_MOMENTS_PER_REGION)
            for moment in moments:
                moment["analysis_region"] = {
                    "index": region["index"],
                    "start_sec": region["start_sec"],
                    "duration_sec": region["duration_sec"],
                }
            collected.extend(moments)
            entry.update({"scene_count": len(scenes), "candidate_count": len(moments),
                          "transcript_chars": len(text.strip()), "problem": None})
        except (RuntimeError, ValueError, OSError) as exc:
            # One unreadable region must not throw away the regions that worked.
            entry.update({"scene_count": 0, "candidate_count": 0, "transcript_chars": 0,
                          "problem": f"{type(exc).__name__}: {exc}"})
            report.note(f"bolge {region['index']} incelenemedi: {entry['problem']}")
        finally:
            for scratch in (clip_path, audio_path):
                try:
                    scratch.unlink(missing_ok=True)
                except OSError:
                    pass
        region_reports.append(entry)
        report.count(done, len(regions), unit="bolge",
                     message=f"{done}/{len(regions)} bolge incelendi")

    if not collected:
        raise RuntimeError(
            "tam kaynak taramasinda hicbir hot-moment adayi bulunamadi "
            f"({len(regions)} bolge tarandi)")

    # ---------------------------------------------------------- merge
    report.stage("scoring", "Adaylar film genelinde siralaniyor")
    _raise_if_cancelled(report)
    hot_moments = _merge_region_moments(collected, profile, requested_candidates)

    report.stage("previews", f"{len(hot_moments)} onizleme klibi uretiliyor")
    _raise_if_cancelled(report)
    generate_preview_clips(ffmpeg_bin, raw_video_path, hot_moments,
                           preview_dir or (runtime_dir / "hot_moment_previews"),
                           progress=report, time_field="source")

    manifest = build_hot_moments_manifest(
        hot_moments, transcript_candidate=f"faster-whisper-{whisper_model_size}",
        window_sec=30.0, stride_sec=10.0, weights=DEFAULT_WEIGHTS,
        raw_video_path=raw_video_path, analysis_clip_path=None,
        clip_start_sec=0.0, clip_duration_sec=round(source_duration, 3),
        candidate_request={
            "requested": requested_candidates,
            "produced": len(hot_moments),
            "policy": "full-source-two-pass-v1",
            "analysis_duration_sec": round(source_duration, 3),
            "window_sec": 30.0,
            "stride_sec": 10.0,
            "complete": len(hot_moments) == requested_candidates,
        },
    )
    manifest["method"] = "full-source-two-pass-v1"
    manifest["analysis_scope"] = "full_source"
    manifest["full_source_scan"] = {
        "source_duration_sec": round(source_duration, 3),
        "coarse_pass": {
            "signal": "audio_rms_1s",
            "problem": scan_problem,
            "frame_count": int(len(profile["times"])) if profile.get("times") is not None else 0,
        },
        "region_duration_sec": round(float(region_duration_sec), 3),
        "region_budget": region_budget,
        "regions_scanned": len(regions),
        "regions_with_candidates": sum(1 for item in region_reports if item.get("candidate_count")),
        "regions": region_reports,
        "candidates_before_merge": len(collected),
        "ranking_policy": "full-source-energy-blend-v1",
        "ranking_notes": (
            "score_hot_moments normalizes its components within one region, so region "
            "scores are not comparable across regions. Final ordering blends the region "
            f"score with the globally comparable coarse energy at weight "
            f"{FULL_SCAN_GLOBAL_ENERGY_WEIGHT}; both inputs are kept on each moment as "
            "region_score and global_energy_score. Without a usable energy profile the "
            "region score is used alone and global_energy_score is null."
        ),
        "timestamp_notes": (
            "aligned_*_sec are offsets into the region clip that scored the moment "
            "(see analysis_region); that clip is scratch and is deleted after analysis. "
            "source_*_sec are absolute offsets into raw_video_path and are what render "
            "and preview use."
        ),
    }
    manifest["transcript_available"] = transcript_available
    manifest["transcript_language"] = transcript_language
    manifest["transcript_language_probability"] = transcript_language_probability
    if not transcript_available:
        manifest["status_reason"] += "; WARNING: transcript was empty (no speech detected)"
    return manifest


class _NullProgress:
    """No-op observer, so the notebook call paths stay untouched."""

    def stage(self, key, message="") -> None: ...
    def count(self, completed, total, *, unit="", message="") -> None: ...
    def note(self, message: str) -> None: ...
    def raise_if_cancelled(self) -> None: ...


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
    _raise_if_cancelled(report)
    extract_clip_and_audio(ffmpeg_bin, raw_video_path, video_path, audio_path,
                           clip_start_sec, clip_duration_sec, progress=report)
    report.stage("audio", "Ses akisi hazir")

    report.stage("whisper", f"faster-whisper-{whisper_model_size} calisiyor")
    text, segments, transcript_info = transcribe_audio(
        audio_path, model_size=whisper_model_size,
        device=whisper_device, compute_type=whisper_compute_type,
        return_info=True, progress=report,
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
    _raise_if_cancelled(report)
    scenes = detect_scenes(video_path, detector=scene_detector)
    _raise_if_cancelled(report)
    scene_boundaries = [end for _, end in scenes[:-1]] if scenes else []
    report.count(len(scenes), len(scenes), unit="sahne",
                 message=f"{len(scenes)} sahne bulundu")

    report.stage("scoring", "Ses enerjisi ve moment skorlari")
    _raise_if_cancelled(report)
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
    _raise_if_cancelled(report)
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
