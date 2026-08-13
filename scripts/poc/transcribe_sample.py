from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path

import psutil
import torch


DEFAULT_SOURCE = Path(
    r"G:\Youtube kaynak videolar"
    r"\THE ABSOLUTE BEST OF DAEMON TARGARYEN - ABC CINEMA (1080p).mp4"
)
DEFAULT_MODEL_ROOT = Path(r"D:\AIContent\models")
DEFAULT_OUTPUT_ROOT = Path(r"G:\AIContent\cache\poc-whisper")


def configure_cuda_dlls() -> Path:
    """Expose PyTorch-bundled CUDA DLLs to CTranslate2 on Windows."""
    torch_lib = Path(torch.__file__).resolve().parent / "lib"
    if not torch_lib.is_dir():
        raise RuntimeError(f"PyTorch CUDA DLL directory is missing: {torch_lib}")

    os.add_dll_directory(str(torch_lib))
    os.environ["PATH"] = f"{torch_lib}{os.pathsep}{os.environ.get('PATH', '')}"
    return torch_lib


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the native Windows Whisper GPU PoC.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--model-root", type=Path, default=DEFAULT_MODEL_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--model", default="distil-large-v3.5")
    parser.add_argument("--batch-size", type=int, default=8)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.source.resolve(strict=True)
    if source.suffix.lower() not in {".mp4", ".mkv", ".mov"}:
        raise ValueError(f"Unsupported source extension: {source.suffix}")

    args.model_root.mkdir(parents=True, exist_ok=True)
    args.output_root.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(args.model_root / "huggingface")

    torch_lib = configure_cuda_dlls()

    from faster_whisper import BatchedInferencePipeline, WhisperModel

    if not torch.cuda.is_available():
        raise RuntimeError("PyTorch cannot access the NVIDIA GPU.")

    started = time.perf_counter()
    process = psutil.Process()
    before_rss = process.memory_info().rss

    model = WhisperModel(
        args.model,
        device="cuda",
        compute_type="float16",
        download_root=str(args.model_root / "whisper"),
    )
    pipeline = BatchedInferencePipeline(model=model)
    segments_iter, info = pipeline.transcribe(
        str(source),
        language="en",
        batch_size=args.batch_size,
        beam_size=5,
        vad_filter=True,
        word_timestamps=True,
    )

    segments: list[dict[str, object]] = []
    transcript_lines: list[str] = []
    for segment in segments_iter:
        words = [
            {
                "start": word.start,
                "end": word.end,
                "text": word.word,
                "probability": word.probability,
            }
            for word in (segment.words or [])
        ]
        segments.append(
            {
                "id": segment.id,
                "start": segment.start,
                "end": segment.end,
                "text": segment.text.strip(),
                "words": words,
            }
        )
        transcript_lines.append(
            f"[{segment.start:09.3f} --> {segment.end:09.3f}] {segment.text.strip()}"
        )

    elapsed = time.perf_counter() - started
    after_rss = process.memory_info().rss
    device = torch.cuda.get_device_properties(0)

    metrics = {
        "source": str(source),
        "source_size_bytes": source.stat().st_size,
        "model": args.model,
        "batch_size": args.batch_size,
        "language": info.language,
        "language_probability": info.language_probability,
        "audio_duration_seconds": info.duration,
        "duration_after_vad_seconds": info.duration_after_vad,
        "segment_count": len(segments),
        "word_count": sum(len(segment["words"]) for segment in segments),
        "elapsed_seconds": elapsed,
        "realtime_factor": elapsed / info.duration if info.duration else None,
        "process_rss_delta_bytes": after_rss - before_rss,
        "python": platform.python_version(),
        "torch": torch.__version__,
        "torch_cuda_runtime": torch.version.cuda,
        "gpu": device.name,
        "gpu_total_memory_bytes": device.total_memory,
        "torch_lib": str(torch_lib),
    }

    (args.output_root / "transcript.json").write_text(
        json.dumps({"metrics": metrics, "segments": segments}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (args.output_root / "transcript.txt").write_text(
        "\n".join(transcript_lines) + "\n",
        encoding="utf-8",
    )
    (args.output_root / "metrics.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(json.dumps(metrics, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise

