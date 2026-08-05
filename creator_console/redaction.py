"""Process-local secret redaction registry.

WHY THIS EXISTS (a real leak this project shipped)
--------------------------------------------------
`google_ai.sanitize_google_error(text)` redacts things that LOOK like credentials
(`AIza…`, `Authorization: …`, `?key=…`) and, when a `CredentialRef` is passed, the
exact configured value. But the console's persistence paths -- `JobStore.finish()`
storing an error, `AuditLog` recording a detail, `ApiError` building a response --
have no credential in scope, so they only got pattern matching.

A provider whose key does NOT match a known pattern (a Groq/Azure/Cloudflare token,
or any future format) could therefore be written verbatim into a job record, an
audit event or an HTTP response via an exception message. That was reproducible.

So the resolver registers every secret value it resolves into this module-private
set, and every persistence path scrubs through `scrub()`. The set lives in memory
only for the life of the process -- it is never serialized, logged, or exposed, and
`registered_count()` deliberately returns a COUNT, not the values.
"""
from __future__ import annotations

import re

from local_pipeline import google_ai

_REGISTERED: set = set()

#: Below this length a "secret" is more likely to be a common substring
#: (a region name like `westeurope`, a short id) and blanket-replacing it would
#: corrupt unrelated messages.
MIN_REDACTABLE_LENGTH = 8

REDACTED = "***REDACTED***"


def register_secret(value) -> bool:
    """Remember a value that must never appear in output. Returns whether it was
    registered (short/empty values are rejected on purpose)."""
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    if len(candidate) < MIN_REDACTABLE_LENGTH:
        return False
    _REGISTERED.add(candidate)
    return True


def registered_count() -> int:
    """Count only -- never the values themselves."""
    return len(_REGISTERED)


def forget_all() -> None:
    """Test helper; production code never needs this."""
    _REGISTERED.clear()


#: Defence in depth for secrets that never went through `SecretResolver` (an
#: operator env var read directly by a third-party SDK, a token inside a
#: provider's own error body). Such a value is not in the registry and need not
#: look like `AIza…`, so long high-entropy tokens are redacted on sight.
#:
#: This is a BACKSTOP, not the primary control -- `errors.py` is, because it
#: never puts exception text in a response at all.
_HIGH_ENTROPY_RE = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")
_ENTROPY_MIN_CLASSES = 3


def _looks_high_entropy(token: str) -> bool:
    """True for token-shaped strings: long, mixed-case/digit, not a sentence."""
    classes = sum((
        any(c.islower() for c in token),
        any(c.isupper() for c in token),
        any(c.isdigit() for c in token),
        any(c in "_-" for c in token),
    ))
    if classes < _ENTROPY_MIN_CLASSES:
        return False
    # A hex digest is high entropy but is deliberately PUBLIC in this project
    # (approval hashes are shown to the user and pasted back), so keep those.
    if len(token) in (32, 40, 56, 64, 96, 128) and all(c in "0123456789abcdefABCDEF" for c in token):
        return False
    return True


def scrub_text(text, *, max_len: int = 2000, redact_high_entropy: bool = True) -> str:
    """Exact registered values, then high-entropy tokens, then patterns."""
    result = str(text)
    if _REGISTERED:
        # Longest first, so a value that contains another is fully replaced.
        for secret in sorted(_REGISTERED, key=len, reverse=True):
            if secret in result:
                result = result.replace(secret, REDACTED)
    if redact_high_entropy:
        result = _HIGH_ENTROPY_RE.sub(
            lambda m: REDACTED if _looks_high_entropy(m.group(0)) else m.group(0), result)
    return google_ai.sanitize_google_error(result, max_len=max_len)


def scrub(value, *, max_len: int = 2000):
    """Recursively scrub any JSON-shaped value before it is persisted or sent."""
    if value is None:
        return None
    if isinstance(value, str):
        return scrub_text(value, max_len=max_len)
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        return {str(k): scrub(v, max_len=max_len) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, max_len=max_len) for v in value]
    return scrub_text(value, max_len=max_len)


def contains_registered_secret(blob) -> list:
    """Test helper: which registered values appear in `blob`.

    Returns INDEXES into the sorted registry, never the values, so a failing
    assertion message cannot itself leak the secret.
    """
    text = blob if isinstance(blob, str) else str(blob)
    ordered = sorted(_REGISTERED)
    return [index for index, secret in enumerate(ordered) if secret in text]
