from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Any

from django.conf import settings

from .base import SynthesisResult, VoiceProviderError

API_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
DEFAULT_MODEL_ID = "eleven_multilingual_v2"
# ElevenLabs' publicly documented sample voice ("Rachel"); callers can
# override via voice_settings["voice_id"].
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"
_VOICE_SETTING_KEYS = {"stability", "similarity_boost", "style", "use_speaker_boost"}
_WORDS_PER_SECOND = 2.5


class ElevenLabsProvider:
    """ElevenLabs text-to-speech, called only when the user explicitly picks it.

    Reads the API key from an environment variable - never from settings.py
    or the database - and never sent anywhere except ElevenLabs' own API.
    """

    key = "elevenlabs"
    display_name = "ElevenLabs"

    def _api_key(self) -> str:
        api_key = os.environ.get("ELEVENLABS_API_KEY")
        if not api_key:
            raise VoiceProviderError(
                "ELEVENLABS_API_KEY_MISSING",
                "Set the ELEVENLABS_API_KEY environment variable to use this provider.",
            )
        return api_key

    def estimate_cost(self, text: str, voice_settings: dict[str, Any]) -> Decimal:
        rate = Decimal(str(getattr(settings, "ELEVENLABS_COST_PER_CHARACTER", "0.00018")))
        return (rate * len(text)).quantize(Decimal("0.0001"))

    def synthesize(self, text: str, voice_settings: dict[str, Any]) -> SynthesisResult:
        api_key = self._api_key()
        voice_id = voice_settings.get("voice_id") or DEFAULT_VOICE_ID
        model_id = voice_settings.get("model_id") or DEFAULT_MODEL_ID
        payload = json.dumps(
            {
                "text": text,
                "model_id": model_id,
                "voice_settings": {
                    setting_key: value
                    for setting_key, value in voice_settings.items()
                    if setting_key in _VOICE_SETTING_KEYS
                },
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            API_URL.format(voice_id=voice_id),
            data=payload,
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                audio_bytes = response.read()
        except urllib.error.HTTPError as exc:
            raise VoiceProviderError(
                "ELEVENLABS_REQUEST_FAILED",
                f"ElevenLabs returned HTTP {exc.code}: {exc.reason}",
            ) from exc
        except urllib.error.URLError as exc:
            raise VoiceProviderError(
                "ELEVENLABS_UNREACHABLE", f"Could not reach ElevenLabs: {exc.reason}"
            ) from exc

        words = max(len(text.split()), 1)
        duration_seconds = round(words / _WORDS_PER_SECOND, 2)
        return SynthesisResult(
            audio_bytes=audio_bytes,
            duration_seconds=duration_seconds,
            cost=self.estimate_cost(text, voice_settings),
        )
