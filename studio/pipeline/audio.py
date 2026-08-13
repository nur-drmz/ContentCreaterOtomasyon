from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from . import PipelineStageError


def analyze_audio(source_path: Path, scenes: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute RMS/onset-strength features per scene using librosa."""
    try:
        import librosa
    except ImportError as exc:  # pragma: no cover - dependency guaranteed by pyproject
        raise PipelineStageError(
            "LIBROSA_UNAVAILABLE", f"librosa is not installed: {exc}"
        ) from exc

    try:
        waveform, sample_rate = librosa.load(str(source_path), sr=22050, mono=True)
    except Exception as exc:  # noqa: BLE001 - untrusted media, never crash the worker
        raise PipelineStageError("AUDIO_LOAD_FAILED", f"Could not decode audio: {exc}") from exc

    rms = librosa.feature.rms(y=waveform)[0]
    onset_env = librosa.onset.onset_strength(y=waveform, sr=sample_rate)
    hop_length = 512
    frame_times = librosa.frames_to_time(np.arange(len(rms)), sr=sample_rate, hop_length=hop_length)
    onset_times = librosa.frames_to_time(
        np.arange(len(onset_env)), sr=sample_rate, hop_length=hop_length
    )

    per_scene: dict[str, dict[str, float]] = {}
    for index, scene in enumerate(scenes):
        start_ts = float(scene["start_ts"])
        end_ts = float(scene["end_ts"])
        rms_mask = (frame_times >= start_ts) & (frame_times < end_ts)
        onset_mask = (onset_times >= start_ts) & (onset_times < end_ts)
        scene_rms = rms[rms_mask]
        scene_onset = onset_env[onset_mask]
        per_scene[str(index)] = {
            "rms_mean": float(scene_rms.mean()) if scene_rms.size else 0.0,
            "rms_max": float(scene_rms.max()) if scene_rms.size else 0.0,
            "onset_strength_mean": float(scene_onset.mean()) if scene_onset.size else 0.0,
            "onset_strength_max": float(scene_onset.max()) if scene_onset.size else 0.0,
        }

    overall_max_rms = float(rms.max()) if rms.size else 0.0
    return {"per_scene": per_scene, "overall_max_rms": overall_max_rms, "sample_rate": sample_rate}
