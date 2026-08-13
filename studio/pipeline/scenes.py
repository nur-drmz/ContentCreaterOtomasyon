from __future__ import annotations

from pathlib import Path
from typing import Any

from . import PipelineStageError


def detect_scenes(proxy_path: Path) -> list[dict[str, Any]]:
    """Detect shot/scene boundaries on the proxy using PySceneDetect's ContentDetector."""
    try:
        from scenedetect import ContentDetector, SceneManager, open_video
    except ImportError as exc:  # pragma: no cover - dependency guaranteed by pyproject
        raise PipelineStageError(
            "SCENEDETECT_UNAVAILABLE", f"PySceneDetect is not installed: {exc}"
        ) from exc

    try:
        video = open_video(str(proxy_path))
        scene_manager = SceneManager()
        scene_manager.add_detector(ContentDetector())
        scene_manager.detect_scenes(video)
        scene_list = scene_manager.get_scene_list()
    except Exception as exc:  # noqa: BLE001 - untrusted media, never crash the worker
        raise PipelineStageError(
            "SCENE_DETECTION_FAILED", f"Scene detection failed: {exc}"
        ) from exc

    scenes: list[dict[str, Any]] = []
    for start, end in scene_list:
        start_ts = start.get_seconds()
        end_ts = end.get_seconds()
        if end_ts <= start_ts:
            continue
        scenes.append({"start_ts": start_ts, "end_ts": end_ts})

    if not scenes:
        duration = video.duration.get_seconds() if getattr(video, "duration", None) else 0.0
        if duration > 0:
            scenes.append({"start_ts": 0.0, "end_ts": duration})

    return scenes
