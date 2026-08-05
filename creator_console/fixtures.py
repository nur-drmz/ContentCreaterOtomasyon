"""Deterministic fixtures + mocked provider clients.

WHY THIS EXISTS
---------------
The whole console flow -- source -> strategy -> job -> review -> output -- must be
walkable and testable with NO Colab, NO credentials and NO network. So:

  * `fake_google_client()` / `fake_*_client()` return doubles shaped like the real
    SDKs. They record their calls, which is how the suite proves that
    `direct_clip_series` makes zero of them.
  * `seed_demo_project()` builds a real (tiny, synthetic) MP4 with ffmpeg so the
    local analysis and render stages run for real against real media.

Nothing here contacts a provider, uploads, or publishes.
"""
from __future__ import annotations

import json
import struct
import subprocess
from pathlib import Path

from local_pipeline import google_ai

#: Every fake call lands here so tests can assert exact counts.
PROVIDER_CALLS: list = []

DEMO_SCRIPT_TEXT = (
    "Bu sahnede kimsenin beklemedigi bir sey oluyor ve butun dengeyi degistiriyor. "
    "Karakterin verdigi karar, sonraki her olayin sebebi haline geliyor."
)


def reset_provider_calls() -> None:
    PROVIDER_CALLS.clear()


#: Distinct enough that a test can prove it was CARRIED, not reconstructed.
DEMO_HOOK_TEXT = "Bir hanedani yikan sey kilic degil, yasak bir asktir."


def fake_google_client(*, fail_models=(), script_text=DEMO_SCRIPT_TEXT,
                        tts_pcm=None, empty_audio=False):
    """A `google.genai.Client`-shaped double for text, TTS and embeddings."""
    pcm = tts_pcm if tts_pcm is not None else b"\x10\x27" * google_ai.TTS_SAMPLE_RATE

    class _Models:
        def generate_content(self, *, model, contents, config=None):
            PROVIDER_CALLS.append(("google.generate_content", model))
            if model in fail_models:
                raise RuntimeError("503 unavailable (fixture)")
            if config and "AUDIO" in (config.get("response_modalities") or []):
                data = b"" if empty_audio else pcm
                return type("R", (), {
                    "text": None, "usage_metadata": None,
                    "candidates": [{"content": {"parts": [
                        {"inline_data": {"data": data,
                                          "mime_type": "audio/L16;rate=24000"}}]}}]})()
            payload = json.dumps({"script": script_text, "hook": DEMO_HOOK_TEXT,
                                   "estimated_duration_sec": 60})
            return type("R", (), {"text": payload, "candidates": [],
                                   "usage_metadata": {"total_token_count": 128}})()

        def embed_content(self, *, model, contents, config=None):
            PROVIDER_CALLS.append(("google.embed_content", model))
            dim = int((config or {}).get("output_dimensionality") or 768)
            return {"embeddings": [{"values": [0.01] * dim} for _ in contents]}

        def list(self):
            PROVIDER_CALLS.append(("google.models.list", None))
            return [type("M", (), {"name": f"models/{m}"})()
                    for m in google_ai.MODEL_ALLOWLIST]

    return type("C", (), {"models": _Models()})()


def fake_openai_client():
    class _Completions:
        def create(self, **kwargs):
            PROVIDER_CALLS.append(("openai.chat", kwargs.get("model")))
            payload = json.dumps({"script": DEMO_SCRIPT_TEXT, "hook": DEMO_HOOK_TEXT,
                                   "estimated_duration_sec": 60})
            message = type("M", (), {"content": payload})()
            return type("R", (), {
                "choices": [type("C", (), {"message": message})()],
                "usage": type("U", (), {"prompt_tokens": 10, "completion_tokens": 5})()})()

    return type("C", (), {"chat": type("Chat", (), {"completions": _Completions()})()})()


def fake_anthropic_client():
    class _Messages:
        def create(self, **kwargs):
            PROVIDER_CALLS.append(("anthropic.messages", kwargs.get("model")))
            payload = json.dumps({"script": DEMO_SCRIPT_TEXT, "hook": DEMO_HOOK_TEXT,
                                   "estimated_duration_sec": 60})
            block = type("B", (), {"text": payload})()
            return type("R", (), {
                "content": [block],
                "usage": type("U", (), {"input_tokens": 10, "output_tokens": 5})()})()

    return type("C", (), {"messages": _Messages()})()


def fake_secret_getter(*, google=True, youtube=False):
    """Fixture secrets. The VALUES are obviously fake and are never real keys."""
    values = {}
    if google:
        values["GeminiKey"] = "FAKE-CONSOLE-GEMINI-" + "0" * 10
    if youtube:
        values["YoutubeKEY"] = "FAKE-CONSOLE-YOUTUBE-" + "0" * 10
    return lambda name: values.get(name)


def fixture_client_factories(**kwargs):
    """Factory bundle for `ConsoleConfig`, so no real SDK is ever constructed."""
    return {
        "google_client_factory": lambda: fake_google_client(**kwargs),
        "openai_client_factory": lambda: None,
        "anthropic_client_factory": lambda: None,
        "elevenlabs_client_factory": lambda: None,
    }


def make_test_source_video(path, *, seconds: int = 6, with_audio: bool = True) -> Path:
    """Real, tiny MP4 via ffmpeg so local analysis/render run against real media."""
    from local_pipeline import render_utils

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg_bin, _ = render_utils.resolve_ffmpeg_binaries()
    command = [
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=size=640x360:rate=12:duration={seconds}",
    ]
    if with_audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    command += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if with_audio:
        command += ["-c:a", "aac", "-shortest"]
    command += [str(path)]
    proc = subprocess.run(command, capture_output=True, text=True)
    if proc.returncode != 0 or not path.is_file():
        raise RuntimeError(f"fixture video uretilemedi: {proc.stderr[-800:]}")
    return path


def truncate_file(path, keep_bytes: int = 64) -> Path:
    """Fault-injection helper: make a file exist but be unusable."""
    path = Path(path)
    data = path.read_bytes()[:keep_bytes]
    path.write_bytes(data)
    return path


def write_wav(path, *, seconds: float = 1.0, sample_rate: int = 24000) -> Path:
    """Minimal valid WAV, for voiceover fixtures that must pass validation."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(sample_rate * seconds)
    pcm = b"".join(struct.pack("<h", (i % 2000) - 1000) for i in range(frames))
    path.write_bytes(google_ai.pcm_to_wav_bytes(pcm, sample_rate=sample_rate))
    return path


def seed_demo_project(app, *, title: str = "Demo proje (fixture)") -> dict:
    """Create a project with a real synthetic source video and a fresh assessment.

    Deliberately stops BEFORE choosing a mode: Phase 0's whole point is that a
    human picks the strategy, so a fixture must not pick one either.
    """
    record = app.create_project(title=title, created_by="fixtures")
    ownership = app.ownership(record["project_id"])
    service = app.service(ownership)

    staging = app.storage.resolve(ownership, ".runtime", "demo_source.mp4", create_parent=True)
    make_test_source_video(staging)
    service.register_source_video(
        filename="demo_source.mp4", payload=staging.read_bytes(), registered_by="fixtures")
    staging.unlink(missing_ok=True)

    assessment = service.run_source_assessment(actor="fixtures")
    return {
        "project_id": ownership.project_id,
        "title": title,
        "recommended_mode": assessment["recommended_mode"],
        "selected_mode": assessment["selected_mode"],
    }
