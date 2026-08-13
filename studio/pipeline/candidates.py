from __future__ import annotations

from typing import Any

_MIN_WINDOW_SECONDS = 8.0
_MAX_WINDOW_SECONDS = 25.0
_OVERSAMPLE_FACTOR = 2.5


def adaptive_target_count(duration_seconds: float) -> int:
    """20/30/40 candidates by source duration, per docs/pre-alpha-local/03-media-ai-pipeline.md §5."""
    minutes = duration_seconds / 60.0
    if minutes <= 60:
        return 20
    if minutes <= 120:
        return 30
    return 40


def _merge_scenes_into_windows(scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for index, scene in enumerate(scenes):
        start_ts = float(scene["start_ts"])
        end_ts = float(scene["end_ts"])
        if current is None:
            current = {"start_ts": start_ts, "end_ts": end_ts, "scene_indices": [index]}
            continue
        if current["end_ts"] - current["start_ts"] < _MIN_WINDOW_SECONDS:
            current["end_ts"] = end_ts
            current["scene_indices"].append(index)
            continue
        windows.append(current)
        current = {"start_ts": start_ts, "end_ts": end_ts, "scene_indices": [index]}
    if current is not None:
        windows.append(current)

    # Split any window that grew past the max span into roughly even halves.
    split_windows: list[dict[str, Any]] = []
    for window in windows:
        span = window["end_ts"] - window["start_ts"]
        if span <= _MAX_WINDOW_SECONDS:
            split_windows.append(window)
            continue
        midpoint = window["start_ts"] + span / 2
        split_windows.append(
            {
                "start_ts": window["start_ts"],
                "end_ts": midpoint,
                "scene_indices": window["scene_indices"],
            }
        )
        split_windows.append(
            {
                "start_ts": midpoint,
                "end_ts": window["end_ts"],
                "scene_indices": window["scene_indices"],
            }
        )
    return split_windows


def _transcript_excerpt(transcript_segments: list[dict[str, Any]], start_ts: float, end_ts: float) -> str:
    overlapping = [
        segment["text"]
        for segment in transcript_segments
        if segment["start_ts"] < end_ts and segment["end_ts"] > start_ts
    ]
    return " ".join(overlapping).strip()


def _dialogue_density(transcript_excerpt: str, duration: float) -> float:
    if duration <= 0:
        return 0.0
    word_count = len(transcript_excerpt.split())
    return min(word_count / duration, 5.0) / 5.0


def _scene_signal_mean(per_scene: dict[str, dict[str, float]], scene_indices: list[int], key: str) -> float:
    values = [per_scene.get(str(i), {}).get(key, 0.0) for i in scene_indices]
    return sum(values) / len(values) if values else 0.0


def _normalize(values: list[float]) -> list[float]:
    if not values:
        return []
    highest = max(values)
    if highest <= 0:
        return [0.0 for _ in values]
    return [value / highest for value in values]


def generate_pre_candidates(
    *,
    transcript_segments: list[dict[str, Any]],
    scenes: list[dict[str, Any]],
    audio_features: dict[str, Any],
    motion_features: dict[str, Any],
    source_duration_seconds: float,
) -> list[dict[str, Any]]:
    """Merge scene boundaries + transcript/audio/motion signals into candidate windows."""
    windows = _merge_scenes_into_windows(scenes)
    audio_per_scene = audio_features.get("per_scene", {})
    motion_per_scene = motion_features.get("per_scene", {})

    raw_candidates: list[dict[str, Any]] = []
    for window in windows:
        start_ts = window["start_ts"]
        end_ts = window["end_ts"]
        duration = end_ts - start_ts
        excerpt = _transcript_excerpt(transcript_segments, start_ts, end_ts)
        audio_signal = _scene_signal_mean(audio_per_scene, window["scene_indices"], "onset_strength_mean")
        motion_signal = _scene_signal_mean(motion_per_scene, window["scene_indices"], "motion_mean")
        raw_candidates.append(
            {
                "start_ts": start_ts,
                "end_ts": end_ts,
                "scene_indices": window["scene_indices"],
                "transcript_excerpt": excerpt,
                "dialogue_density": _dialogue_density(excerpt, duration),
                "audio_signal_raw": audio_signal,
                "motion_signal_raw": motion_signal,
            }
        )

    normalized_audio = _normalize([c["audio_signal_raw"] for c in raw_candidates])
    normalized_motion = _normalize([c["motion_signal_raw"] for c in raw_candidates])
    for candidate, audio_norm, motion_norm in zip(raw_candidates, normalized_audio, normalized_motion, strict=True):
        candidate["audio_signal"] = audio_norm
        candidate["motion_signal"] = motion_norm
        candidate["heuristic_score"] = (
            candidate["dialogue_density"] + audio_norm + motion_norm
        ) / 3.0
        del candidate["audio_signal_raw"]
        del candidate["motion_signal_raw"]

    raw_candidates.sort(key=lambda item: item["heuristic_score"], reverse=True)
    target_count = adaptive_target_count(source_duration_seconds)
    oversample_count = max(target_count, int(target_count * _OVERSAMPLE_FACTOR))
    return raw_candidates[:oversample_count]
