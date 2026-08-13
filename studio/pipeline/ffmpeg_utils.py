from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from django.conf import settings

from .. import preflight
from . import PipelineStageError


def discover_ffmpeg() -> Path:
    configured = getattr(settings, "FFMPEG_PATH", None)
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return path.resolve()
        raise PipelineStageError(
            "FFMPEG_NOT_FOUND", "The configured FFmpeg executable does not exist."
        )

    discovered = shutil.which("ffmpeg")
    if discovered and Path(discovered).is_file():
        return Path(discovered).resolve()

    try:
        ffprobe = preflight.discover_ffprobe()
    except preflight.PreflightError:
        ffprobe = None
    if ffprobe is not None:
        candidate = ffprobe.with_name("ffmpeg.exe")
        if candidate.is_file():
            return candidate.resolve()

    raise PipelineStageError(
        "FFMPEG_NOT_FOUND",
        "FFmpeg was not found. Install the approved WinGet FFmpeg Shared package "
        "or configure FFMPEG_PATH.",
    )


def run_ffmpeg(args: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess:
    """Run ffmpeg with an argument array (no shell) and raise on failure/timeout."""
    executable = discover_ffmpeg()
    command = [str(executable), "-y", *args]
    resolved_timeout = timeout or getattr(settings, "FFMPEG_TIMEOUT_SECONDS", 600)
    try:
        completed = subprocess.run(
            command,
            shell=False,
            capture_output=True,
            text=True,
            timeout=resolved_timeout,
            check=False,
            **preflight._subprocess_options(),
        )
    except subprocess.TimeoutExpired as exc:
        raise PipelineStageError(
            "FFMPEG_TIMEOUT", f"ffmpeg did not finish within {resolved_timeout} seconds."
        ) from exc
    except OSError as exc:
        raise PipelineStageError("FFMPEG_FAILED", f"ffmpeg could not be started: {exc}") from exc

    if completed.returncode != 0:
        raise PipelineStageError(
            "FFMPEG_FAILED",
            f"ffmpeg exited with code {completed.returncode}: "
            f"{(completed.stderr or '').strip()[-500:]}",
        )
    return completed
