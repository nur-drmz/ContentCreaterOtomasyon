from __future__ import annotations

from decimal import Decimal
from typing import Any

from .base import SynthesisResult, VoiceProviderError


class LocalDisabledProvider:
    """The required local fallback: never silently substitutes for a real provider.

    This is the default provider when no API key is configured. It always
    fails clearly rather than generating placeholder audio, so "automatic
    fallback is prohibited" (docs/pre-alpha-local/06-security-privacy.md §3)
    is satisfied - a user always explicitly picks a provider, and if it's
    unusable they see why instead of a silent swap.
    """

    key = "local_disabled"
    display_name = "No provider configured"

    def estimate_cost(self, text: str, voice_settings: dict[str, Any]) -> Decimal:
        return Decimal("0")

    def synthesize(self, text: str, voice_settings: dict[str, Any]) -> SynthesisResult:
        raise VoiceProviderError(
            "VOICE_PROVIDER_NOT_CONFIGURED",
            "No text-to-speech provider is configured. Set the ELEVENLABS_API_KEY "
            "environment variable and select a real provider to generate voice-over audio.",
        )
