from __future__ import annotations

from pathlib import Path

from django.conf import settings

from . import PipelineStageError
from .ffmpeg_utils import run_ffmpeg


def proxy_path_for(project_id: str) -> Path:
    return Path(settings.GENERATED_MEDIA_ROOT) / "cache" / str(project_id) / "proxy.mp4"


def create_proxy(source_path: Path, project_id: str) -> dict[str, object]:
    """Create (or reuse) a fast-to-decode downscaled proxy for analysis stages."""
    output_path = proxy_path_for(project_id)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists() and output_path.stat().st_size > 0:
        return {"proxy_relative_path": str(output_path), "reused": True}

    max_height = getattr(settings, "PROXY_MAX_HEIGHT", 480)
    scale_filter = f"scale=-2:{max_height}"
    tmp_output = output_path.with_suffix(".tmp.mp4")
    try:
        run_ffmpeg(
            [
                "-i",
                str(source_path),
                "-vf",
                scale_filter,
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "28",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                str(tmp_output),
            ]
        )
    except PipelineStageError:
        tmp_output.unlink(missing_ok=True)
        raise

    tmp_output.replace(output_path)
    return {"proxy_relative_path": str(output_path), "reused": False}
