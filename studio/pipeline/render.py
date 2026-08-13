from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from django.conf import settings

from . import PipelineStageError
from .ffmpeg_utils import run_ffmpeg

RENDER_WIDTH = 720
RENDER_HEIGHT = 1280
RENDER_CONFIG_VERSION = "rough-cut-v1"

_COMPOSITE_FILTER = (
    "[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
    "crop={width}:{height},gblur=sigma=20,eq=brightness=-0.12[bg];"
    "[0:v]scale={width}:-2:force_original_aspect_ratio=decrease[fg];"
    "[bg][fg]overlay=(W-w)/2:(H-h)/2[vout]"
)


def output_path_for(project_id: str, version_number: int) -> Path:
    return (
        Path(settings.GENERATED_MEDIA_ROOT)
        / "outputs"
        / str(project_id)
        / f"rough_cut_v{version_number}.mp4"
    )


def _render_single_clip(source_path: Path, clip: dict[str, Any], output_path: Path) -> None:
    duration = clip["end_ts"] - clip["start_ts"]
    if duration <= 0:
        raise PipelineStageError("RENDER_INVALID_CLIP", "A timeline clip has a non-positive duration.")

    filter_complex = _COMPOSITE_FILTER.format(width=RENDER_WIDTH, height=RENDER_HEIGHT)
    run_ffmpeg(
        [
            "-ss",
            f"{clip['start_ts']:.3f}",
            "-i",
            str(source_path),
            "-t",
            f"{duration:.3f}",
            "-filter_complex",
            filter_complex,
            "-map",
            "[vout]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-c:a",
            "aac",
            str(output_path),
        ]
    )


def _write_srt(captions: list[dict[str, Any]], srt_path: Path) -> None:
    def _format_time(seconds: float) -> str:
        millis = round(seconds * 1000)
        hh, remainder = divmod(millis, 3_600_000)
        mm, remainder = divmod(remainder, 60_000)
        ss, ms = divmod(remainder, 1000)
        return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"

    lines = []
    for index, caption in enumerate(captions, start=1):
        lines.append(str(index))
        lines.append(f"{_format_time(caption['start_ts'])} --> {_format_time(caption['end_ts'])}")
        lines.append(caption["text"])
        lines.append("")
    srt_path.write_text("\n".join(lines), encoding="utf-8")


def render_rough_cut(
    *,
    source_path: Path,
    clips: list[dict[str, Any]],
    output_path: Path,
    captions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Render clips into one normalized, blurred-background 720x1280 rough cut."""
    if not clips:
        raise PipelineStageError("RENDER_NO_CLIPS", "The timeline has no clips to render.")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        clip_paths: list[Path] = []
        for index, clip in enumerate(clips):
            clip_output = tmp_path / f"clip_{index:02d}.mp4"
            _render_single_clip(source_path, clip, clip_output)
            clip_paths.append(clip_output)

        concat_list = tmp_path / "concat.txt"
        concat_list.write_text(
            "\n".join(f"file '{path.as_posix()}'" for path in clip_paths), encoding="utf-8"
        )

        audio_filters = ["loudnorm"]
        video_args: list[str] = []
        srt_path = tmp_path / "captions.srt"
        if captions:
            _write_srt(captions, srt_path)
            escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")
            video_args = ["-vf", f"subtitles={escaped}"]

        args = ["-f", "concat", "-safe", "0", "-i", str(concat_list)]
        if video_args:
            args += video_args + ["-c:v", "libx264", "-preset", "veryfast"]
        else:
            args += ["-c:v", "copy"]
        args += ["-af", ",".join(audio_filters), "-c:a", "aac", str(output_path)]
        run_ffmpeg(args)

    total_duration = sum(clip["end_ts"] - clip["start_ts"] for clip in clips)
    return {
        "relative_path": str(output_path),
        "width": RENDER_WIDTH,
        "height": RENDER_HEIGHT,
        "codec": "h264/aac",
        "duration_seconds": total_duration,
    }
