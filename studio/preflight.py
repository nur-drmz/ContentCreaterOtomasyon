from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path, PureWindowsPath
from typing import Any

from django.conf import settings

from .models import SourceAsset
from .services import resolve_library_video


@dataclass(frozen=True)
class PreflightError(Exception):
    code: str
    detail: str

    def __str__(self) -> str:
        return self.detail


def validate_source_path(source: SourceAsset) -> tuple[Path, dict[str, Any]]:
    raw_path = source.relative_path
    windows_path = PureWindowsPath(raw_path)
    if Path(raw_path).is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise PreflightError(
            "SOURCE_OUTSIDE_LIBRARY",
            "The registered source path must be relative to the approved library.",
        )
    if any(":" in part for part in windows_path.parts):
        raise PreflightError(
            "SOURCE_OUTSIDE_LIBRARY",
            "The registered source path contains a disallowed path component.",
        )
    try:
        path = resolve_library_video(raw_path)
    except ValueError as exc:
        message = str(exc)
        if "type" in message:
            code = "SOURCE_TYPE_UNSUPPORTED"
        elif "outside" in message:
            code = "SOURCE_OUTSIDE_LIBRARY"
        else:
            code = "SOURCE_NOT_FOUND"
        raise PreflightError(code, message) from exc
    except OSError as exc:
        raise PreflightError(
            "SOURCE_NOT_FOUND",
            "The registered source video could not be accessed.",
        ) from exc

    try:
        stat = path.stat()
    except OSError as exc:
        raise PreflightError(
            "SOURCE_NOT_FOUND",
            "The registered source video could not be accessed.",
        ) from exc
    return path, {
        "relative_path": raw_path,
        "extension": path.suffix.lower(),
        "size_bytes": stat.st_size,
    }


def inspect_source_size(source: SourceAsset) -> dict[str, Any]:
    _path, result = validate_source_path(source)
    size_bytes = result["size_bytes"]
    maximum = settings.MAX_SOURCE_SIZE_BYTES
    if size_bytes > maximum:
        raise PreflightError(
            "SOURCE_TOO_LARGE",
            "The source exceeds the 20 GB Pre-Alpha limit.",
        )
    return {"size_bytes": size_bytes, "maximum_size_bytes": maximum}


def discover_ffprobe() -> Path:
    configured = getattr(settings, "FFPROBE_PATH", None)
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return path.resolve()
        raise PreflightError(
            "FFPROBE_NOT_FOUND",
            "The configured FFprobe executable does not exist.",
        )

    discovered = shutil.which("ffprobe")
    if discovered and Path(discovered).is_file():
        return Path(discovered).resolve()

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        package_root = (
            Path(local_app_data)
            / "Microsoft"
            / "WinGet"
            / "Packages"
            / "Gyan.FFmpeg.Shared_Microsoft.Winget.Source_8wekyb3d8bbwe"
        )
        matches = sorted(package_root.glob("ffmpeg-*/bin/ffprobe.exe"), reverse=True)
        for match in matches:
            if match.is_file():
                return match.resolve()

    raise PreflightError(
        "FFPROBE_NOT_FOUND",
        "FFprobe was not found. Install the approved WinGet FFmpeg Shared package "
        "or configure FFPROBE_PATH.",
    )


def _subprocess_options() -> dict[str, Any]:
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        options["startupinfo"] = startupinfo
    return options


def _parse_positive_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise PreflightError(
            "SOURCE_DURATION_INVALID",
            "FFprobe did not report a valid positive source duration.",
        ) from exc
    if parsed <= 0:
        raise PreflightError(
            "SOURCE_DURATION_INVALID",
            "FFprobe did not report a valid positive source duration.",
        )
    return parsed


def _parse_frame_rate(value: Any) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        return round(float(Fraction(str(value))), 6)
    except (ValueError, ZeroDivisionError):
        return None


def inspect_with_ffprobe(source_path: Path, executable: Path | None = None) -> dict[str, Any]:
    executable = executable or discover_ffprobe()
    timeout = settings.FFPROBE_TIMEOUT_SECONDS
    command = [
        str(executable),
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(source_path),
    ]
    try:
        completed = subprocess.run(
            command,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            **_subprocess_options(),
        )
    except subprocess.TimeoutExpired as exc:
        raise PreflightError(
            "FFPROBE_TIMEOUT",
            f"FFprobe did not finish within {timeout} seconds.",
        ) from exc
    except OSError as exc:
        raise PreflightError(
            "FFPROBE_FAILED",
            "FFprobe could not inspect the registered source.",
        ) from exc

    if completed.returncode != 0:
        raise PreflightError(
            "FFPROBE_FAILED",
            "FFprobe could not read the source media. Verify that the file is accessible "
            "and not corrupt.",
        )

    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise PreflightError(
            "FFPROBE_INVALID_JSON",
            "FFprobe returned malformed metadata.",
        ) from exc

    streams = payload.get("streams")
    media_format = payload.get("format")
    if not isinstance(streams, list) or not isinstance(media_format, dict):
        raise PreflightError("FFPROBE_INVALID_JSON", "FFprobe metadata was incomplete.")
    video_streams = [item for item in streams if item.get("codec_type") == "video"]
    audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
    subtitle_streams = [item for item in streams if item.get("codec_type") == "subtitle"]
    if not video_streams:
        raise PreflightError(
            "VIDEO_STREAM_MISSING",
            "The selected source does not contain a video stream.",
        )

    duration = _parse_positive_float(media_format.get("duration"))
    if duration > settings.MAX_SOURCE_DURATION_SECONDS:
        raise PreflightError(
            "SOURCE_TOO_LONG",
            "The source exceeds the 180-minute Pre-Alpha duration limit.",
        )

    video = video_streams[0]
    audio = audio_streams[0] if audio_streams else {}
    format_name = str(media_format.get("format_name") or "")
    try:
        reported_size = int(media_format.get("size") or source_path.stat().st_size)
    except (TypeError, ValueError, OSError):
        reported_size = 0

    version = ""
    try:
        version_result = subprocess.run(
            [str(executable), "-version"],
            shell=False,
            capture_output=True,
            text=True,
            timeout=min(timeout, 5),
            check=False,
            **_subprocess_options(),
        )
        if version_result.returncode == 0:
            version = (version_result.stdout or "").splitlines()[0][:160]
    except (OSError, subprocess.TimeoutExpired):
        pass

    return {
        "container": format_name,
        "duration_seconds": duration,
        "size_bytes": reported_size,
        "video_codec": str(video.get("codec_name") or ""),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "frame_rate": _parse_frame_rate(video.get("avg_frame_rate")),
        "audio_codec": str(audio.get("codec_name") or ""),
        "audio_sample_rate": int(audio.get("sample_rate") or 0),
        "audio_channels": int(audio.get("channels") or 0),
        "subtitle_stream_count": len(subtitle_streams),
        "stream_count": len(streams),
        "video_stream_count": len(video_streams),
        "audio_stream_count": len(audio_streams),
        "ffprobe_executable": executable.name,
        "ffprobe_version": version,
    }


def inspect_source_media(source: SourceAsset) -> dict[str, Any]:
    path, _path_result = validate_source_path(source)
    inspect_source_size(source)
    metadata = inspect_with_ffprobe(path)
    container_tokens = set(metadata["container"].split(","))
    expected_tokens = (
        {"mov", "mp4"}
        if path.suffix.lower() in {".mp4", ".mov"}
        else {"matroska"}
    )
    if not container_tokens.intersection(expected_tokens):
        raise PreflightError(
            "SOURCE_TYPE_UNSUPPORTED",
            "The source container reported by FFprobe does not match a supported "
            "MP4, MKV or MOV file.",
        )
    source.duration_seconds = metadata["duration_seconds"]
    source.container = metadata["container"].split(",")[0][:24]
    source.size_bytes = path.stat().st_size
    source.probe_metadata = metadata
    source.save(
        update_fields=[
            "duration_seconds",
            "container",
            "size_bytes",
            "probe_metadata",
        ]
    )
    return metadata


def _nearest_existing_path(configured_root: Path) -> Path:
    candidate = configured_root.expanduser()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if not candidate.exists():
        raise OSError("No existing parent was found.")
    return candidate


def inspect_storage() -> dict[str, Any]:
    checks = [
        (
            "generated_media",
            Path(settings.GENERATED_MEDIA_ROOT),
            settings.MIN_GENERATED_FREE_BYTES,
            "GENERATED_STORAGE_UNAVAILABLE",
            "GENERATED_STORAGE_LOW",
        ),
        (
            "model_storage",
            Path(settings.MODEL_ROOT),
            settings.MIN_MODEL_FREE_BYTES,
            "MODEL_STORAGE_UNAVAILABLE",
            "MODEL_STORAGE_LOW",
        ),
    ]
    result: dict[str, Any] = {}
    for key, configured_root, required, unavailable_code, low_code in checks:
        try:
            inspected_root = _nearest_existing_path(configured_root)
            usage = shutil.disk_usage(inspected_root)
        except OSError as exc:
            raise PreflightError(
                unavailable_code,
                f"The configured {key.replace('_', ' ')} location is unavailable.",
            ) from exc
        item = {
            "root": str(configured_root),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "required_free_bytes": required,
            "passed": usage.free >= required,
        }
        result[key] = item
        if not item["passed"]:
            raise PreflightError(
                low_code,
                f"The {key.replace('_', ' ')} location needs at least "
                f"{required / 1024**3:.0f} GB free.",
            )
    return result


def inspect_cuda() -> dict[str, Any]:
    try:
        import torch

        if not torch.cuda.is_available():
            raise PreflightError(
                "CUDA_UNAVAILABLE",
                "CUDA is unavailable. Verify the NVIDIA driver and local PyTorch installation.",
            )
        device = torch.cuda.get_device_properties(0)
        total_bytes = int(device.total_memory)
        try:
            free_bytes, runtime_total = torch.cuda.mem_get_info(0)
            free_bytes = int(free_bytes)
            total_bytes = int(runtime_total)
        except (AttributeError, RuntimeError):
            free_bytes = total_bytes - int(torch.cuda.memory_reserved(0))
        result = {
            "torch_version": str(torch.__version__),
            "cuda_runtime_version": str(torch.version.cuda or ""),
            "cuda_available": True,
            "gpu_name": str(device.name),
            "total_vram_bytes": total_bytes,
            "free_vram_bytes": free_bytes,
            "required_free_vram_bytes": settings.MIN_FREE_VRAM_BYTES,
        }
        if free_bytes < settings.MIN_FREE_VRAM_BYTES:
            raise PreflightError(
                "GPU_VRAM_LOW",
                "At least 8 GB of free GPU memory is required. Close GPU-heavy "
                "applications and try again; the worker will not terminate them.",
            )
        return result
    except PreflightError:
        raise
    except Exception as exc:
        raise PreflightError(
            "CUDA_INSPECTION_FAILED",
            "CUDA device information could not be inspected safely.",
        ) from exc
