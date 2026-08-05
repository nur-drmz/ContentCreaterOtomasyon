"""Reusable text-to-speech logic shared by 03_tts_voice.ipynb and
00_youtube_automation_master.ipynb.

Notebook 03 keeps its own multi-provider benchmarking UI; the provider-call
wrappers and the selected_voiceover.json contract builder live here so both
notebooks share one implementation.

Never uploads or publishes anything. Never prints/logs/persists an API key.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

STATUS_VALUES = {
    "experimental", "development_placeholder", "review_required",
    "approved", "insufficient_data", "failed", "production_ready",
}

TTS_MODEL_REGISTRY = {
    "elevenlabs": {"model_id": "eleven_multilingual_v2", "voice_id": "21m00Tcm4TlvDq8ikWAM"},
    "openai": {"model_id": "tts-1-hd", "voice": "alloy", "price_per_1k_chars_usd": 0.030},
    "piper": {"model_id": "piper-local"},
}


def audio_duration_sec(ffprobe_bin, path) -> float | None:
    if not ffprobe_bin:
        return None
    proc = subprocess.run(
        [ffprobe_bin, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    try:
        return round(float(json.loads(proc.stdout)["format"]["duration"]), 3)
    except Exception:
        return None


def call_elevenlabs_tts(client, script_text: str, out_path, model_id: str, voice_id: str) -> dict:
    """`client` is an elevenlabs.client.ElevenLabs-compatible object (or test
    double) with `.text_to_speech.convert(voice_id=, model_id=, text=)`
    returning an iterable of byte chunks."""
    out_path = Path(out_path)
    try:
        t0 = time.time()
        audio = client.text_to_speech.convert(voice_id=voice_id, model_id=model_id, text=script_text)
        with open(out_path, "wb") as f:
            for chunk in audio:
                f.write(chunk)
        latency = time.time() - t0
        cost = (len(script_text) / 1000) * 0.30
        return {"provider": "elevenlabs", "model": model_id, "voice_id": voice_id,
                "audio_path": str(out_path), "cost_usd_estimate": cost, "latency_sec": latency}
    except Exception as exc:
        return {"error": f"ElevenLabs cagrisi basarisiz: {exc}"}


def call_openai_tts(client, script_text: str, out_path, model_id: str, voice: str, price_per_1k_chars: float) -> dict:
    """`client` is an openai.OpenAI-compatible object with
    `.audio.speech.create(model=, voice=, input=)` returning a response with
    `.stream_to_file(path)`."""
    out_path = Path(out_path)
    try:
        t0 = time.time()
        resp = client.audio.speech.create(model=model_id, voice=voice, input=script_text)
        resp.stream_to_file(str(out_path))
        latency = time.time() - t0
        cost = (len(script_text) / 1000) * price_per_1k_chars
        return {"provider": "openai", "model": model_id, "voice_id": voice,
                "audio_path": str(out_path), "cost_usd_estimate": cost, "latency_sec": latency}
    except Exception as exc:
        return {"error": f"OpenAI TTS cagrisi basarisiz: {exc}"}


def call_piper_tts(piper_binary: str | None, voice_model_path, script_text: str, out_path) -> dict:
    """Free, fully local TTS via the Piper CLI. No API key, no network call.

    `piper_binary` is a command name (resolved via PATH) or a path to the
    piper executable; `voice_model_path` is a local .onnx voice model file.
    A missing binary or model returns a clean {"error": ...} instead of
    raising, so callers surface 'insufficient_data' rather than crashing --
    same contract as call_elevenlabs_tts/call_openai_tts on failure."""
    out_path = Path(out_path)
    if not piper_binary:
        return {"error": "Piper calistirilamadi: piper_binary yapilandirilmamis"}
    resolved_bin = shutil.which(piper_binary)
    if resolved_bin is None and Path(piper_binary).exists():
        resolved_bin = piper_binary
    if resolved_bin is None:
        return {"error": f"Piper calistirilamadi: binary bulunamadi ({piper_binary})"}
    if not voice_model_path or not Path(voice_model_path).exists():
        return {"error": f"Piper calistirilamadi: ses modeli bulunamadi ({voice_model_path})"}
    try:
        t0 = time.time()
        proc = subprocess.run(
            [resolved_bin, "--model", str(voice_model_path), "--output_file", str(out_path)],
            input=script_text, capture_output=True, text=True,
        )
        if proc.returncode != 0 or not out_path.exists():
            return {"error": f"Piper cagrisi basarisiz (returncode={proc.returncode}): {proc.stderr[:500]}"}
        latency = time.time() - t0
        return {"provider": "piper", "model": Path(voice_model_path).stem, "voice_id": None,
                "audio_path": str(out_path), "cost_usd_estimate": 0.0, "latency_sec": latency}
    except Exception as exc:
        return {"error": f"Piper cagrisi basarisiz: {exc}"}


def call_gtts(script_text: str, out_path, language: str = "tr", gtts_factory=None) -> dict:
    """SADECE development_placeholder -- gTTS is a free baseline that hits a
    Google web endpoint with no API key, so a network failure here is
    expected/handled, never fatal. `gtts_factory(text, lang)` defaults to
    `gtts.gTTS`; tests can inject a fake factory to avoid any real network
    call."""
    out_path = Path(out_path)
    try:
        t0 = time.time()
        if gtts_factory is None:
            from gtts import gTTS as gtts_factory  # noqa: N813 (matches upstream class name)
        tts = gtts_factory(text=script_text, lang=language) if callable(gtts_factory) else gtts_factory
        tts.save(str(out_path))
        latency = time.time() - t0
        return {"provider": "gtts", "model": "gtts-default", "voice_id": None,
                "audio_path": str(out_path), "cost_usd_estimate": 0.0, "latency_sec": latency}
    except Exception as exc:
        return {"error": f"gTTS basarisiz (ag/servis hatasi olabilir): {exc}"}


# ---------------------------------------------------------------------------
# Provider selection for the master pipeline.
#
# Quality-first default: ElevenLabs eleven_multilingual_v2. OpenAI tts-1-hd is
# an OPTIONAL paid fallback. Piper is a free/local, API-key-free provider that
# is still full-fledged production tier -- its output is 'review_required' and
# approvable, unlike gTTS. gTTS is never part of the production chain -- it is
# a separately-gated development baseline that can NEVER be approved
# (see approval.NEVER_APPROVABLE_PROVIDERS).
# ---------------------------------------------------------------------------

PRODUCTION_TTS_PROVIDERS = ("elevenlabs", "openai", "piper")
DEFAULT_TTS_PROVIDER = "elevenlabs"

DEV_FALLBACK_SCRIPT_TEXT = (
    "Bu sahnede hic kimsenin beklemedigi bir sey oluyor. Ana karakter, en yakin dostunun "
    "aslinda baska bir plani oldugunu ogreniyor. Peki bundan sonra ne olacak? Cevabi son "
    "saniyede geliyor ve kimse buna hazir degil."
)


def build_tts_candidates(provider: str | None, allow_fallback: bool, available_providers) -> list[str]:
    """Ordered, de-duplicated PRODUCTION provider chain for one TTS request.

    With allow_fallback=False this returns at most ONE provider, so a single
    request can cost at most one paid call. gTTS is deliberately absent -- it is
    handled separately and only when explicitly enabled.
    """
    available = {p for p in available_providers if p in PRODUCTION_TTS_PROVIDERS}
    selected = provider or DEFAULT_TTS_PROVIDER
    if selected not in PRODUCTION_TTS_PROVIDERS:
        raise ValueError(
            f"bilinmeyen uretim TTS saglayicisi: {selected!r} (beklenen: {list(PRODUCTION_TTS_PROVIDERS)})"
        )

    chain = []
    if selected in available:
        chain.append(selected)
    if allow_fallback:
        chain.extend(p for p in PRODUCTION_TTS_PROVIDERS if p in available and p not in chain)

    return chain if allow_fallback else chain[:1]


def build_selected_voiceover_artifact(candidate: dict | None, script_text: str, script_status: str,
                                       used_dev_fallback_script: bool, ffprobe_bin=None,
                                       quality_score_1_5=None, language: str = "tr",
                                       attempts: list | None = None) -> dict:
    """Builds the selected_voiceover.json contract dict from one winning TTS
    candidate result (as returned by call_elevenlabs_tts / call_openai_tts /
    call_gtts).

    This function CANNOT produce status='approved' -- it takes no approval
    argument. A production provider's fresh output is always 'review_required'
    (nobody has listened to it yet); a gTTS result is always
    'development_placeholder' and can never be approved at all. Promotion to
    'approved' happens only via `local_pipeline.approval.approve_artifact()`,
    which requires the SHA-256 of the audio file the human actually played.
    """
    source_script_hash = hashlib.sha256((script_text or "").encode("utf-8")).hexdigest()[:16]
    unapproved = {
        "approved": False, "approved_by": None, "approved_at": None,
        "notes": None, "approved_content_sha256": None,
    }

    if candidate is None or "error" in (candidate or {}):
        return {
            "status": "insufficient_data",
            "status_reason": "no TTS candidate could be produced (missing API keys, unapproved script, or all attempts failed)",
            "provider": None, "model": None, "voice_id": None, "audio_path": None,
            "source_script_hash": source_script_hash, "source_script_status": script_status,
            "used_dev_fallback_script": used_dev_fallback_script,
            "duration_sec": None, "cost_usd_estimate": 0.0, "cost_usd_actual": None, "latency_sec": 0.0,
            "quality_score_1_5": None, "language": language,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "provider_attempts": list(attempts or []),
            "human_approval": dict(unapproved),
        }

    is_gtts = candidate.get("provider") == "gtts"
    if is_gtts:
        status = "development_placeholder"
        status_reason = "gTTS free/local baseline; never approvable or production_ready by policy"
    else:
        status = "review_required"
        status_reason = (
            "seslendirme uretildi; INSAN INCELEMESI BEKLIYOR (sesi dinledikten sonra onay "
            "ayri bir adimda, approval.approve_artifact ile verilir)"
        )

    return {
        "status": status,
        "status_reason": status_reason,
        "provider": candidate["provider"],
        "model": candidate["model"],
        "voice_id": candidate.get("voice_id"),
        "audio_path": candidate["audio_path"],
        "source_script_hash": source_script_hash,
        "source_script_status": script_status,
        "used_dev_fallback_script": used_dev_fallback_script,
        "duration_sec": audio_duration_sec(ffprobe_bin, candidate["audio_path"]),
        "cost_usd_estimate": candidate.get("cost_usd_estimate", 0.0),
        "cost_usd_actual": None,
        "latency_sec": candidate.get("latency_sec", 0.0),
        "quality_score_1_5": quality_score_1_5,
        "language": language,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider_attempts": list(attempts or []),
        "human_approval": dict(unapproved),
    }


def run_tts(selected_script: dict | None, out_dir, ffprobe_bin=None, elevenlabs_client=None,
            openai_client=None, piper_binary: str | None = None, piper_voice_model_path=None,
            gtts_factory=None, tts_provider: str | None = None,
            allow_provider_fallback: bool = False, allow_dev_gtts_fallback: bool = False) -> dict:
    """Master-pipeline TTS: exactly ONE paid call on the happy path (Piper is free
    and does not count against that budget).

    Cost contract:
      - Production providers are tried STRICTLY SEQUENTIALLY via
        `build_tts_candidates()`, and the loop RETURNS on the first success.
        OpenAI is therefore never called after ElevenLabs succeeds, etc.
      - With allow_provider_fallback=False at most ONE production provider is
        contacted.
      - Piper requires no API key/client; it is "available" once both
        piper_binary and piper_voice_model_path are provided. A missing
        binary/model is caught inside call_piper_tts() and surfaces as a
        failed attempt (never a crash), which flows into 'insufficient_data'
        if no other provider succeeds.
      - gTTS is contacted ONLY when every production provider was unavailable or
        failed AND allow_dev_gtts_fallback=True. It is never called after a
        production success.

    Approval contract: production output is always 'review_required'; gTTS output
    is always 'development_placeholder'. There is no approval argument.

    Production providers additionally require an APPROVED script: if
    selected_script['status'] != 'approved', no paid call is made at all (an
    unreviewed script must not be turned into paid audio).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    using_dev_fallback = not (selected_script and selected_script.get("script_text"))
    if using_dev_fallback:
        script_text, language, script_status = DEV_FALLBACK_SCRIPT_TEXT, "tr", "development_placeholder"
    else:
        script_text = selected_script["script_text"]
        language = selected_script.get("language", "tr")
        script_status = selected_script.get("status", "review_required")

    run_production_tts = (not using_dev_fallback) and script_status == "approved"
    attempts = []

    if run_production_tts:
        clients = {"elevenlabs": elevenlabs_client, "openai": openai_client}
        available = [name for name, client in clients.items() if client is not None]
        if piper_binary and piper_voice_model_path:
            available.append("piper")
        chain = build_tts_candidates(tts_provider, allow_provider_fallback, available)

        for provider_name in chain:
            if provider_name == "elevenlabs":
                cfg = TTS_MODEL_REGISTRY["elevenlabs"]
                result = call_elevenlabs_tts(
                    clients["elevenlabs"], script_text, out_dir / "elevenlabs.mp3",
                    cfg["model_id"], cfg["voice_id"],
                )
            elif provider_name == "openai":
                cfg = TTS_MODEL_REGISTRY["openai"]
                result = call_openai_tts(
                    clients["openai"], script_text, out_dir / f"openai-{cfg['model_id']}.mp3",
                    cfg["model_id"], cfg["voice"], cfg["price_per_1k_chars_usd"],
                )
            else:
                cfg = TTS_MODEL_REGISTRY["piper"]
                result = call_piper_tts(
                    piper_binary, piper_voice_model_path, script_text, out_dir / "piper.wav",
                )
            if "error" not in result:
                attempts.append({"provider": provider_name, "model": result["model"],
                                  "ok": True, "error": None})
                # RETURN immediately: no further production provider, and no gTTS.
                return build_selected_voiceover_artifact(
                    result, script_text, script_status, using_dev_fallback,
                    ffprobe_bin=ffprobe_bin, language=language, attempts=attempts,
                )
            attempts.append({"provider": provider_name, "model": cfg.get("model_id"),
                              "ok": False, "error": result["error"]})
    else:
        attempts.append({
            "provider": None, "model": None, "ok": False,
            "error": (
                "uretim TTS atlandi: script onaylanmamis "
                f"(script_status={script_status!r}); ucretli cagri yapilmadi"
            ),
        })

    # Only reachable when no production provider succeeded.
    if allow_dev_gtts_fallback:
        gtts_result = call_gtts(script_text, out_dir / "gtts.mp3",
                                 language=language, gtts_factory=gtts_factory)
        if "error" not in gtts_result:
            attempts.append({"provider": "gtts", "model": "gtts-default", "ok": True, "error": None})
            return build_selected_voiceover_artifact(
                gtts_result, script_text, script_status, using_dev_fallback,
                ffprobe_bin=ffprobe_bin, language=language, attempts=attempts,
            )
        attempts.append({"provider": "gtts", "model": "gtts-default",
                          "ok": False, "error": gtts_result["error"]})

    return build_selected_voiceover_artifact(
        None, script_text, script_status, using_dev_fallback,
        ffprobe_bin=ffprobe_bin, language=language, attempts=attempts,
    )
