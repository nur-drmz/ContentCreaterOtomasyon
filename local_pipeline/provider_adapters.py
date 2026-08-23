"""Optional, credential-guarded cross-provider adapters (Phase 8).

WHY THIS MODULE EXISTS
----------------------
When the Google route is exhausted (RPD spent, model 404, circuit open) the
pipeline should be able to continue on a DIFFERENT provider -- which is a
legitimate provider change, not quota circumvention (see
`google_ai.ALLOW_ACCOUNT_QUOTA_ROTATION`).

Nothing here runs unless its credentials exist. A missing key is NOT an error:
the adapter reports `available=False` with a sanitized reason and the caller
simply moves on. No adapter ever invents a successful result, and none of them
change existing local behavior (faster-whisper stays the primary transcriber,
Piper/ElevenLabs stay the existing TTS providers).

Every network call goes through an injected `transport` callable, so the whole
module is exercised in `verify_local.py` without contacting any real endpoint.

Never uploads, publishes, prints a secret, or serializes a secret.
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

from .google_ai import (
    CredentialRef,
    resolve_secret,
    sanitize_google_error,
)

#: Colab Secret names these adapters look for. All optional.
PROVIDER_SECRETS = {
    "groq": ("groq",),
    "cloudflare": ("cloudflare_account_id", "cloudflare_auth_token"),
    "azure_speech": ("azure_speech_key", "azure_speech_region"),
}

#: Recommended cross-provider routes per capability. Google always comes first;
#: these are what the router falls THROUGH to, in order.
CROSS_PROVIDER_ROUTES = {
    "high_quality_text": (
        ("google", None),
        ("groq", "openai/gpt-oss-120b"),
        ("cloudflare", "@cf/openai/gpt-oss-120b"),
    ),
    "high_volume_text": (
        ("google", None),
        ("groq", "qwen/qwen3.6-27b"),
        ("cloudflare", "@cf/qwen/qwen3-30b-a3b-fp8"),
    ),
    "embeddings": (
        ("google", "gemini-embedding-2"),
        ("cloudflare", "@cf/baai/bge-m3"),
        ("local", "sentence-transformers (multilingual, yerel)"),
    ),
    "transcription": (
        ("local", "faster-whisper (BIRINCIL, degismedi)"),
        ("groq", "whisper-large-v3-turbo"),
        ("cloudflare", "@cf/openai/whisper-large-v3-turbo"),
    ),
    "tts": (
        ("google", "gemini TTS route"),
        # Edge needs no credential, so it is the route step that still works
        # when every keyed provider is spent. It sits before Azure because a
        # free, always-available fallback should be tried before a paid one.
        ("edge_tts", "tr-TR-AhmetNeural"),
        ("azure_speech", "tr-TR-AhmetNeural"),
    ),
}

#: Verified-at-write-time Azure Neural Turkish voices.
AZURE_TURKISH_VOICES = ("tr-TR-AhmetNeural", "tr-TR-EmelNeural")

#: The Cloudflare multilingual text model is the one field most likely to drift;
#: it is a CONFIGURABLE DEFAULT that must be re-verified against
#: developers.cloudflare.com/workers-ai/models/ before being relied on.
CLOUDFLARE_MODEL_VERIFICATION_NOTE = (
    "Cloudflare Workers AI model kimlikleri sik degisir; kullanmadan once "
    "developers.cloudflare.com/workers-ai/models/ uzerinden dogrula."
)


def resolve_provider_credentials(provider: str, secret_getter=None) -> dict:
    """Resolves every secret a provider needs, without ever exposing a value.

    Returns {'provider', 'available', 'reason', 'credentials': {kind: CredentialRef}}.
    `available=False` because a key is absent is a normal, non-fatal state.
    """
    kinds = PROVIDER_SECRETS.get(provider)
    if not kinds:
        return {"provider": provider, "available": False,
                "reason": f"bilinmeyen saglayici: {provider!r}", "credentials": {}}
    credentials = {kind: resolve_secret(kind, secret_getter) for kind in kinds}
    missing = [c.aliases_tried[0] for c in credentials.values() if not c.configured]
    if missing:
        return {
            "provider": provider, "available": False,
            "reason": f"gizli anahtar yok ({', '.join(missing)}); saglayici temiz atlandi",
            "credentials": credentials,
        }
    return {"provider": provider, "available": True, "reason": "", "credentials": credentials}


def describe_provider_states(secret_getter=None) -> dict:
    """Sanitized 'configured/missing' map for every optional provider."""
    return {
        provider: {
            "state": "configured" if resolve_provider_credentials(provider, secret_getter)["available"]
                     else "missing",
            "secrets": [resolve_secret(k, secret_getter).aliases_tried[0] for k in kinds],
        }
        for provider, kinds in PROVIDER_SECRETS.items()
    }


def _skipped(provider: str, reason: str, capability: str) -> dict:
    return {
        "provider": provider, "capability": capability, "model": None, "ok": False,
        "skipped": True, "status": "skipped", "output": None,
        "error": reason, "error_kind": "credential_missing",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _failed(provider: str, capability: str, model, exc, credential=None) -> dict:
    return {
        "provider": provider, "capability": capability, "model": model, "ok": False,
        "skipped": False, "status": "failed", "output": None,
        "error": sanitize_google_error(exc, credential), "error_kind": "provider_error",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _require_transport(transport):
    if transport is not None:
        return transport

    def _default(method, url, headers=None, json_body=None, data=None, timeout=60):
        import requests  # imported lazily: tests always inject a transport
        response = requests.request(method, url, headers=headers, json=json_body,
                                     data=data, timeout=timeout)
        try:
            payload = response.json()
        except Exception:
            payload = {"_raw_bytes": response.content}
        return response.status_code, payload

    return _default


# ---------------------------------------------------------------------------
# Groq
# ---------------------------------------------------------------------------

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def call_groq_text(prompt: str, *, model: str = "openai/gpt-oss-120b", secret_getter=None,
                   transport=None, capability: str = "high_quality_text",
                   max_tokens: int = 1024) -> dict:
    """OpenAI-compatible chat completion on Groq. Skips cleanly with no key."""
    state = resolve_provider_credentials("groq", secret_getter)
    if not state["available"]:
        return _skipped("groq", state["reason"], capability)
    credential: CredentialRef = state["credentials"]["groq"]
    send = _require_transport(transport)
    try:
        status, payload = send(
            "POST", GROQ_CHAT_URL,
            headers={"Authorization": f"Bearer {credential.value}",
                     "Content-Type": "application/json"},
            json_body={"model": model, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]},
        )
        if status != 200:
            raise RuntimeError(f"HTTP {status}: {str(payload)[:200]}")
        text = payload["choices"][0]["message"]["content"]
        return {"provider": "groq", "capability": capability, "model": model, "ok": True,
                "skipped": False, "status": "ok", "output": text, "error": None,
                "usage_metadata": payload.get("usage"),
                "created_at": datetime.now(timezone.utc).isoformat()}
    except Exception as exc:
        return _failed("groq", capability, model, exc, credential)


def call_groq_transcription(audio_bytes: bytes, *, model: str = "whisper-large-v3-turbo",
                            secret_getter=None, transport=None, filename: str = "audio.wav") -> dict:
    """Optional transcription fallback. faster-whisper stays PRIMARY -- this only
    runs when the caller explicitly asks for a fallback."""
    state = resolve_provider_credentials("groq", secret_getter)
    if not state["available"]:
        return _skipped("groq", state["reason"], "transcription")
    credential: CredentialRef = state["credentials"]["groq"]
    send = _require_transport(transport)
    try:
        status, payload = send(
            "POST", GROQ_TRANSCRIBE_URL,
            headers={"Authorization": f"Bearer {credential.value}"},
            data={"model": model, "file_name": filename, "file_bytes": audio_bytes},
        )
        if status != 200:
            raise RuntimeError(f"HTTP {status}: {str(payload)[:200]}")
        return {"provider": "groq", "capability": "transcription", "model": model, "ok": True,
                "skipped": False, "status": "ok", "output": payload.get("text"), "error": None,
                "created_at": datetime.now(timezone.utc).isoformat()}
    except Exception as exc:
        return _failed("groq", "transcription", model, exc, credential)


# ---------------------------------------------------------------------------
# Cloudflare Workers AI
# ---------------------------------------------------------------------------

def _cloudflare_url(account_id: str, model: str) -> str:
    return f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"


def call_cloudflare_text(prompt: str, *, model: str = "@cf/openai/gpt-oss-120b",
                         secret_getter=None, transport=None,
                         capability: str = "high_quality_text") -> dict:
    state = resolve_provider_credentials("cloudflare", secret_getter)
    if not state["available"]:
        return _skipped("cloudflare", state["reason"], capability)
    account = state["credentials"]["cloudflare_account_id"]
    token = state["credentials"]["cloudflare_auth_token"]
    send = _require_transport(transport)
    try:
        status, payload = send(
            "POST", _cloudflare_url(account.value, model),
            headers={"Authorization": f"Bearer {token.value}"},
            json_body={"messages": [{"role": "user", "content": prompt}]},
        )
        if status != 200:
            raise RuntimeError(f"HTTP {status}: {str(payload)[:200]}")
        result = payload.get("result") or {}
        text = result.get("response") if isinstance(result, dict) else None
        if not text:
            raise RuntimeError("Cloudflare yaniti bos")
        return {"provider": "cloudflare", "capability": capability, "model": model, "ok": True,
                "skipped": False, "status": "ok", "output": text, "error": None,
                "model_verification_note": CLOUDFLARE_MODEL_VERIFICATION_NOTE,
                "created_at": datetime.now(timezone.utc).isoformat()}
    except Exception as exc:
        result = _failed("cloudflare", capability, model, exc, token)
        # The account id is not a secret per se, but it is never echoed anyway.
        result["error"] = sanitize_google_error(result["error"], account)
        return result


def call_cloudflare_embeddings(texts, *, model: str = "@cf/baai/bge-m3", secret_getter=None,
                               transport=None) -> dict:
    state = resolve_provider_credentials("cloudflare", secret_getter)
    if not state["available"]:
        return _skipped("cloudflare", state["reason"], "embeddings")
    account = state["credentials"]["cloudflare_account_id"]
    token = state["credentials"]["cloudflare_auth_token"]
    send = _require_transport(transport)
    items = [texts] if isinstance(texts, str) else list(texts)
    try:
        status, payload = send(
            "POST", _cloudflare_url(account.value, model),
            headers={"Authorization": f"Bearer {token.value}"},
            json_body={"text": items},
        )
        if status != 200:
            raise RuntimeError(f"HTTP {status}: {str(payload)[:200]}")
        vectors = ((payload.get("result") or {}).get("data")) or []
        if not vectors:
            raise RuntimeError("Cloudflare embedding yaniti bos")
        return {"provider": "cloudflare", "capability": "embeddings", "model": model, "ok": True,
                "skipped": False, "status": "ok", "output": vectors, "error": None,
                "dimension": len(vectors[0]),
                "model_verification_note": CLOUDFLARE_MODEL_VERIFICATION_NOTE,
                "created_at": datetime.now(timezone.utc).isoformat()}
    except Exception as exc:
        return _failed("cloudflare", "embeddings", model, exc, token)


# ---------------------------------------------------------------------------
# Azure Neural TTS
# ---------------------------------------------------------------------------

def call_azure_tts(script_text: str, out_path, *, voice: str = "tr-TR-AhmetNeural",
                   secret_getter=None, transport=None) -> dict:
    """Azure Neural TTS fallback for Turkish narration.

    Like every TTS path in this project, a produced file is `review_required` and
    NEVER inherits a previous voiceover's approval.
    """
    if voice not in AZURE_TURKISH_VOICES:
        return _failed("azure_speech", "tts", voice,
                       ValueError(f"desteklenmeyen Turkce ses: {voice!r} "
                                  f"(beklenen: {list(AZURE_TURKISH_VOICES)})"))
    state = resolve_provider_credentials("azure_speech", secret_getter)
    if not state["available"]:
        return _skipped("azure_speech", state["reason"], "tts")
    key = state["credentials"]["azure_speech_key"]
    region = state["credentials"]["azure_speech_region"]
    send = _require_transport(transport)
    safe_script = html.escape(script_text or "", quote=False)
    ssml = (
        f'<speak version="1.0" xml:lang="tr-TR">'
        f'<voice name="{voice}">{safe_script}</voice></speak>'
    )
    try:
        status, payload = send(
            "POST",
            f"https://{region.value}.tts.speech.microsoft.com/cognitiveservices/v1",
            headers={
                "Ocp-Apim-Subscription-Key": key.value,
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": "riff-24khz-16bit-mono-pcm",
            },
            data=ssml.encode("utf-8"),
        )
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
        audio = payload.get("_raw_bytes") if isinstance(payload, dict) else payload
        if not audio:
            raise RuntimeError("Azure TTS bos ses dondurdu")
        from pathlib import Path as _Path
        _Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        _Path(out_path).write_bytes(audio)
        return {"provider": "azure_speech", "capability": "tts", "model": voice, "ok": True,
                "skipped": False, "status": "review_required", "output": str(out_path),
                "audio_path": str(out_path), "error": None,
                "inherits_previous_approval": False,
                "created_at": datetime.now(timezone.utc).isoformat()}
    except Exception as exc:
        return _failed("azure_speech", "tts", voice, exc, key)


# ---------------------------------------------------------------------------
# Edge TTS (Microsoft Edge read-aloud endpoint, no credential)
# ---------------------------------------------------------------------------

#: Verified-at-write-time Turkish Edge voices. Edge exposes hundreds of voices
#: and the catalog changes remotely, so this is a CURATED DEFAULT SET, not the
#: whole catalog: `voice` accepts any well-formed `xx-XX-NameNeural` id.
EDGE_TURKISH_VOICES = ("tr-TR-AhmetNeural", "tr-TR-EmelNeural")
DEFAULT_EDGE_VOICE = "tr-TR-AhmetNeural"

#: Edge TTS returns MPEG audio. Every other narration path in this project
#: produces 24 kHz/16-bit/mono PCM WAV, and `tts_runner.concat_wav_chunks`
#: refuses to join files whose format differs. Converting here (rather than
#: teaching the concat step a second format) keeps one audio contract.
EDGE_OUTPUT_SAMPLE_RATE = 24_000
EDGE_OUTPUT_CHANNELS = 1


def _valid_voice_id(voice: str) -> bool:
    """`tr-TR-AhmetNeural`-shaped, without asserting the remote catalog."""
    parts = str(voice or "").split("-")
    return (len(parts) >= 3 and len(parts[0]) == 2 and parts[0].isalpha()
            and len(parts[1]) == 2 and parts[1].isalnum()
            and bool(parts[-1]) and parts[-1][:1].isupper())


def normalize_speech_rate(value=None) -> str:
    """Accepts a multiplier (1.1), a signed percent ('+10%'), or None.

    WHY A MULTIPLIER IS ALLOWED
    ---------------------------
    Callers reason in "10% faster", not in Edge's wire format. Returning the
    wire string from one place keeps that translation out of every call site,
    and makes an out-of-range request a refusal instead of a silent clamp.
    """
    if value is None or value == "":
        return "+0%"
    if isinstance(value, str):
        text = value.strip().replace(" ", "")
        if text.endswith("%") and text[:1] in "+-" and text[1:-1].isdigit():
            if abs(int(text[:-1])) > 100:
                raise ValueError(f"konusma hizi araligi disinda: {value!r} (+-100%)")
            return text
        raise ValueError(f"gecersiz konusma hizi: {value!r} "
                         "(beklenen '+10%' bicimi veya 1.1 gibi bir carpan)")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"gecersiz konusma hizi: {value!r}")
    if not 0.5 <= float(value) <= 2.0:
        raise ValueError(f"konusma hizi carpani araligi disinda: {value!r} (0.5-2.0)")
    percent = int(round((float(value) - 1.0) * 100))
    return f"{'+' if percent >= 0 else '-'}{abs(percent)}%"


def normalize_speech_volume(value=None) -> str:
    if value is None or value == "":
        return "+0%"
    if isinstance(value, str):
        text = value.strip().replace(" ", "")
        if text.endswith("%") and text[:1] in "+-" and text[1:-1].isdigit():
            return text
        raise ValueError(f"gecersiz ses seviyesi: {value!r}")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"gecersiz ses seviyesi: {value!r}")
    percent = int(round((float(value) - 1.0) * 100))
    return f"{'+' if percent >= 0 else '-'}{abs(percent)}%"


def _default_edge_synth(text, *, voice, rate, volume, pitch):
    """Real Edge call. Returns (mpeg_bytes, word_boundaries).

    Imported lazily and only reached when no `synth` was injected, so the whole
    adapter is exercised offline by the verification suites.
    """
    import asyncio

    import edge_tts

    async def _run():
        communicate = edge_tts.Communicate(text, voice, rate=rate,
                                           volume=volume, pitch=pitch)
        audio, boundaries = bytearray(), []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                # Edge reports 100-ns ticks; seconds are what subtitles use.
                boundaries.append({
                    "text": chunk.get("text"),
                    "start": round(chunk["offset"] / 10_000_000, 3),
                    "end": round((chunk["offset"] + chunk["duration"]) / 10_000_000, 3),
                })
        return bytes(audio), boundaries

    return asyncio.run(_run())


def _mpeg_to_pcm_wav(audio_bytes: bytes, out_path, *, ffmpeg_bin=None) -> None:
    import subprocess
    from pathlib import Path as _Path

    if ffmpeg_bin is None:
        from .render_utils import resolve_ffmpeg_binaries
        ffmpeg_bin = resolve_ffmpeg_binaries()[0]
    out_path = _Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Written to a sibling and renamed so an interrupted convert never leaves a
    # truncated file that later looks like a finished narration chunk.
    temp = out_path.with_suffix(out_path.suffix + ".part")
    process = subprocess.run(
        [str(ffmpeg_bin), "-hide_banner", "-loglevel", "error", "-y",
         "-i", "pipe:0", "-vn", "-acodec", "pcm_s16le",
         "-ar", str(EDGE_OUTPUT_SAMPLE_RATE), "-ac", str(EDGE_OUTPUT_CHANNELS),
         "-f", "wav", str(temp)],
        input=audio_bytes, capture_output=True, check=False)
    if process.returncode != 0 or not temp.is_file() or temp.stat().st_size == 0:
        temp.unlink(missing_ok=True)
        detail = (process.stderr or b"").decode("utf-8", "replace").strip()[:200]
        raise RuntimeError(f"Edge TTS sesi WAV'a cevrilemedi: {detail}")
    import os

    os.replace(temp, out_path)


def call_edge_tts(script_text: str, out_path, *, voice: str = DEFAULT_EDGE_VOICE,
                  rate=None, volume=None, pitch: str = "+0Hz", synth=None,
                  ffmpeg_bin=None) -> dict:
    """Microsoft Edge read-aloud TTS. Needs NO credential.

    WHY THIS IS THE ONE ADAPTER WITHOUT A CREDENTIAL GATE
    -----------------------------------------------------
    Every other provider here is skipped when its key is absent. Edge needs no
    key, so its availability question is "is the package installed", and a
    missing package is a clean skip rather than a failure -- same contract, a
    different reason.

    This is what keeps narration alive when the Gemini daily quota is spent: it
    is a legitimate provider change, not quota circumvention.

    Like every TTS path here, the produced file is `review_required` and NEVER
    inherits a previous voiceover's approval. Word boundaries are returned
    because Edge reports them for free, which gives subtitles a timing source
    that costs no extra transcription pass.
    """
    if not _valid_voice_id(voice):
        return _failed("edge_tts", "tts", voice,
                       ValueError(f"gecersiz Edge ses kimligi: {voice!r} "
                                  "(beklenen 'tr-TR-AhmetNeural' bicimi)"))
    if not str(script_text or "").strip():
        return _failed("edge_tts", "tts", voice,
                       ValueError("Edge TTS icin metin bos olamaz"))
    try:
        rate_value = normalize_speech_rate(rate)
        volume_value = normalize_speech_volume(volume)
    except ValueError as exc:
        return _failed("edge_tts", "tts", voice, exc)

    speak = synth
    if speak is None:
        try:
            import edge_tts  # noqa: F401 - availability probe only
        except Exception as exc:  # noqa: BLE001
            return _skipped("edge_tts",
                            f"edge-tts paketi kurulu degil ({type(exc).__name__}); "
                            "saglayici temiz atlandi", "tts")
        speak = _default_edge_synth

    try:
        audio, boundaries = speak(script_text, voice=voice, rate=rate_value,
                                  volume=volume_value, pitch=pitch)
        if not audio:
            raise RuntimeError("Edge TTS bos ses dondurdu")
        _mpeg_to_pcm_wav(audio, out_path, ffmpeg_bin=ffmpeg_bin)
        return {
            "provider": "edge_tts", "capability": "tts", "model": voice,
            "ok": True, "skipped": False, "status": "review_required",
            "output": str(out_path), "audio_path": str(out_path), "error": None,
            "inherits_previous_approval": False,
            "requires_credential": False,
            "speech_rate": rate_value, "speech_volume": volume_value,
            "speech_pitch": pitch,
            "sample_rate": EDGE_OUTPUT_SAMPLE_RATE,
            "channels": EDGE_OUTPUT_CHANNELS,
            "word_boundaries": list(boundaries or []),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:  # noqa: BLE001 - recorded, never fabricated
        return _failed("edge_tts", "tts", voice, exc)


# ---------------------------------------------------------------------------
# Cross-provider fallback driver
# ---------------------------------------------------------------------------

TEXT_ADAPTERS = {"groq": call_groq_text, "cloudflare": call_cloudflare_text}


def _validate_text_output(raw, validator=None):
    if validator is None:
        return raw if isinstance(raw, str) and raw.strip() else None
    try:
        return validator(raw)
    except Exception:
        return None


def run_cross_provider_text_fallback(prompt: str, capability: str = "high_quality_text", *,
                                     secret_getter=None, transport=None,
                                     max_provider_attempts: int = 3,
                                     enabled: bool = True, validator=None) -> dict:
    """Tries the non-Google providers for `capability`, in the documented order.

    Returns on the FIRST success. Providers whose credentials are missing are
    skipped (recorded as `skipped`, never as a failure and never as a success).
    When every provider is unavailable the result is `insufficient_data` -- output
    is never fabricated.
    """
    attempts = []
    if not enabled:
        return {"status": "skipped", "status_reason": "ENABLE_CROSS_PROVIDER_FALLBACK=False",
                "provider": None, "model": None, "output": None, "provider_attempts": []}
    route = [entry for entry in CROSS_PROVIDER_ROUTES.get(capability, ()) if entry[0] != "google"]
    contacted = 0
    for provider, model in route:
        adapter = TEXT_ADAPTERS.get(provider)
        if adapter is None:
            attempts.append({"provider": provider, "model": model, "ok": False,
                             "skipped": True, "error": "yerel/otomatik olmayan saglayici"})
            continue
        state = resolve_provider_credentials(provider, secret_getter)
        if state["available"] and contacted >= max(1, int(max_provider_attempts)):
            break
        if state["available"]:
            contacted += 1
        result = adapter(prompt, model=model, secret_getter=secret_getter,
                         transport=transport, capability=capability)
        validated = _validate_text_output(result.get("output"), validator) if result["ok"] else None
        invalid_schema = result["ok"] and validated is None
        if invalid_schema:
            result = dict(result)
            result.update({"ok": False, "status": "failed", "output": None,
                           "error": "saglayici ciktisi beklenen semaya uymadi",
                           "error_kind": "invalid_structured_output"})
        attempts.append({"provider": provider, "model": result.get("model"),
                         "ok": result["ok"], "skipped": result.get("skipped", False),
                         "error": result.get("error")})
        if result["ok"]:
            return {"status": "ok", "status_reason": "", "provider": provider,
                    "model": result["model"], "output": validated,
                    "provider_attempts": attempts}
        if invalid_schema and contacted < max(1, int(max_provider_attempts)):
            contacted += 1
            corrective_prompt = (
                prompt + "\n\nDUZELTME: Onceki yanit beklenen semaya uymadi. "
                "Aciklama veya Markdown eklemeden yalnizca gecerli JSON dondur."
            )
            retry = adapter(corrective_prompt, model=model, secret_getter=secret_getter,
                            transport=transport, capability=capability)
            retry_validated = (_validate_text_output(retry.get("output"), validator)
                               if retry.get("ok") else None)
            retry_ok = bool(retry.get("ok") and retry_validated is not None)
            attempts.append({"provider": provider, "model": retry.get("model"),
                             "ok": retry_ok, "skipped": retry.get("skipped", False),
                             "corrective_retry": True,
                             "error": (retry.get("error") if not retry.get("ok") else
                                       (None if retry_ok else
                                        "duzeltici yanit da beklenen semaya uymadi"))})
            if retry_ok:
                return {"status": "ok", "status_reason": "", "provider": provider,
                        "model": retry["model"], "output": retry_validated,
                        "provider_attempts": attempts}
    skipped_all = all(a.get("skipped") for a in attempts) if attempts else True
    return {
        "status": "insufficient_data" if skipped_all else "failed",
        "status_reason": ("hicbir capraz-saglayici kimlik bilgisi yok; temiz atlandi"
                          if skipped_all else
                          "tum capraz-saglayici denemeleri basarisiz; cikti UYDURULMADI"),
        "provider": None, "model": None, "output": None, "provider_attempts": attempts,
    }


def run_cross_provider_embeddings_fallback(texts, *, secret_getter=None, transport=None,
                                           enabled: bool = True,
                                           max_provider_attempts: int = 2,
                                           allow_local: bool = True,
                                           local_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2") -> dict:
    """Cloudflare BGE-M3, then an explicitly named local multilingual model.

    The result records its vector-space identity. Callers must never mix vectors
    with a different `vector_space` in the same index without rebuilding it.
    """
    if not enabled:
        return {"status": "skipped", "status_reason": "cross-provider fallback kapali",
                "provider": None, "model": None, "output": None, "provider_attempts": []}
    attempts = []
    if max_provider_attempts > 0:
        result = call_cloudflare_embeddings(texts, secret_getter=secret_getter, transport=transport)
        attempts.append({"provider": "cloudflare", "model": result.get("model"),
                         "ok": result.get("ok", False), "skipped": result.get("skipped", False),
                         "error": result.get("error")})
        if result.get("ok"):
            result["vector_space"] = f"cloudflare:{result['model']}:{result['dimension']}"
            result["provider_attempts"] = attempts
            return result
    if allow_local and max_provider_attempts > 1:
        try:
            from sentence_transformers import SentenceTransformer
            items = [texts] if isinstance(texts, str) else list(texts)
            vectors = SentenceTransformer(local_model).encode(items).tolist()
            dimension = len(vectors[0]) if vectors else 0
            attempts.append({"provider": "local", "model": local_model,
                             "ok": bool(vectors), "skipped": False, "error": None})
            if vectors:
                return {"provider": "local", "capability": "embeddings", "model": local_model,
                        "ok": True, "status": "ok", "output": vectors, "dimension": dimension,
                        "vector_space": f"local:{local_model}:{dimension}",
                        "provider_attempts": attempts}
        except Exception as exc:
            attempts.append({"provider": "local", "model": local_model, "ok": False,
                             "skipped": True, "error": f"yerel embedding kullanilamadi: {type(exc).__name__}"})
    skipped_all = all(a.get("skipped") for a in attempts) if attempts else True
    return {"status": "insufficient_data" if skipped_all else "failed",
            "status_reason": "embedding fallback rotasinda kullanilabilir cikti yok",
            "provider": None, "model": None, "output": None,
            "provider_attempts": attempts}


def run_cross_provider_tts_fallback(script_text: str, out_path, *, secret_getter=None,
                                    transport=None, enabled: bool = True,
                                    voice: str = "tr-TR-AhmetNeural",
                                    rate=None, volume=None,
                                    edge_synth=None, ffmpeg_bin=None,
                                    allow_edge: bool = True) -> dict:
    """Edge first (no credential), then Azure. Returns on the FIRST success.

    Edge leads because it needs no key: when the Google route is spent, a route
    step that cannot be blocked by a missing credential is the one most likely
    to actually produce narration. Azure remains the keyed fallback behind it.
    """
    if not enabled:
        return {"status": "skipped", "status_reason": "cross-provider fallback kapali",
                "provider": None, "model": None, "output": None, "provider_attempts": []}
    attempts = []

    if allow_edge:
        edge = call_edge_tts(script_text, out_path, voice=voice, rate=rate,
                             volume=volume, synth=edge_synth, ffmpeg_bin=ffmpeg_bin)
        attempts.append({"provider": "edge_tts", "model": edge.get("model"),
                         "ok": edge.get("ok", False),
                         "skipped": edge.get("skipped", False),
                         "error": edge.get("error")})
        if edge.get("ok"):
            edge["provider_attempts"] = attempts
            edge["status"] = "review_required"
            edge["inherits_previous_approval"] = False
            return edge

    result = call_azure_tts(script_text, out_path, voice=voice,
                            secret_getter=secret_getter, transport=transport)
    attempts.append({
        "provider": "azure_speech", "model": result.get("model"),
        "ok": result.get("ok", False), "skipped": result.get("skipped", False),
        "error": result.get("error"),
    })
    result["provider_attempts"] = attempts
    if result.get("ok"):
        result["status"] = "review_required"
        result["inherits_previous_approval"] = False
    elif all(a.get("skipped") for a in attempts):
        # Every step declined for a non-failure reason (no package, no key):
        # that is "nothing was available", not "TTS failed".
        result["status"] = "insufficient_data"
    return result
