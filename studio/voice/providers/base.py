from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol


@dataclass(frozen=True)
class SynthesisResult:
    audio_bytes: bytes
    duration_seconds: float
    cost: Decimal


class VoiceProviderError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)

    def __str__(self) -> str:
        return self.detail


class TextToSpeechProvider(Protocol):
    """A pluggable voice-over backend.

    Matches the interface already specified in docs/alpha/07-api-boundary-spec.md
    (`TextToSpeechProvider.synthesize(text, voiceSettings) -> {audioRef, durationSec, cost}`)
    so this stays consistent with the project's own future-facing design.
    Concrete providers are injected by key (studio/voice/providers/__init__.py);
    no call site should ever reference a vendor name directly.
    """

    key: str
    display_name: str

    def estimate_cost(self, text: str, voice_settings: dict[str, Any]) -> Decimal: ...

    def synthesize(self, text: str, voice_settings: dict[str, Any]) -> SynthesisResult: ...
