from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from django.conf import settings

from . import PipelineStageError
from .ffmpeg_utils import run_ffmpeg

_SRT_TIME = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


def _srt_timestamp_to_seconds(hh: str, mm: str, ss: str, ms: str) -> float:
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0


def parse_srt(text: str) -> list[dict[str, Any]]:
    """Parse SRT text into a list of {start_ts, end_ts, text} dicts."""
    segments: list[dict[str, Any]] = []
    blocks = re.split(r"\r?\n\r?\n", text.strip())
    for block in blocks:
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        time_line_index = None
        for index, line in enumerate(lines):
            if _SRT_TIME.search(line):
                time_line_index = index
                break
        if time_line_index is None:
            continue
        match = _SRT_TIME.search(lines[time_line_index])
        if not match:
            continue
        start_ts = _srt_timestamp_to_seconds(*match.groups()[0:4])
        end_ts = _srt_timestamp_to_seconds(*match.groups()[4:8])
        body = " ".join(lines[time_line_index + 1 :]).strip()
        if end_ts <= start_ts or not body:
            continue
        segments.append({"start_ts": start_ts, "end_ts": end_ts, "text": body})
    return segments


def _sidecar_path(source_path: Path) -> Path:
    return source_path.with_suffix(".srt")


def _extract_embedded_subtitles(source_path: Path) -> list[dict[str, Any]] | None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        srt_path = Path(tmp_dir) / "embedded.srt"
        try:
            run_ffmpeg(["-i", str(source_path), "-map", "0:s:0", str(srt_path)])
        except PipelineStageError:
            return None
        if not srt_path.exists() or srt_path.stat().st_size == 0:
            return None
        text = srt_path.read_text(encoding="utf-8", errors="replace")
    parsed = parse_srt(text)
    return parsed or None


def _extract_sidecar_subtitles(source_path: Path) -> list[dict[str, Any]] | None:
    sidecar = _sidecar_path(source_path)
    if not sidecar.is_file():
        return None
    text = sidecar.read_text(encoding="utf-8", errors="replace")
    parsed = parse_srt(text)
    return parsed or None


def _configure_cuda_dlls() -> None:
    import torch

    torch_lib = Path(torch.__file__).resolve().parent / "lib"
    if not torch_lib.is_dir():
        return
    os.add_dll_directory(str(torch_lib))
    os.environ["PATH"] = f"{torch_lib}{os.pathsep}{os.environ.get('PATH', '')}"


def _transcribe_with_whisper(source_path: Path) -> list[dict[str, Any]]:
    model_root = Path(settings.MODEL_ROOT)
    os.environ.setdefault("HF_HOME", str(model_root / "huggingface"))
    _configure_cuda_dlls()

    import torch
    from faster_whisper import BatchedInferencePipeline, WhisperModel

    if not torch.cuda.is_available():
        raise PipelineStageError(
            "CUDA_UNAVAILABLE", "CUDA is unavailable for Whisper transcription."
        )

    model_name = getattr(settings, "WHISPER_MODEL_NAME", "distil-large-v3.5")
    model = WhisperModel(
        model_name,
        device="cuda",
        compute_type="float16",
        download_root=str(model_root / "whisper"),
    )
    try:
        pipeline = BatchedInferencePipeline(model=model)
        segments_iter, _info = pipeline.transcribe(
            str(source_path),
            language="en",
            batch_size=8,
            beam_size=5,
            vad_filter=True,
            word_timestamps=True,
        )
        segments: list[dict[str, Any]] = []
        for segment in segments_iter:
            text = segment.text.strip()
            if not text:
                continue
            segments.append(
                {
                    "start_ts": float(segment.start),
                    "end_ts": float(segment.end),
                    "text": text,
                    "confidence": (
                        float(getattr(segment, "avg_logprob", 0.0))
                        if getattr(segment, "avg_logprob", None) is not None
                        else None
                    ),
                }
            )
        return segments
    finally:
        del model
        torch.cuda.empty_cache()


def extract_transcript(source_path: Path, probe_metadata: dict[str, Any]) -> dict[str, Any]:
    """Priority cascade: embedded subtitle -> sidecar SRT -> local Whisper fallback."""
    if int(probe_metadata.get("subtitle_stream_count") or 0) > 0:
        embedded = _extract_embedded_subtitles(source_path)
        if embedded:
            return {"source": "embedded", "language": "en", "segments": embedded}

    sidecar = _extract_sidecar_subtitles(source_path)
    if sidecar:
        return {"source": "sidecar", "language": "en", "segments": sidecar}

    whisper_segments = _transcribe_with_whisper(source_path)
    return {"source": "whisper", "language": "en", "segments": whisper_segments}
