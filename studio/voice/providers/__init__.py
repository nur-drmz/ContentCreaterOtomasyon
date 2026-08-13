from __future__ import annotations

import os

from .base import SynthesisResult, TextToSpeechProvider, VoiceProviderError
from .elevenlabs import ElevenLabsProvider
from .local_disabled import LocalDisabledProvider

# Adding another provider (OpenAI TTS, Azure, PlayHT, ...) means adding one
# class here and one registry entry - no call site outside this module ever
# references a vendor name, per docs/alpha/07-api-boundary-spec.md §4.
PROVIDERS: dict[str, type] = {
    LocalDisabledProvider.key: LocalDisabledProvider,
    ElevenLabsProvider.key: ElevenLabsProvider,
}


def provider_choices() -> list[tuple[str, str]]:
    return [(key, cls.display_name) for key, cls in PROVIDERS.items()]


def get_provider(key: str) -> TextToSpeechProvider:
    provider_cls = PROVIDERS.get(key)
    if provider_cls is None:
        raise VoiceProviderError("VOICE_PROVIDER_UNKNOWN", f"Unknown voice provider: {key}")
    return provider_cls()


def default_provider_key() -> str:
    return ElevenLabsProvider.key if os.environ.get("ELEVENLABS_API_KEY") else LocalDisabledProvider.key


__all__ = [
    "PROVIDERS",
    "SynthesisResult",
    "TextToSpeechProvider",
    "VoiceProviderError",
    "default_provider_key",
    "get_provider",
    "provider_choices",
]
