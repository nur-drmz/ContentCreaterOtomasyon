"""Minimal, dependency-free loader for the local Creator Console secret file.

Only explicitly supported variable names are accepted. Values are placed in the
server process environment and are never returned, printed or serialized.
Existing environment variables take precedence over the file by default.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


class EnvFileError(ValueError):
    pass


SUPPORTED_LOCAL_ENV_KEYS = frozenset({
    "GeminiKey",
    "GOOGLE_API_KEY",
    "YoutubeKEY",
    "YOUTUBE_DATA_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ELEVENLABS_API_KEY",
    "GROQ_API_KEY",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_AUTH_TOKEN",
    "AZURE_SPEECH_KEY",
    "AZURE_SPEECH_REGION",
    "TMDB_API_KEY",
    "HF_TOKEN",
    "YOUTUBE_OAUTH_CLIENT_SECRETS_PATH",
    "YOUTUBE_AUTOMATION_USE_GPU",
})

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_FILE_BYTES = 64 * 1024


def _parse_value(raw: str, line_number: int) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] in ("'", '"'):
        if len(value) < 2 or value[-1] != value[0]:
            raise EnvFileError(f".env.local satir {line_number}: kapanmamis tirnak")
        value = value[1:-1]
    if "\x00" in value or "\r" in value or "\n" in value:
        raise EnvFileError(f".env.local satir {line_number}: gecersiz kontrol karakteri")
    return value


def read_env_file(path) -> dict[str, str]:
    """Read supported key/value pairs. Returned values must remain server-only."""
    path = Path(path)
    if not path.is_file():
        return {}
    if path.stat().st_size > _MAX_FILE_BYTES:
        raise EnvFileError(f"yerel API dosyasi {_MAX_FILE_BYTES} bayttan buyuk")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise EnvFileError("yerel API dosyasi UTF-8 olmali") from exc

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise EnvFileError(f".env.local satir {line_number}: KEY=VALUE bekleniyordu")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not _KEY_RE.fullmatch(key):
            raise EnvFileError(f".env.local satir {line_number}: gecersiz anahtar adi")
        if key not in SUPPORTED_LOCAL_ENV_KEYS:
            raise EnvFileError(
                f".env.local satir {line_number}: desteklenmeyen degisken {key!r}"
            )
        if key in values:
            raise EnvFileError(f".env.local satir {line_number}: {key!r} iki kez tanimlanmis")
        values[key] = _parse_value(raw_value, line_number)
    return values


def load_env_file(path, *, environ=None, override: bool = False) -> dict:
    """Load non-empty values without ever returning or logging those values."""
    target = os.environ if environ is None else environ
    parsed = read_env_file(path)
    loaded_names, preserved_names = [], []
    for key, value in parsed.items():
        if not value:
            continue
        if not override and target.get(key):
            preserved_names.append(key)
            continue
        target[key] = value
        loaded_names.append(key)
    return {
        "exists": Path(path).is_file(),
        "loaded_count": len(loaded_names),
        "preserved_count": len(preserved_names),
        "loaded_names": tuple(sorted(loaded_names)),
        "preserved_names": tuple(sorted(preserved_names)),
    }
