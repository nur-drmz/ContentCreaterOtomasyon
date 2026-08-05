"""Server-side secret REFERENCES. Values never leave this process.

WHY THIS EXISTS
---------------
The console must show "Gemini: configured (GeminiKey)" without ever putting the
key in an HTTP response, a log line, a job record, an audit event, an artifact or
the browser bundle. So the API layer only ever handles `SecretReference` objects,
which carry the alias chain and a boolean -- never the value.

The value itself is resolved on demand, inside the pipeline service, through the
canonical `google_ai.resolve_secret`. Nothing here caches or persists it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from local_pipeline import google_ai

from .redaction import register_secret

#: Logical secret -> what it unlocks in the console UI.
CONSOLE_SECRET_KINDS = {
    "google_api": "Gemini script, TTS, embedding ve grounded arastirma rotalari",
    "youtube_data_api": "YouTube Data API v3 arastirma yan kolu (yalnizca okuma)",
    "openai": "Opsiyonel OpenAI script/TTS yedegi",
    "anthropic": "Opsiyonel Anthropic script yedegi",
    "elevenlabs": "Opsiyonel ElevenLabs seslendirme",
    "groq": "Opsiyonel capraz-saglayici metin yedegi",
    "cloudflare_account_id": "Opsiyonel Cloudflare Workers AI yedegi",
    "cloudflare_auth_token": "Opsiyonel Cloudflare Workers AI yedegi",
    "azure_speech_key": "Opsiyonel Azure Neural TTS yedegi",
    "azure_speech_region": "Opsiyonel Azure Neural TTS yedegi",
    "tmdb": "Opsiyonel film/dizi trend metadata zenginlestirmesi",
    "huggingface": "Opsiyonel Hugging Face model indirme kimlik dogrulamasi",
}

CONSOLE_SECRET_ALIASES = {
    **google_ai.SECRET_ALIASES,
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "elevenlabs": ("ELEVENLABS_API_KEY",),
    "tmdb": ("TMDB_API_KEY",),
    "huggingface": ("HF_TOKEN",),
}


@dataclass(frozen=True)
class SecretReference:
    """Everything the UI may know about a secret. Deliberately value-free."""

    kind: str
    configured: bool
    alias_used: str | None
    aliases_supported: tuple = field(default_factory=tuple)
    purpose: str = ""

    def to_public_dict(self) -> dict:
        return {
            "kind": self.kind,
            "state": "configured" if self.configured else "missing",
            "alias_used": self.alias_used,
            "aliases_supported": list(self.aliases_supported),
            "purpose": self.purpose,
        }


class SecretResolver:
    """Resolves secrets server-side only.

    `getter` is injected (os.environ by default, a fixture dict in tests), so no
    test ever needs a real key and no code path can be surprised by an ambient
    credential.
    """

    def __init__(self, getter=None):
        self._getter = getter or (lambda name: os.environ.get(name))

    def reference(self, kind: str) -> SecretReference:
        credential = self.credential(kind)
        # Registering here means every persistence path can scrub this value even
        # when it has no credential in scope (see redaction.py).
        if credential.configured:
            register_secret(credential.value)
        return SecretReference(
            kind=kind,
            configured=credential.configured,
            alias_used=credential.alias_used,
            aliases_supported=tuple(credential.aliases_tried),
            purpose=CONSOLE_SECRET_KINDS.get(kind, ""),
        )

    def references(self) -> list:
        return [self.reference(kind) for kind in CONSOLE_SECRET_KINDS]

    def credential(self, kind: str):
        """The real `CredentialRef`. ONLY the pipeline service may call this, and
        the returned object still refuses to print or serialize its value."""
        if kind in google_ai.SECRET_ALIASES:
            credential = google_ai.resolve_secret(kind, self._getter)
        else:
            aliases = CONSOLE_SECRET_ALIASES.get(kind)
            if not aliases:
                raise KeyError(f"bilinmeyen secret turu: {kind!r}")
            alias_used, value = None, None
            for alias in aliases:
                candidate = self._getter(alias)
                if candidate:
                    alias_used, value = alias, candidate
                    break
            credential = google_ai.CredentialRef(kind, alias_used, aliases, value)
        if credential.configured:
            register_secret(credential.value)
        return credential

    def public_state(self) -> list:
        return [ref.to_public_dict() for ref in self.references()]
