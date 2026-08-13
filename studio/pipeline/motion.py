from __future__ import annotations

from pathlib import Path
from typing import Any

from . import PipelineStageError

_SAMPLE_FRAMES_PER_SCENE = 6


def analyze_motion(proxy_path: Path, scenes: list[dict[str, Any]]) -> dict[str, Any]:
    """Estimate motion intensity per scene from the proxy using OpenCV frame differencing."""
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - dependency guaranteed by pyproject
        raise PipelineStageError("OPENCV_UNAVAILABLE", f"OpenCV is not installed: {exc}") from exc

    capture = cv2.VideoCapture(str(proxy_path))
    if not capture.isOpened():
        raise PipelineStageError("PROXY_OPEN_FAILED", "Could not open the proxy video for motion analysis.")

    try:
        fps = capture.get(cv2.CAP_PROP_FPS) or 0.0
        per_scene: dict[str, dict[str, float]] = {}
        for index, scene in enumerate(scenes):
            start_ts = float(scene["start_ts"])
            end_ts = float(scene["end_ts"])
            duration = max(end_ts - start_ts, 0.01)
            sample_count = min(_SAMPLE_FRAMES_PER_SCENE, max(2, int(duration)))
            sample_times = [
                start_ts + (duration * position / sample_count) for position in range(sample_count)
            ]

            previous_gray = None
            diffs: list[float] = []
            for sample_time in sample_times:
                capture.set(cv2.CAP_PROP_POS_MSEC, sample_time * 1000)
                ok, frame = capture.read()
                if not ok or frame is None:
                    continue
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                gray = cv2.resize(gray, (160, 90))
                if previous_gray is not None:
                    diff = cv2.absdiff(gray, previous_gray)
                    diffs.append(float(diff.mean()))
                previous_gray = gray

            per_scene[str(index)] = {
                "motion_mean": (sum(diffs) / len(diffs)) if diffs else 0.0,
                "motion_max": max(diffs) if diffs else 0.0,
                "sampled_frames": len(sample_times),
            }
        return {"per_scene": per_scene, "fps": fps}
    finally:
        capture.release()
