from __future__ import annotations

from pathlib import Path
from typing import Any

from . import PipelineStageError


def extract_keyframes(proxy_path: Path, start_ts: float, end_ts: float, count: int = 4) -> list[Any]:
    """Sample `count` evenly spaced RGB frames (as PIL Images) from the proxy."""
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - dependency guaranteed by pyproject
        raise PipelineStageError("OPENCV_UNAVAILABLE", f"OpenCV is not installed: {exc}") from exc
    try:
        from PIL import Image
    except ImportError as exc:
        raise PipelineStageError("PILLOW_UNAVAILABLE", f"Pillow is not installed: {exc}") from exc

    capture = cv2.VideoCapture(str(proxy_path))
    if not capture.isOpened():
        raise PipelineStageError(
            "PROXY_OPEN_FAILED", "Could not open the proxy video for keyframe extraction."
        )

    try:
        duration = max(end_ts - start_ts, 0.01)
        divisor = max(count - 1, 1)
        frames: list[Any] = []
        for position in range(count):
            sample_time = start_ts + duration * position / divisor
            capture.set(cv2.CAP_PROP_POS_MSEC, sample_time * 1000)
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frames.append(Image.fromarray(rgb))
        return frames
    finally:
        capture.release()
