"""Canonical, task-aware Google AI (Gemini / Gemma) router for this project.

WHY THIS MODULE EXISTS
----------------------
Before this module every notebook built its own `genai.Client(...)` and
hardcoded `model='gemini-3.6-flash'` inline. That made it impossible to (a) know
which models the free project can actually reach, (b) keep the free-tier
RPM/TPM/RPD budget from being burned by an accidental fan-out, and (c) fail in a
predictable, auditable way. This module is the ONE place that:

  1. creates a client (secret-independent, dependency-injected)
  2. owns the model registry + configured free-tier quota defaults
  3. discovers capabilities via `client.models.list()` and intersects them with
     an explicit allowlist
  4. picks a deterministic route per task
  5. keeps a persistent, ESTIMATED quota ledger
  6. retries / backs off / trips a circuit breaker
  7. validates structured output syntactically AND semantically
  8. logs every provider attempt into the artifact
  9. sanitizes every error before it is printed or persisted
 10. exposes a separate, explicitly named command per Google capability

Notebooks must not re-implement any of the above. They import this module.

VERIFIED AGAINST OFFICIAL GOOGLE AI DOCUMENTATION (see README "Kaynak dogrulama")
--------------------------------------------------------------------------------
- Model IDs come from https://ai.google.dev/gemini-api/docs/models
- `client.models.generate_content(model=..., contents=..., config=...)`,
  `client.models.embed_content(...)` and `client.models.list()` are the
  documented google-genai surfaces
  (https://ai.google.dev/gemini-api/docs/migrate). `config` may be a plain dict
  or a `types.*Config` object; this module passes DICTS so it stays importable
  and mockable without the SDK installed.
- `thinking_level` accepts "minimal" | "low" | "medium" | "high" (per-model),
  https://ai.google.dev/gemini-api/docs/thinking
- CORRECTION vs. the project brief: `temperature`, `top_p`, `top_k` and
  `candidate_count` are NOT removed from the API/SDK. Official guidance for
  Gemini 3 is to leave temperature at its default 1.0. This module therefore
  simply NEVER SENDS them for Gemini 3.x models (see `_build_generate_config`),
  which achieves the brief's intent without asserting a false deprecation.
- CORRECTION vs. the project brief: `gemini-2.5-flash-lite` is NOT listed as a
  Google Search grounding model (https://ai.google.dev/gemini-api/docs/google-search),
  so it is NOT in the grounded-research route.
- CORRECTION vs. the project brief: `gemini-embedding-2` does NOT support
  `task_type`; task instructions go in the prompt text
  (https://ai.google.dev/gemini-api/docs/embeddings). The task prefix is
  recorded in the artifact instead.
- Free-tier RPM/TPM/RPD numbers are NOT published in the docs; the docs point to
  AI Studio and state limits are "applied per project, not per API key" and that
  "RPD quotas reset at midnight Pacific time"
  (https://ai.google.dev/gemini-api/docs/rate-limits). Everything in
  `MODEL_REGISTRY` is therefore a CONFIGURABLE DEFAULT, never an authority.

NEVER: uploads, publishes, prints a secret, serializes a secret, or rotates
between Google accounts/projects to dodge a quota.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import struct
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Hard policy switches. These are deliberately module-level constants, not
# function arguments, so that flipping one is a visible source change that the
# regression suite can see.
# ---------------------------------------------------------------------------

#: Rotating between Google ACCOUNTS/PROJECTS to reset a free-tier quota is quota
#: circumvention. It is off, and `assert_no_account_quota_rotation()` refuses to
#: run if anyone flips it. Changing MODELS inside one legitimate project (or
#: later switching to a different PROVIDER) is fine and is what the router does.
ALLOW_ACCOUNT_QUOTA_ROTATION = False

#: Free-tier submissions may be used by Google to improve their products and may
#: be read by human reviewers (https://ai.google.dev/gemini-api/terms). Every
#: artifact this module writes carries this notice verbatim.
FREE_TIER_DATA_NOTICE = (
    "Gemini API ucretsiz katman: gonderilen icerik ve uretilen yanitlar Google "
    "tarafindan urunleri gelistirmek icin kullanilabilir ve insan gozden "
    "gecirenler tarafindan okunabilir (ai.google.dev/gemini-api/terms). Telif "
    "hakli ham medyayi ucretsiz katmana gondermeyin."
)

#: Raw (copyrighted) source video/audio is NEVER uploaded by default; only
#: locally derived transcript windows / scores / metadata are sent.
ALLOW_RAW_MEDIA_TO_FREE_TIER_DEFAULT = False

STATUS_VALUES = {
    "experimental", "development_placeholder", "review_required",
    "approved", "insufficient_data", "failed", "production_ready",
}

SCHEMA_VERSION = 1


class GoogleAIPolicyError(RuntimeError):
    """Raised when a hard policy switch is violated."""


def assert_no_account_quota_rotation() -> None:
    """Fails loudly if account/key rotation was ever enabled.

    Called at the top of `execute_google_route()`, so no Google call can be made
    in a build where someone turned rotation on.
    """
    if ALLOW_ACCOUNT_QUOTA_ROTATION:
        raise GoogleAIPolicyError(
            "ALLOW_ACCOUNT_QUOTA_ROTATION=True: birden fazla Google hesabi/projesi "
            "arasinda anahtar dondurerek RPD/RPM/TPM limitlerini asmak kota "
            "ihlalidir ve bu projede yasaktir. Router yalnizca AYNI proje icinde "
            "MODEL degistirebilir."
        )


# ---------------------------------------------------------------------------
# Secrets. Values are wrapped so they cannot be printed, formatted, or
# json-serialized by accident.
# ---------------------------------------------------------------------------

#: Exact Colab Secret names the user already stores, plus backward-compatible
#: aliases, in resolution order.
SECRET_ALIASES = {
    "google_api": ("GeminiKey", "GOOGLE_API_KEY"),
    "youtube_data_api": ("YoutubeKEY", "YOUTUBE_DATA_API_KEY"),
    "groq": ("GROQ_API_KEY",),
    "nvidia": ("NVIDIA_API_KEY",),
    "cloudflare_account_id": ("CLOUDFLARE_ACCOUNT_ID",),
    "cloudflare_auth_token": ("CLOUDFLARE_AUTH_TOKEN",),
    "azure_speech_key": ("AZURE_SPEECH_KEY",),
    "azure_speech_region": ("AZURE_SPEECH_REGION",),
}

DEFAULT_CREDENTIAL_PROFILE = "default"
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

COLAB_SECRET_HELP = {
    "google_api": (
        "Colab sol paneldeki anahtar (Secrets) simgesine tikla -> '+ Yeni gizli "
        "anahtar' -> Ad: 'GeminiKey' (veya eski ad 'GOOGLE_API_KEY'), Deger: "
        "https://aistudio.google.com/apikey adresinden aldigin anahtar. "
        "'Notebook erisimi' anahtarini ACMAYI unutma -- kapaliysa userdata.get() "
        "SecretNotFoundError/NotebookAccessError verir."
    ),
    "youtube_data_api": (
        "Colab Secrets -> Ad: 'YoutubeKEY' (veya eski ad 'YOUTUBE_DATA_API_KEY'), "
        "Deger: console.cloud.google.com -> YouTube Data API v3 anahtari. "
        "'Notebook erisimi' acik olmali."
    ),
}


class CredentialRef:
    """A resolved secret whose value can be used but never accidentally shown.

    `repr()`/`str()`/`format()` return only the sanitized state, and the object
    is not JSON-serializable, so a credential cannot leak into an artifact, a
    log line, or an exception message through ordinary formatting.
    """

    __slots__ = ("kind", "alias_used", "aliases_tried", "profile", "_value")

    def __init__(self, kind: str, alias_used: str | None, aliases_tried, value,
                 profile: str = DEFAULT_CREDENTIAL_PROFILE):
        self.kind = kind
        self.alias_used = alias_used
        self.aliases_tried = tuple(aliases_tried)
        self.profile = validate_credential_profile(profile)
        self._value = value or None

    @property
    def configured(self) -> bool:
        return bool(self._value)

    @property
    def value(self) -> str | None:
        """The raw secret. Only `build_google_client()` should read this."""
        return self._value

    def state(self) -> str:
        return "configured" if self.configured else "missing"

    def to_public_dict(self) -> dict:
        """Sanitized, artifact-safe description. Contains NO secret material."""
        return {
            "kind": self.kind,
            "state": self.state(),
            "profile": self.profile,
            "alias_used": self.alias_used,
            "aliases_supported": list(self.aliases_tried),
        }

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (f"<CredentialRef kind={self.kind} state={self.state()} "
                f"profile={self.profile} alias={self.alias_used}>")

    __str__ = __repr__

    def __format__(self, spec) -> str:  # pragma: no cover - trivial
        return repr(self)


def _default_secret_getter(name: str):
    return os.environ.get(name)


def validate_credential_profile(profile: str) -> str:
    """Validates a public, non-secret profile label used only for manual selection."""
    value = str(profile or "").strip()
    if not _PROFILE_NAME_RE.fullmatch(value):
        raise ValueError(
            "credential profile 1-64 karakter olmali ve yalnizca harf, rakam, '.', '_' veya '-' icermeli"
        )
    return value


def resolve_profile_secret(kind: str, profile: str, profiles: dict,
                           secret_getter=None) -> CredentialRef:
    """Resolves exactly the secret(s) assigned to one manually selected profile.

    This function never searches another profile and never rotates after a quota
    error. `profiles` maps public profile labels to one secret name or an ordered
    tuple of backward-compatible aliases belonging to that SAME profile.
    """
    selected = validate_credential_profile(profile)
    if not isinstance(profiles, dict) or selected not in profiles:
        available = sorted(str(k) for k in (profiles or {}))
        raise ValueError(f"bilinmeyen credential profile: {selected!r}; mevcut: {available}")
    configured = profiles[selected]
    aliases = (configured,) if isinstance(configured, str) else tuple(configured)
    if not aliases or any(not isinstance(a, str) or not a.strip() for a in aliases):
        raise ValueError(f"{selected!r} profili icin en az bir gecerli secret adi gerekli")
    getter = secret_getter or _default_secret_getter
    for alias in aliases:
        try:
            value = getter(alias)
        except Exception:
            value = None
        if value:
            return CredentialRef(kind, alias, aliases, value, profile=selected)
    return CredentialRef(kind, None, aliases, None, profile=selected)


def resolve_secret(kind: str, secret_getter=None) -> CredentialRef:
    """Resolves one logical secret through its alias chain.

    `secret_getter(name)` is injected (Colab `userdata.get`, `os.environ.get`, or
    a test double) so this module never imports `google.colab` itself. A getter
    that raises for an unset/locked secret is treated as "not found" for that
    alias, never as a crash.
    """
    aliases = SECRET_ALIASES.get(kind)
    if not aliases:
        raise ValueError(f"bilinmeyen secret turu: {kind!r} (beklenen: {sorted(SECRET_ALIASES)})")
    getter = secret_getter or _default_secret_getter
    for alias in aliases:
        try:
            value = getter(alias)
        except Exception:
            value = None
        if value:
            return CredentialRef(kind, alias, aliases, value)
    return CredentialRef(kind, None, aliases, None)


def profile_scoped_filename(filename: str, profile: str) -> str:
    """Adds a sanitized profile suffix while preserving the legacy default path."""
    selected = validate_credential_profile(profile)
    if selected == DEFAULT_CREDENTIAL_PROFILE:
        return filename
    path = Path(filename)
    return f"{path.stem}.{selected}{path.suffix}"


def describe_credential_state(credential: CredentialRef) -> str:
    """'configured (GeminiKey)' / 'missing (GeminiKey, GOOGLE_API_KEY denendi)'.

    Never contains the value.
    """
    if credential.configured:
        return f"configured ({credential.alias_used})"
    return f"missing ({', '.join(credential.aliases_tried)} denendi)"


def credential_help_text(kind: str) -> str:
    return COLAB_SECRET_HELP.get(kind, "Colab Secrets panelinden ilgili anahtari ekle.")


# ---------------------------------------------------------------------------
# Client creation (dependency-injected; no hidden clients in business logic).
# ---------------------------------------------------------------------------

class GoogleCredentialMissing(RuntimeError):
    pass


def build_google_client(credential: CredentialRef, client_factory=None):
    """Builds a google-genai client from ONE credential.

    Takes exactly one `CredentialRef` -- never a list, never a pool. That is the
    structural reason multi-account quota rotation cannot be expressed by this
    module (see `assert_no_account_quota_rotation`).

    `client_factory(api_key=...)` is injected in tests so no real SDK/network is
    involved.
    """
    if not isinstance(credential, CredentialRef):
        raise TypeError(
            "build_google_client tek bir CredentialRef bekler (hesap havuzu/rotasyon desteklenmez)."
        )
    if not credential.configured:
        raise GoogleCredentialMissing(
            f"Google API anahtari yok. {credential_help_text(credential.kind)}"
        )
    if client_factory is not None:
        return client_factory(api_key=credential.value)
    from google import genai  # imported lazily so tests need no SDK
    return genai.Client(api_key=credential.value)


def _resolve_client(client=None, client_factory=None, credential=None):
    if client is not None:
        return client
    if credential is None:
        raise GoogleCredentialMissing(
            "Ne client ne credential verildi; gizli anahtar okunmadan istemci kurulamaz."
        )
    return build_google_client(credential, client_factory=client_factory)


# ---------------------------------------------------------------------------
# Model registry.
#
# `rpm`/`tpm`/`rpd` are CONFIGURABLE DEFAULTS for THIS project's free tier, not
# universal facts. Server responses and live AI Studio limits win. A model with
# rpd=0 or rpm=0 is never eligible for an automatic route.
# ---------------------------------------------------------------------------

ACTION_TEXT = "generate_content"
ACTION_STRUCTURED = "structured_output"
ACTION_GROUNDED = "google_search_grounding"
ACTION_TTS = "tts"
ACTION_EMBED = "embed_content"
ACTION_AGENT = "agentic_research"


def _spec(*, actions, rpm, tpm, rpd, thinking_levels=(), family="gemini",
          experimental=False, notes="", api="generate_content"):
    return {
        "actions": tuple(actions),
        "configured_rpm": rpm,
        "configured_tpm": tpm,
        "configured_rpd": rpd,
        "thinking_levels": tuple(thinking_levels),
        "family": family,
        "experimental": bool(experimental),
        "notes": notes,
        "api": api,
        "quota_source": "project_configured_default",
    }


MODEL_REGISTRY = {
    # --- A: high-quality creative reasoning -------------------------------
    "gemini-3.6-flash": _spec(
        actions=(ACTION_TEXT, ACTION_STRUCTURED, ACTION_GROUNDED),
        rpm=5, tpm=250_000, rpd=20,
        thinking_levels=("minimal", "low", "medium", "high"),
    ),
    "gemini-3.5-flash": _spec(
        actions=(ACTION_TEXT, ACTION_STRUCTURED, ACTION_GROUNDED),
        rpm=5, tpm=250_000, rpd=20,
        thinking_levels=("minimal", "low", "medium", "high"),
    ),
    # --- B: high-volume structured work -----------------------------------
    "gemini-3.5-flash-lite": _spec(
        actions=(ACTION_TEXT, ACTION_STRUCTURED, ACTION_GROUNDED),
        rpm=15, tpm=250_000, rpd=500,
        thinking_levels=("minimal", "low", "medium", "high"),
    ),
    "gemini-3.1-flash-lite": _spec(
        actions=(ACTION_TEXT, ACTION_STRUCTURED),
        rpm=15, tpm=250_000, rpd=500,
        thinking_levels=("minimal", "low", "medium", "high"),
    ),
    # --- C: search-grounded research (Gemini 2.5 grounding quota) ---------
    "gemini-2.5-flash": _spec(
        actions=(ACTION_TEXT, ACTION_STRUCTURED, ACTION_GROUNDED),
        rpm=5, tpm=250_000, rpd=20,
        thinking_levels=("low", "medium", "high"),
        notes="Google Search grounding icin dogrulanmis model (docs/google-search).",
    ),
    "gemini-2.5-flash-lite": _spec(
        actions=(ACTION_TEXT, ACTION_STRUCTURED),
        rpm=10, tpm=250_000, rpd=20,
        thinking_levels=("low", "medium", "high"),
        notes=(
            "DUZELTME: resmi grounding model listesinde YOK, bu yuzden "
            "grounded_research rotasina alinmadi."
        ),
    ),
    # --- D: TTS (preview -> her cikti review_required) ---------------------
    "gemini-3.1-flash-tts-preview": _spec(
        actions=(ACTION_TTS,), rpm=3, tpm=10_000, rpd=10,
        experimental=True, api="generate_content_audio",
        notes="preview model; cikti her zaman review_required.",
    ),
    "gemini-2.5-flash-preview-tts": _spec(
        actions=(ACTION_TTS,), rpm=3, tpm=10_000, rpd=10,
        experimental=True, api="generate_content_audio",
        notes="preview model; cikti her zaman review_required.",
    ),
    # --- E: embeddings -----------------------------------------------------
    "gemini-embedding-2": _spec(
        actions=(ACTION_EMBED,), rpm=100, tpm=30_000, rpd=1_000,
        api="embed_content",
        notes=(
            "task_type DESTEKLENMEZ (docs/embeddings); gorev yonergesi prompt "
            "metnine 'task: ... | ...' onekiyle yazilir."
        ),
    ),
    # --- Gemma (open models on the Gemini API) -----------------------------
    "gemma-4-31b-it": _spec(
        actions=(ACTION_TEXT,), rpm=30, tpm=16_000, rpd=14_400, family="gemma",
        notes="response_schema desteklenmez; JSON prompt ile istenir ve ayristirilir.",
    ),
    "gemma-4-26b-a4b-it": _spec(
        actions=(ACTION_TEXT,), rpm=30, tpm=16_000, rpd=14_400, family="gemma",
        notes="response_schema desteklenmez; JSON prompt ile istenir ve ayristirilir.",
    ),
    # --- F: antigravity (experimental, opt-in only) ------------------------
    "antigravity-preview-05-2026": _spec(
        actions=(ACTION_AGENT,), rpm=60, tpm=100_000, rpd=100,
        family="antigravity", experimental=True,
        notes="deneysel ajan modeli; yapisal cikti garantisi YOK, varsayilan KAPALI.",
    ),
    # --- Zero-quota on this free project: registered so they are explicitly
    #     EXCLUDED (and the exclusion is auditable), never routed. ----------
    "gemini-2.5-pro": _spec(
        actions=(ACTION_TEXT, ACTION_STRUCTURED, ACTION_GROUNDED),
        rpm=0, tpm=0, rpd=0,
        notes="bu ucretsiz projede 0 kota; otomatik rotaya ASLA girmez.",
    ),
    "gemini-3.1-pro-preview": _spec(
        actions=(ACTION_TEXT, ACTION_STRUCTURED), rpm=0, tpm=0, rpd=0,
        experimental=True, notes="bu ucretsiz projede 0 kota; otomatik rotaya girmez.",
    ),
    "gemini-3.1-flash-image": _spec(
        actions=(ACTION_TEXT,), rpm=0, tpm=0, rpd=0,
        notes="goruntu uretimi bu ucretsiz projede 0 kota; otomatik rotaya girmez.",
    ),
    "gemini-3-pro-image": _spec(
        actions=(ACTION_TEXT,), rpm=0, tpm=0, rpd=0,
        notes="goruntu uretimi bu ucretsiz projede 0 kota; otomatik rotaya girmez.",
    ),
    "gemini-2.5-flash-image": _spec(
        actions=(ACTION_TEXT,), rpm=0, tpm=0, rpd=0,
        notes="goruntu uretimi bu ucretsiz projede 0 kota; otomatik rotaya girmez.",
    ),
}

#: The explicit allowlist. `client.models.list()` output is INTERSECTED with
#: this; a model the server exposes but that is not here is never used.
MODEL_ALLOWLIST = tuple(MODEL_REGISTRY)


def has_nonzero_quota(model_id: str) -> bool:
    spec = MODEL_REGISTRY.get(model_id)
    if not spec:
        return False
    return spec["configured_rpm"] > 0 and spec["configured_rpd"] > 0


def is_automatic_route_eligible(model_id: str) -> bool:
    """A model may enter an automatic route only if it is allowlisted AND has a
    nonzero configured free-tier quota."""
    return model_id in MODEL_REGISTRY and has_nonzero_quota(model_id)


# ---------------------------------------------------------------------------
# Task routes (Phase 3). Order is the fallback order; a successful first call
# STOPS the route. The master pipeline never fans out.
# ---------------------------------------------------------------------------

ROUTE_A_CREATIVE = ("gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemma-4-31b-it")
ROUTE_B_BULK = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemma-4-26b-a4b-it")
# 2026-08-18 canli pilotu: gemini-2.5-flash `models.list()` icinde GORUNUYOR
# ama cagri 404 donuyor -- "no longer available to new users. Please update
# your code to use models/gemini-3.6-flash". Tek modelli bir rota, o model
# emekliye ayrildigi anda asamayi tamamen olduruyor; gorunurluk
# cagrilabilirlik demek degil. Bu yuzden grounded rota da yedekli.
ROUTE_C_GROUNDED = ("gemini-3.6-flash", "gemini-3.5-flash")
ROUTE_D_TTS = ("gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts")
ROUTE_E_EMBED = ("gemini-embedding-2",)
ROUTE_F_AGENT = ("antigravity-preview-05-2026",)

TASK_ROUTES = {
    "hotmoment_review": ROUTE_A_CREATIVE,
    "script_generation": ROUTE_A_CREATIVE,
    "consistency_review": ROUTE_A_CREATIVE,
    "quality_audit": ROUTE_A_CREATIVE,
    "bulk_classification": ROUTE_B_BULK,
    "viral_interpretation": ROUTE_B_BULK,
    "metadata_generation": ROUTE_B_BULK,
    "transcript_cleanup": ROUTE_B_BULK,
    "grounded_research": ROUTE_C_GROUNDED,
    "tts": ROUTE_D_TTS,
    "embeddings": ROUTE_E_EMBED,
    "antigravity_research": ROUTE_F_AGENT,
}

#: Per-(task, model) thinking level. Absent => no thinking_level is sent.
TASK_THINKING_LEVELS = {
    "hotmoment_review": {"gemini-3.6-flash": "medium", "gemini-3.5-flash": "medium",
                         "gemini-3.5-flash-lite": "low"},
    "script_generation": {"gemini-3.6-flash": "medium", "gemini-3.5-flash": "medium",
                          "gemini-3.5-flash-lite": "low"},
    "consistency_review": {"gemini-3.6-flash": "medium", "gemini-3.5-flash": "medium",
                           "gemini-3.5-flash-lite": "low"},
    "quality_audit": {"gemini-3.6-flash": "medium", "gemini-3.5-flash": "medium",
                      "gemini-3.5-flash-lite": "low"},
    "bulk_classification": {"gemini-3.5-flash-lite": "minimal", "gemini-3.1-flash-lite": "minimal"},
    "viral_interpretation": {"gemini-3.5-flash-lite": "minimal", "gemini-3.1-flash-lite": "minimal"},
    "metadata_generation": {"gemini-3.5-flash-lite": "minimal", "gemini-3.1-flash-lite": "minimal"},
    "transcript_cleanup": {"gemini-3.5-flash-lite": "minimal", "gemini-3.1-flash-lite": "minimal"},
    "grounded_research": {"gemini-3.6-flash": "low", "gemini-3.5-flash": "low"},
}

#: Task -> the capability the chosen model must actually support.
TASK_REQUIRED_ACTION = {
    "hotmoment_review": ACTION_TEXT,
    "script_generation": ACTION_TEXT,
    "consistency_review": ACTION_TEXT,
    "quality_audit": ACTION_TEXT,
    "bulk_classification": ACTION_TEXT,
    "viral_interpretation": ACTION_TEXT,
    "metadata_generation": ACTION_TEXT,
    "transcript_cleanup": ACTION_TEXT,
    "grounded_research": ACTION_GROUNDED,
    "tts": ACTION_TTS,
    "embeddings": ACTION_EMBED,
    "antigravity_research": ACTION_AGENT,
}

#: Task -> default output-token ceiling. This is a RUNAWAY BOUND, not a way to
#: shave tokens off the normal path: on Gemini the thinking budget is spent from
#: the same `max_output_tokens` allowance, so a tight cap does not save money --
#: it truncates the JSON mid-object, fails validation, and costs a SECOND
#: request. Every value here is therefore deliberately generous, sized to the
#: largest legitimate response for the task plus its thinking level. Callers may
#: still pass an explicit `max_output_tokens=` and it wins.
TASK_MAX_OUTPUT_TOKENS = {
    # Route A, thinking_level=medium: one long-form chapter (~1500 TR words is
    # roughly 3k tokens) plus summary/fact lists, plus the thinking budget.
    "hotmoment_review": 16_384,
    "script_generation": 16_384,
    "consistency_review": 16_384,
    "quality_audit": 16_384,
    # Route B, thinking_level=minimal: short structured payloads only.
    "bulk_classification": 8_192,
    "viral_interpretation": 8_192,
    "metadata_generation": 8_192,
    "transcript_cleanup": 8_192,
    # Route C: grounded answers stay short; the citations live in metadata.
    "grounded_research": 8_192,
}


def max_output_tokens_for(task: str) -> int | None:
    """The configured ceiling for a text task, or None when the task has no
    default (TTS/embeddings do not go through `_build_generate_config`)."""
    return TASK_MAX_OUTPUT_TOKENS.get(task)


DEFAULT_MAX_GOOGLE_MODEL_ATTEMPTS = 4


def thinking_level_for(task: str, model_id: str) -> str | None:
    """Returns the configured thinking_level, but only if the registry says this
    model actually accepts it (Gemma has no thinking config at all)."""
    level = TASK_THINKING_LEVELS.get(task, {}).get(model_id)
    if not level:
        return None
    spec = MODEL_REGISTRY.get(model_id) or {}
    return level if level in spec.get("thinking_levels", ()) else None


# ---------------------------------------------------------------------------
# Capability discovery + manifest (Phase 2).
# ---------------------------------------------------------------------------

DEFAULT_MANIFEST_FILENAME = "google_capability_manifest.json"
DEFAULT_MANIFEST_TTL_HOURS = 24.0


def _field(obj, name, default=None):
    """Dict-first field lookup for SDK objects and their test doubles.

    Dict FIRST is not cosmetic: a plain dict already has `.values`/`.items`/`.keys`
    attributes, so `getattr(response_dict, 'values')` would silently return a
    bound method instead of the embedding vector.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _normalize_model_name(raw) -> str:
    """`models.list()` returns names like 'models/gemini-3.6-flash'."""
    name = _field(raw, "name") or str(raw)
    return str(name).split("/")[-1].strip()


def list_available_google_models(client=None, *, client_factory=None, credential=None) -> dict:
    """CANONICAL COMMAND: what does `client.models.list()` expose to this project?

    Returns {'ok', 'models': [ids], 'error', 'error_kind'}. Discovery alone does
    NOT prove a model has nonzero free quota -- that is why the caller must
    intersect with the allowlist (`build_google_capability_manifest`).

    Consumes no generation quota: `models.list()` is a metadata call.
    """
    try:
        resolved = _resolve_client(client, client_factory, credential)
    except GoogleCredentialMissing as exc:
        return {"ok": False, "models": [], "error": str(exc), "error_kind": "credential_missing"}
    try:
        discovered = sorted({_normalize_model_name(m) for m in resolved.models.list()})
        return {"ok": True, "models": discovered, "error": None, "error_kind": None}
    except Exception as exc:
        return {
            "ok": False, "models": [],
            "error": sanitize_google_error(exc, credential),
            "error_kind": classify_google_error(exc),
        }


def build_google_capability_manifest(client=None, *, client_factory=None, credential=None,
                                      discovered_models=None, run_connection_test: bool = False,
                                      connection_test_tasks=("script_generation", "bulk_classification"),
                                      now=None) -> dict:
    """CANONICAL COMMAND: builds the sanitized capability manifest.

    Discovery -> intersect with `MODEL_ALLOWLIST` -> drop zero-quota models ->
    compute the per-task routes that are actually usable.

    `run_connection_test=True` performs a MINIMAL smoke call against ONLY the
    primary model of each task in `connection_test_tasks` -- never the whole
    catalog, so a capability check cannot burn the day's RPD. The test never
    prints or records the key.
    """
    now = now or datetime.now(timezone.utc)
    if discovered_models is None:
        discovery = list_available_google_models(client, client_factory=client_factory,
                                                  credential=credential)
    else:
        discovery = {"ok": True,
                     "models": sorted({_normalize_model_name(m) for m in discovered_models}),
                     "error": None, "error_kind": None}

    discovered = list(discovery["models"])
    allowlisted = [m for m in discovered if m in MODEL_REGISTRY]
    excluded_not_allowlisted = [m for m in discovered if m not in MODEL_REGISTRY]
    excluded_zero_quota = [m for m in allowlisted if not has_nonzero_quota(m)]
    usable = [m for m in allowlisted if has_nonzero_quota(m)]

    # If discovery failed we do NOT invent availability: the manifest records the
    # failure and falls back to the registry's own eligible set so routing can
    # still be shown/planned offline, flagged as unverified.
    discovery_verified = bool(discovery["ok"])
    if not discovery_verified:
        usable = [m for m in MODEL_ALLOWLIST if has_nonzero_quota(m)]

    models_block = {}
    for model_id in sorted(set(allowlisted) | set(usable)):
        spec = MODEL_REGISTRY[model_id]
        models_block[model_id] = {
            "supported_actions": list(spec["actions"]),
            "configured_rpm": spec["configured_rpm"],
            "configured_tpm": spec["configured_tpm"],
            "configured_rpd": spec["configured_rpd"],
            "quota_source": spec["quota_source"],
            "thinking_levels": list(spec["thinking_levels"]),
            "family": spec["family"],
            "experimental": spec["experimental"],
            "automatic_route_eligible": is_automatic_route_eligible(model_id),
            "discovered": model_id in discovered,
            "notes": spec["notes"],
        }

    routes = {}
    for task, route in TASK_ROUTES.items():
        required = TASK_REQUIRED_ACTION[task]
        selected, skipped = [], []
        for model_id in route:
            reason = _route_exclusion_reason(model_id, required, usable, discovery_verified)
            if reason is None:
                selected.append(model_id)
            else:
                skipped.append({"model": model_id, "reason": reason})
        routes[task] = {
            "route_requested": list(route),
            "route_usable": selected,
            "skipped": skipped,
            "required_action": required,
            "status": "ok" if selected else "no_usable_model",
        }

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "provider": "google",
        "discovered_at": now.isoformat(),
        "ttl_hours": DEFAULT_MANIFEST_TTL_HOURS,
        "discovery_verified": discovery_verified,
        "discovery_error": discovery["error"],
        "discovery_error_kind": discovery["error_kind"],
        "credential": (credential.to_public_dict() if isinstance(credential, CredentialRef)
                       else {"kind": "google_api", "state": "unknown"}),
        "allowlist": list(MODEL_ALLOWLIST),
        "discovered_models": discovered,
        "excluded_not_allowlisted": excluded_not_allowlisted,
        "excluded_zero_quota": excluded_zero_quota,
        "usable_models": sorted(usable),
        "models": models_block,
        "task_routes": routes,
        "connection_test": None,
        "account_quota_rotation": "disabled",
        "free_tier_data_notice": FREE_TIER_DATA_NOTICE,
        "status": "experimental",
        "status_reason": (
            "yetenek manifesti yerel allowlist + yapilandirilmis kota "
            "varsayimlarina dayanir; canli AI Studio limitleri baglayicidir."
        ),
    }

    if run_connection_test:
        manifest["connection_test"] = run_google_connection_test(
            client, client_factory=client_factory, credential=credential,
            manifest=manifest, tasks=connection_test_tasks,
        )
    return manifest


def _route_exclusion_reason(model_id, required_action, usable, discovery_verified) -> str | None:
    if model_id not in MODEL_REGISTRY:
        return "allowlist_disi"
    spec = MODEL_REGISTRY[model_id]
    if required_action not in spec["actions"]:
        return f"yetenek yok ({required_action})"
    if not has_nonzero_quota(model_id):
        return "yapilandirilmis kota 0 (otomatik rotaya alinmaz)"
    if model_id not in usable:
        return "models.list() ciktisinda yok" if discovery_verified else "kesif dogrulanamadi"
    return None


def manifest_is_fresh(manifest: dict, now=None, ttl_hours: float | None = None) -> bool:
    if not manifest or not manifest.get("discovered_at"):
        return False
    ttl = ttl_hours if ttl_hours is not None else manifest.get("ttl_hours", DEFAULT_MANIFEST_TTL_HOURS)
    try:
        discovered = datetime.fromisoformat(manifest["discovered_at"])
    except Exception:
        return False
    if discovered.tzinfo is None:
        discovered = discovered.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - discovered) <= timedelta(hours=float(ttl))


def load_or_build_capability_manifest(results_dir, client=None, *, client_factory=None,
                                       credential=None, force_refresh: bool = False,
                                       run_connection_test: bool = False, now=None,
                                       ttl_hours: float | None = None,
                                       credential_profile: str | None = None) -> dict:
    """Cached manifest: reuses `results/google_capability_manifest.json` while it
    is within TTL, so a rerun of a notebook cell does not re-list the catalog."""
    profile = validate_credential_profile(
        credential_profile or getattr(credential, "profile", DEFAULT_CREDENTIAL_PROFILE)
    )
    path = Path(results_dir) / profile_scoped_filename(DEFAULT_MANIFEST_FILENAME, profile)
    if not force_refresh and path.is_file():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            cached_profile = ((cached.get("credential") or {}).get("profile")
                              or DEFAULT_CREDENTIAL_PROFILE)
            if (cached_profile == profile and
                    manifest_is_fresh(cached, now=now, ttl_hours=ttl_hours)):
                cached["cache"] = "hit"
                return cached
        except Exception:
            pass  # corrupt cache -> rebuild
    manifest = build_google_capability_manifest(
        client, client_factory=client_factory, credential=credential,
        run_connection_test=run_connection_test, now=now,
    )
    manifest["cache"] = "miss"
    write_json_atomic(path, manifest)
    return manifest


def run_google_connection_test(client=None, *, client_factory=None, credential=None,
                                manifest=None, tasks=("script_generation",)) -> dict:
    """Minimal, opt-in smoke test against ONLY the primary model of each task.

    Default is OFF (`RUN_GOOGLE_CONNECTION_TEST = False` in the notebook). Never
    prints the key; every error is sanitized.
    """
    result = {
        "ran": True, "tested_models": [], "ok": True, "results": [],
        "note": "yalnizca secili birincil modeller test edildi; tum katalog denenmedi.",
    }
    try:
        resolved = _resolve_client(client, client_factory, credential)
    except GoogleCredentialMissing as exc:
        return {"ran": False, "tested_models": [], "ok": False,
                "results": [], "error": str(exc), "error_kind": "credential_missing"}

    for task in tasks:
        route = ((manifest or {}).get("task_routes", {}).get(task) or {}).get("route_usable")
        if route is None:
            route = [m for m in TASK_ROUTES.get(task, ()) if is_automatic_route_eligible(m)]
        if not route:
            result["results"].append({"task": task, "model": None, "ok": False,
                                       "error": "kullanilabilir model yok"})
            result["ok"] = False
            continue
        model_id = route[0]
        result["tested_models"].append(model_id)
        try:
            response = resolved.models.generate_content(
                model=model_id, contents="ping",
                config={"max_output_tokens": 8},
            )
            result["results"].append({
                "task": task, "model": model_id, "ok": True,
                "response_nonempty": bool(_response_text(response)), "error": None,
            })
        except Exception as exc:
            result["ok"] = False
            result["results"].append({
                "task": task, "model": model_id, "ok": False,
                "error": sanitize_google_error(exc, credential),
                "error_kind": classify_google_error(exc),
            })
    return result


# ---------------------------------------------------------------------------
# Error classification + sanitization (Phase 7).
# ---------------------------------------------------------------------------

ERROR_RATE_LIMIT_RPM = "rate_limit_rpm"
ERROR_RATE_LIMIT_RPD = "rate_limit_rpd"
ERROR_BILLING_QUOTA = "billing_or_quota_configuration"
ERROR_AUTH = "auth"
ERROR_NOT_FOUND = "model_not_found"
ERROR_SERVER = "server_error"
ERROR_TIMEOUT = "timeout"
ERROR_SAFETY = "safety_refusal"
ERROR_INVALID_OUTPUT = "invalid_structured_output"
ERROR_CREDENTIAL_MISSING = "credential_missing"
ERROR_UNKNOWN = "unknown"

#: Errors that count toward the circuit breaker's consecutive-failure counter.
TRANSIENT_ERROR_KINDS = (ERROR_SERVER, ERROR_TIMEOUT)

#: Errors after which the router moves to the next model.
FALLBACK_ERROR_KINDS = (
    ERROR_RATE_LIMIT_RPM, ERROR_RATE_LIMIT_RPD, ERROR_NOT_FOUND,
    ERROR_SERVER, ERROR_TIMEOUT, ERROR_INVALID_OUTPUT,
)

_SECRET_LIKE_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z_\-]{10,}"),
    re.compile(r"(?i)\b(api[_\-]?key|authorization|bearer|x-goog-api-key)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)([?&](?:key|api_?key|access_token)=)[^&\s\"']+"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9_\-]{10,}"),
)


class StructuredOutputError(ValueError):
    """Raised when a response is not valid JSON, does not match the schema, or is
    syntactically valid but SEMANTICALLY wrong (empty required field, out-of-range
    score, ...). Triggers exactly ONE corrective retry on the same model."""


class SafetyRefusalError(RuntimeError):
    """The model refused on safety grounds. NEVER triggers provider rotation."""


def classify_google_error(exc) -> str:
    """Maps an exception (or an error string) to a deterministic error kind.

    Works on the message/status/code surface, so it classifies both real SDK
    errors and test doubles without importing the SDK.
    """
    if isinstance(exc, SafetyRefusalError):
        return ERROR_SAFETY
    if isinstance(exc, StructuredOutputError):
        return ERROR_INVALID_OUTPUT
    if isinstance(exc, GoogleCredentialMissing):
        return ERROR_CREDENTIAL_MISSING

    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    text = " ".join(
        str(part) for part in (
            getattr(exc, "message", None), getattr(exc, "status", None), str(exc),
        ) if part
    )
    lowered = text.lower()

    if code in (401, 403) or "permission_denied" in lowered or "unauthenticated" in lowered \
            or "api key not valid" in lowered or "api_key_invalid" in lowered:
        return ERROR_AUTH
    if code == 404 or "not_found" in lowered or "was not found" in lowered \
            or "is not supported" in lowered:
        return ERROR_NOT_FOUND
    if code == 429 or "resource_exhausted" in lowered or "429" in lowered \
            or "quota" in lowered or "rate limit" in lowered:
        if _looks_like_billing_quota(lowered):
            return ERROR_BILLING_QUOTA
        if _looks_like_daily_quota(lowered):
            return ERROR_RATE_LIMIT_RPD
        return ERROR_RATE_LIMIT_RPM
    if code in (500, 502, 503, 504) or "internal error" in lowered \
            or "unavailable" in lowered or "overloaded" in lowered or "503" in lowered:
        return ERROR_SERVER
    if "timeout" in lowered or "timed out" in lowered or "deadline_exceeded" in lowered:
        return ERROR_TIMEOUT
    if "safety" in lowered or "blocked" in lowered or "prohibited_content" in lowered \
            or "recitation" in lowered:
        return ERROR_SAFETY
    return ERROR_UNKNOWN


def _looks_like_daily_quota(lowered: str) -> bool:
    return any(marker in lowered for marker in (
        "per day", "perday", "requests_per_day", "generaterequestsperday",
        "daily limit", "gunluk", "rpd",
    ))


def _looks_like_billing_quota(lowered: str) -> bool:
    """A quota/configuration block has no trustworthy automatic reset time.

    These provider messages mean that waiting a minute (or until midnight) is
    not a remedy. Treating them as RPM caused the live pilot to retry the same
    model and then spend calls on every fallback model.
    """
    return any(marker in lowered for marker in (
        "plan and billing", "billing details", "billing required",
        "enable billing", "paid tier required", "quota value: 0",
        "quota limit value: 0", '"quota_value":"0"',
        '"quota_limit_value":"0"',
    ))


def parse_retry_after_seconds(exc, default=None):
    """Honors a server-provided Retry-After / retryDelay when present.

    Looks at (in order) an HTTP `Retry-After` header, a `retry_delay`/`retryDelay`
    attribute or dict entry, and finally a "retryDelay": "31s" fragment in the
    message. Returns None when the server said nothing.
    """
    headers = getattr(getattr(exc, "response", None), "headers", None) or getattr(exc, "headers", None)
    if isinstance(headers, dict):
        for key in ("Retry-After", "retry-after", "RETRY-AFTER"):
            if key in headers:
                try:
                    return max(0.0, float(headers[key]))
                except (TypeError, ValueError):
                    pass
    for attr in ("retry_delay", "retryDelay"):
        raw = getattr(exc, attr, None)
        if raw is None and isinstance(getattr(exc, "details", None), dict):
            raw = exc.details.get(attr)
        if raw is not None:
            seconds = _coerce_seconds(raw)
            if seconds is not None:
                return seconds
    match = re.search(r"retry_?delay\"?\s*[:=]\s*\"?(\d+(?:\.\d+)?)s?", str(exc), re.I)
    if match:
        return float(match.group(1))
    return default


def _coerce_seconds(raw):
    if isinstance(raw, (int, float)):
        return max(0.0, float(raw))
    if isinstance(raw, str):
        match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*s?\s*$", raw)
        if match:
            return float(match.group(1))
    seconds = getattr(raw, "seconds", None)
    if seconds is not None:
        try:
            return max(0.0, float(seconds))
        except (TypeError, ValueError):
            return None
    return None


def sanitize_google_error(exc, credential=None, max_len: int = 500) -> str:
    """Turns any exception/text into a short, credential-free string.

    Redacts (a) the exact configured secret value if one was supplied, and
    (b) anything that merely LOOKS like a key/authorization header/credentialed
    URL. Raw exception objects are never stored anywhere; only this output is.
    """
    text = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    if isinstance(credential, CredentialRef) and credential.configured:
        text = text.replace(credential.value, "***REDACTED***")
    elif isinstance(credential, str) and credential:
        text = text.replace(credential, "***REDACTED***")
    for pattern in _SECRET_LIKE_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) + "***REDACTED***") if m.groups() else "***REDACTED***",
                           text)
    text = " ".join(text.split())
    return text[:max_len]


# ---------------------------------------------------------------------------
# Pacific-time day boundary (documented RPD reset) + quota ledger (Phase 6).
# ---------------------------------------------------------------------------

def _pacific_offset_hours(moment_utc: datetime) -> int:
    """US Pacific UTC offset: -7 during DST, -8 otherwise.

    Uses zoneinfo when tz data is available, else the statutory rule (DST from
    the 2nd Sunday in March 10:00 UTC to the 1st Sunday in November 09:00 UTC).
    Implemented explicitly so the daily-reset test is deterministic on a Windows
    box without `tzdata` installed.
    """
    try:
        from zoneinfo import ZoneInfo
        offset = moment_utc.astimezone(ZoneInfo("America/Los_Angeles")).utcoffset()
        if offset is not None:
            return int(offset.total_seconds() // 3600)
    except Exception:
        pass
    year = moment_utc.year
    march = date(year, 3, 1)
    second_sunday = march + timedelta(days=(6 - march.weekday()) % 7 + 7)
    november = date(year, 11, 1)
    first_sunday = november + timedelta(days=(6 - november.weekday()) % 7)
    dst_start = datetime(second_sunday.year, second_sunday.month, second_sunday.day,
                         10, 0, tzinfo=timezone.utc)
    dst_end = datetime(first_sunday.year, first_sunday.month, first_sunday.day,
                       9, 0, tzinfo=timezone.utc)
    return -7 if dst_start <= moment_utc < dst_end else -8


def pacific_day_key(moment_utc: datetime | None = None) -> str:
    """The 'project day' an RPD counter belongs to, per the documented
    midnight-Pacific reset."""
    moment_utc = (moment_utc or datetime.now(timezone.utc))
    if moment_utc.tzinfo is None:
        moment_utc = moment_utc.replace(tzinfo=timezone.utc)
    moment_utc = moment_utc.astimezone(timezone.utc)
    local = moment_utc + timedelta(hours=_pacific_offset_hours(moment_utc))
    return local.date().isoformat()


def next_pacific_reset(moment_utc: datetime | None = None) -> datetime:
    """UTC instant of the next midnight-Pacific RPD reset."""
    moment_utc = (moment_utc or datetime.now(timezone.utc))
    if moment_utc.tzinfo is None:
        moment_utc = moment_utc.replace(tzinfo=timezone.utc)
    offset = _pacific_offset_hours(moment_utc)
    local = moment_utc + timedelta(hours=offset)
    next_local_midnight = datetime.combine(local.date() + timedelta(days=1),
                                           datetime.min.time(), tzinfo=timezone.utc)
    return next_local_midnight - timedelta(hours=offset)


def write_json_atomic(path, data) -> None:
    """Atomic write: temp file in the same directory + os.replace, so a crashed
    or concurrent run can never leave a half-written ledger/manifest."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class _FileLock:
    """Minimal cross-process lock via O_CREAT|O_EXCL, with a stale-lock timeout.

    Good enough for the ledger's single-writer-per-notebook usage and works
    identically on Windows and Linux without extra dependencies.
    """

    def __init__(self, path, timeout=10.0, poll=0.05, stale_after=60.0):
        self.path = Path(str(path) + ".lock")
        self.timeout, self.poll, self.stale_after = timeout, poll, stale_after
        self._fd = None

    def __enter__(self):
        deadline = time.time() + self.timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale_after:
                        self.path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.time() >= deadline:
                    # Never block the pipeline forever on a lock; proceed
                    # unlocked (the write itself is still atomic).
                    return self
                time.sleep(self.poll)

    def __exit__(self, *exc_info):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self.path.unlink(missing_ok=True)
        return False


DEFAULT_LEDGER_FILENAME = "google_quota_usage.json"

#: Circuit breaker defaults (Phase 7).
CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_COOLDOWN_SEC = 15 * 60


class QuotaLedger:
    """Persistent, ESTIMATED per-model quota accounting.

    This is a LOCAL ESTIMATE, never a claim about authoritative server quota --
    every record is labelled `estimated: True`. It exists so that (a) rerunning a
    notebook cell does not forget what was already spent, and (b) the router can
    refuse to make a call that the local estimate already proves is impossible.

    Daily counters roll over at midnight Pacific, matching the documented reset.
    """

    def __init__(self, path=None, now_fn=None,
                 credential_profile: str = DEFAULT_CREDENTIAL_PROFILE):
        self.path = Path(path) if path else None
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self.credential_profile = validate_credential_profile(credential_profile)
        self.data = self._empty()
        if self.path and self.path.is_file():
            self.load()

    # -- persistence -------------------------------------------------------
    def _empty(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "estimated": True,
            "disclaimer": (
                "YEREL TAHMIN. Sunucunun yetkili kota durumu degildir; canli "
                "429 yanitlari ve AI Studio limitleri baglayicidir."
            ),
            "daily_reset_timezone": "America/Los_Angeles (midnight Pacific)",
            "account_quota_rotation": "disabled",
            "credential_profile": self.credential_profile,
            "updated_at": None,
            "models": {},
            "manual_resets": [],
        }

    def load(self) -> "QuotaLedger":
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            loaded_profile = (loaded.get("credential_profile", DEFAULT_CREDENTIAL_PROFILE)
                              if isinstance(loaded, dict) else None)
            if (isinstance(loaded, dict) and "models" in loaded and
                    loaded_profile == self.credential_profile):
                self.data = loaded
                self.data.setdefault("manual_resets", [])
        except Exception:
            self.data = self._empty()  # corrupt ledger -> start clean, never crash
        return self

    def save(self) -> None:
        if not self.path:
            return
        self.data["updated_at"] = self._now().isoformat()
        with _FileLock(self.path):
            write_json_atomic(self.path, self.data)

    # -- per-model records -------------------------------------------------
    def record_for(self, model_id: str) -> dict:
        spec = MODEL_REGISTRY.get(model_id, {})
        record = self.data["models"].get(model_id)
        if record is None:
            record = {
                "estimated": True,
                "requests_current_minute": 0,
                "minute_window_start": None,
                "estimated_input_tokens_current_minute": 0,
                "requests_current_day": 0,
                "day_key": None,
                "successful_calls": 0,
                "failed_calls": 0,
                "last_call_at": None,
                "last_429_at": None,
                "cooldown_until": None,
                "daily_exhausted_until": None,
                "consecutive_transient_failures": 0,
                "circuit_state": "closed",
                "last_server_usage_metadata": None,
                "disabled_reason": None,
            }
            self.data["models"][model_id] = record
        record["configured_rpm"] = spec.get("configured_rpm")
        record["configured_tpm"] = spec.get("configured_tpm")
        record["configured_rpd"] = spec.get("configured_rpd")
        record["quota_source"] = spec.get("quota_source", "project_configured_default")
        self._roll_windows(record)
        record["estimated_remaining_rpd"] = max(
            0, (record["configured_rpd"] or 0) - record["requests_current_day"]
        )
        return record

    def _roll_windows(self, record) -> None:
        now = self._now()
        day = pacific_day_key(now)
        if record.get("day_key") != day:
            record["day_key"] = day
            record["requests_current_day"] = 0
            record["daily_exhausted_until"] = None
        start = record.get("minute_window_start")
        if start:
            try:
                started = datetime.fromisoformat(start)
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                if (now - started).total_seconds() >= 60:
                    record["minute_window_start"] = now.isoformat()
                    record["requests_current_minute"] = 0
                    record["estimated_input_tokens_current_minute"] = 0
            except Exception:
                record["minute_window_start"] = now.isoformat()
                record["requests_current_minute"] = 0
                record["estimated_input_tokens_current_minute"] = 0
        else:
            record["minute_window_start"] = now.isoformat()

    # -- pre-flight --------------------------------------------------------
    def blocking_reason(self, model_id: str, estimated_input_tokens: int = 0) -> str | None:
        """Why a call MUST NOT be attempted right now, or None.

        Only returns a reason when the local estimate PROVES the call cannot
        succeed (day exhausted, cooldown/circuit open, minute budget spent).
        """
        record = self.record_for(model_id)
        now = self._now()
        for field, label in (("daily_exhausted_until", "gunluk kota tukendi"),
                             ("cooldown_until", "devre kesici acik")):
            until = record.get(field)
            if until:
                try:
                    moment = datetime.fromisoformat(until)
                    if moment.tzinfo is None:
                        moment = moment.replace(tzinfo=timezone.utc)
                    if now < moment:
                        return f"{label} (bitis: {until})"
                except Exception:
                    pass
        if record.get("disabled_reason"):
            return record["disabled_reason"]
        rpd = record.get("configured_rpd") or 0
        if rpd and record["requests_current_day"] >= rpd:
            return f"yerel tahmin: gunluk limit dolu ({record['requests_current_day']}/{rpd} RPD)"
        rpm = record.get("configured_rpm") or 0
        if rpm and record["requests_current_minute"] >= rpm:
            return f"yerel tahmin: dakika limiti dolu ({record['requests_current_minute']}/{rpm} RPM)"
        tpm = record.get("configured_tpm") or 0
        if tpm and estimated_input_tokens and \
                record["estimated_input_tokens_current_minute"] + estimated_input_tokens > tpm:
            return f"yerel tahmin: dakika token limiti dolu (~{tpm} TPM)"
        return None

    def snapshot(self, model_id: str) -> dict:
        record = self.record_for(model_id)
        return {
            "model": model_id,
            "estimated": True,
            "requests_current_minute": record["requests_current_minute"],
            "estimated_input_tokens_current_minute": record["estimated_input_tokens_current_minute"],
            "requests_current_day": record["requests_current_day"],
            "estimated_remaining_rpd": record["estimated_remaining_rpd"],
            "configured_rpm": record["configured_rpm"],
            "configured_tpm": record["configured_tpm"],
            "configured_rpd": record["configured_rpd"],
            "circuit_state": record["circuit_state"],
            "cooldown_until": record["cooldown_until"],
            "day_key": record["day_key"],
        }

    # -- post-flight -------------------------------------------------------
    def record_usage(self, model_id: str, *, ok: bool, error_kind: str | None = None,
                     estimated_input_tokens: int = 0, usage_metadata=None,
                     retry_after_sec=None, breaker_threshold=CIRCUIT_BREAKER_THRESHOLD,
                     breaker_cooldown_sec=CIRCUIT_BREAKER_COOLDOWN_SEC) -> dict:
        record = self.record_for(model_id)
        now = self._now()
        record["requests_current_minute"] += 1
        record["estimated_input_tokens_current_minute"] += max(0, int(estimated_input_tokens or 0))
        record["requests_current_day"] += 1
        record["last_call_at"] = now.isoformat()
        record["estimated_remaining_rpd"] = max(
            0, (record["configured_rpd"] or 0) - record["requests_current_day"]
        )
        if usage_metadata:
            record["last_server_usage_metadata"] = normalize_usage_metadata(usage_metadata)

        if ok:
            record["successful_calls"] += 1
            record["consecutive_transient_failures"] = 0
            if record["circuit_state"] == "half_open":
                record["circuit_state"] = "closed"
            record["cooldown_until"] = None
            return record

        record["failed_calls"] += 1
        if error_kind in (ERROR_RATE_LIMIT_RPM, ERROR_RATE_LIMIT_RPD):
            record["last_429_at"] = now.isoformat()
        if error_kind == ERROR_RATE_LIMIT_RPD:
            # Do NOT sleep for hours: mark unavailable until the documented reset.
            record["daily_exhausted_until"] = next_pacific_reset(now).isoformat()
        if error_kind in TRANSIENT_ERROR_KINDS:
            record["consecutive_transient_failures"] += 1
            if record["consecutive_transient_failures"] >= breaker_threshold:
                record["circuit_state"] = "open"
                record["cooldown_until"] = (now + timedelta(seconds=breaker_cooldown_sec)).isoformat()
        elif error_kind == ERROR_NOT_FOUND:
            record["disabled_reason"] = "model bulunamadi (404); bu calistirmada kullanilmaz"
        return record

    def note_circuit_half_open(self, model_id: str) -> None:
        record = self.record_for(model_id)
        record["circuit_state"] = "half_open"
        record["cooldown_until"] = None

    def manual_reset(self, model_id: str | None, reason: str, reset_by: str = "") -> dict:
        """Manual reset -- ONLY with an explicit recorded reason.

        This is an accounting correction for the LOCAL estimate. It does not, and
        cannot, reset server-side quota, and it must never be used as a
        credential-swap workaround.
        """
        if not reason or not reason.strip():
            raise ValueError(
                "manuel kota sifirlama icin gerekce zorunludur (manifest'e kaydedilir)."
            )
        entry = {
            "model": model_id or "*",
            "reason": reason.strip(),
            "reset_by": reset_by or None,
            "reset_at": self._now().isoformat(),
            "note": "yalnizca YEREL tahmini sifirlar; sunucu kotasini degistirmez.",
        }
        targets = [model_id] if model_id else list(self.data["models"])
        for target in targets:
            self.data["models"].pop(target, None)
        self.data.setdefault("manual_resets", []).append(entry)
        return entry


def record_google_usage(ledger: QuotaLedger, model_id: str, *, ok: bool, error_kind=None,
                        estimated_input_tokens: int = 0, usage_metadata=None,
                        persist: bool = True) -> dict:
    """CANONICAL COMMAND: books one attempt against the estimated quota ledger."""
    record = ledger.record_usage(
        model_id, ok=ok, error_kind=error_kind,
        estimated_input_tokens=estimated_input_tokens, usage_metadata=usage_metadata,
    )
    if persist:
        ledger.save()
    return record


def normalize_usage_metadata(usage) -> dict | None:
    """Server-reported token usage, as a plain dict (never the SDK object)."""
    if usage is None:
        return None
    if isinstance(usage, dict):
        source = usage
        get = source.get
    else:
        get = lambda key, default=None: getattr(usage, key, default)  # noqa: E731
    fields = ("prompt_token_count", "candidates_token_count", "total_token_count",
              "thoughts_token_count", "cached_content_token_count")
    normalized = {field: get(field) for field in fields}
    return {k: v for k, v in normalized.items() if v is not None} or None


def estimate_input_tokens(text) -> int:
    """Crude ~4-chars-per-token estimate, used ONLY for the local TPM guard."""
    if text is None:
        return 0
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Route selection + execution (Phases 3 & 7).
# ---------------------------------------------------------------------------

def select_google_route(task: str, *, mode: str = "auto", overrides=None, manifest=None,
                        ledger=None, enable_model_fallback: bool = True,
                        max_model_attempts: int = DEFAULT_MAX_GOOGLE_MODEL_ATTEMPTS,
                        estimated_input_tokens: int = 0) -> dict:
    """CANONICAL COMMAND: the deterministic, auditable model order for one task.

    mode='auto'      -> the registry route, filtered by manifest + ledger.
    mode='explicit'  -> ONLY `GOOGLE_TASK_MODEL_OVERRIDES[task]`; nothing is
                        silently substituted, so an explicit choice stays honest.

    With `enable_model_fallback=False` the route is truncated to ONE model, so a
    single request can make at most one Google call.
    """
    if task not in TASK_ROUTES:
        raise ValueError(f"bilinmeyen Google gorevi: {task!r} (beklenen: {sorted(TASK_ROUTES)})")
    if mode not in ("auto", "explicit"):
        raise ValueError(f"gecersiz GOOGLE_ROUTE_MODE: {mode!r} (beklenen: 'auto' | 'explicit')")

    overrides = overrides or {}
    override = overrides.get(task)
    if isinstance(override, str):
        override = [override]

    if mode == "explicit":
        requested = list(override or ())
        if not requested:
            return {
                "task": task, "mode": mode, "route_requested": [], "route": [],
                "skipped": [], "status": "no_explicit_override",
                "status_reason": (
                    f"GOOGLE_ROUTE_MODE='explicit' ama GOOGLE_TASK_MODEL_OVERRIDES['{task}'] "
                    "tanimli degil; otomatik model secimi yapilmaz."
                ),
            }
    else:
        requested = list(override or TASK_ROUTES[task])

    required_action = TASK_REQUIRED_ACTION[task]
    manifest_routes = ((manifest or {}).get("task_routes", {}) or {}).get(task) or {}
    usable = manifest_routes.get("route_usable")
    manifest_usable_models = set((manifest or {}).get("usable_models") or [])

    route, skipped = [], []
    for model_id in requested:
        if model_id not in MODEL_REGISTRY:
            skipped.append({"model": model_id, "reason": "allowlist_disi"})
            continue
        if required_action not in MODEL_REGISTRY[model_id]["actions"]:
            skipped.append({"model": model_id, "reason": f"yetenek yok ({required_action})"})
            continue
        if not has_nonzero_quota(model_id):
            skipped.append({"model": model_id, "reason": "yapilandirilmis kota 0"})
            continue
        if manifest_usable_models and model_id not in manifest_usable_models and \
                (usable is None or model_id not in usable):
            skipped.append({"model": model_id, "reason": "manifestte kullanilabilir degil"})
            continue
        if ledger is not None:
            reason = ledger.blocking_reason(model_id, estimated_input_tokens)
            if reason:
                skipped.append({"model": model_id, "reason": reason})
                continue
        route.append(model_id)

    if not enable_model_fallback:
        route = route[:1]
    route = route[: max(1, int(max_model_attempts))]

    return {
        "task": task,
        "mode": mode,
        "required_action": required_action,
        "route_requested": requested,
        "route": route,
        "skipped": skipped,
        "status": "ok" if route else "no_usable_model",
        "status_reason": "" if route else (
            "hicbir model kullanilabilir degil (kota/yetenek/manifest kisitlari); "
            "hicbir cagri yapilmadi."
        ),
    }


def format_route_line(selection: dict, credential=None, *, raw_media: bool = False) -> str:
    """The sanitized pre-execution banner the master notebook prints.

    Contains model IDs and policy state only -- never a secret.
    """
    route = " -> ".join(selection.get("route") or []) or "(kullanilabilir model yok)"
    credential_state = (describe_credential_state(credential)
                        if isinstance(credential, CredentialRef) else "unknown")
    return (
        f"Task: {selection['task']}\n"
        f"Route: {route}\n"
        f"Credential: {credential_state}\n"
        f"Account rotation: disabled\n"
        f"Raw media upload: {'enabled' if raw_media else 'disabled'}"
    )


def _response_text(response) -> str:
    """Extracts text from a GenerateContentResponse (or a test double)."""
    text = _field(response, "text")
    if text:
        return text
    chunks = []
    for candidate in _field(response, "candidates") or []:
        for part in _field(_field(candidate, "content"), "parts") or []:
            piece = _field(part, "text")
            if piece:
                chunks.append(piece)
    return "".join(chunks)


def _response_usage(response):
    return _field(response, "usage_metadata")


def _detect_safety_refusal(response) -> str | None:
    """Returns a refusal reason when the model blocked the request."""
    blocked = _field(_field(response, "prompt_feedback"), "block_reason")
    if blocked:
        return f"prompt_feedback.block_reason={blocked}"
    for candidate in _field(response, "candidates") or []:
        reason_text = str(_field(candidate, "finish_reason") or "").upper()
        if reason_text and any(marker in reason_text for marker in
                               ("SAFETY", "PROHIBITED", "BLOCKLIST", "RECITATION")):
            return f"finish_reason={reason_text}"
    return None


def _build_generate_config(task: str, model_id: str, *, response_schema=None,
                            system_instruction=None, max_output_tokens=None,
                            grounding: bool = False, extra=None) -> dict:
    """Builds the `config` dict for `models.generate_content`.

    Deliberately NEVER sets temperature / top_p / top_k / candidate_count. Per
    official Gemini 3 guidance temperature should stay at its default 1.0, and
    the other three add nothing here -- omitting them is the safest way to honor
    that guidance across model families (Gemma included).
    """
    spec = MODEL_REGISTRY.get(model_id) or {}
    config: dict = {}
    if max_output_tokens:
        config["max_output_tokens"] = int(max_output_tokens)
    if system_instruction and spec.get("family") != "gemma":
        # Gemma on the Gemini API has no separate system role; the instruction is
        # folded into the prompt by the caller instead.
        config["system_instruction"] = system_instruction
    level = thinking_level_for(task, model_id)
    if level:
        config["thinking_config"] = {"thinking_level": level}
    if response_schema is not None and ACTION_STRUCTURED in spec.get("actions", ()):
        config["response_mime_type"] = "application/json"
        config["response_schema"] = response_schema
    if grounding:
        if ACTION_GROUNDED not in spec.get("actions", ()):
            raise ValueError(f"{model_id} Google Search grounding desteklemiyor.")
        config["tools"] = [{"google_search": {}}]
    if extra:
        config.update(extra)
    return config


def _sleep_with_backoff(attempt_index, retry_after, sleep_fn, base=1.0, cap=8.0, rng=None):
    """Retry-After wins when the server provided one; otherwise exponential
    backoff with jitter, bounded by `cap`."""
    if retry_after is not None:
        delay = min(float(retry_after), 60.0)
    else:
        rng = rng or random
        delay = min(cap, base * (2 ** attempt_index)) * (0.5 + rng.random() / 2)
    if delay > 0:
        sleep_fn(delay)
    return delay


def execute_google_route(task: str, call_fn, *, client=None, client_factory=None, credential=None,
                         selection=None, mode: str = "auto", overrides=None, manifest=None,
                         ledger=None, enable_model_fallback: bool = True,
                         max_model_attempts: int = DEFAULT_MAX_GOOGLE_MODEL_ATTEMPTS,
                         estimated_input_tokens: int = 0, validate_fn=None,
                         max_transient_retries: int = 2, sleep_fn=None, rng=None,
                         breaker_threshold: int = CIRCUIT_BREAKER_THRESHOLD,
                         breaker_cooldown_sec: int = CIRCUIT_BREAKER_COOLDOWN_SEC,
                         persist_ledger: bool = True) -> dict:
    """CANONICAL COMMAND: runs one task through its route with the full error policy.

    `call_fn(client, model_id, spec) -> raw_response` performs the actual SDK
    call; `validate_fn(raw_response, model_id) -> parsed` may raise
    `StructuredOutputError` for a syntactically OR semantically invalid result.
    Both are injected, which is what makes every branch testable without a
    network.

    Contract:
      - Attempts models STRICTLY SEQUENTIALLY and RETURNS on the first success.
        A later model is contacted only after the previous one genuinely failed,
        so the normal path never fans out across the catalog.
      - RPM 429  -> honor Retry-After / bounded jittered backoff, then next model.
      - RPD 429  -> no long sleep; model marked unavailable until midnight
                    Pacific, then next model.
      - billing/quota configuration 429 -> stop immediately. It has no known
                    reset time, so there is no retry and no model fallback.
      - 401/403  -> the credential is disabled for the whole run; stops.
      - 404      -> model marked unavailable, continue to the next allowlisted one.
      - 5xx      -> bounded retry, circuit breaker after N consecutive transients.
      - invalid structured output -> exactly ONE corrective retry on the SAME
        model, then fallback.
      - safety refusal -> STOPS. Never rotates model/provider to bypass safety.
      - everything exhausted -> status 'failed', output=None. Never fabricates.
    """
    assert_no_account_quota_rotation()
    sleep_fn = sleep_fn or time.sleep
    ledger = ledger if ledger is not None else QuotaLedger()
    started = time.time()

    if selection is None:
        selection = select_google_route(
            task, mode=mode, overrides=overrides, manifest=manifest, ledger=ledger,
            enable_model_fallback=enable_model_fallback, max_model_attempts=max_model_attempts,
            estimated_input_tokens=estimated_input_tokens,
        )

    result = {
        "task": task,
        "provider": "google",
        "model": None,
        "route_requested": list(selection.get("route_requested") or []),
        "route_planned": list(selection.get("route") or []),
        "route_skipped": list(selection.get("skipped") or []),
        "provider_attempts": [],
        "selected_attempt": None,
        "fallback_used": False,
        "fallback_reason": None,
        "output": None,
        "raw_response": None,
        "usage_metadata": None,
        "estimated_quota_before": None,
        "estimated_quota_after": None,
        "latency_sec": 0.0,
        "status": "failed",
        "status_reason": selection.get("status_reason") or "",
        "error_kind": None,
    }

    route = list(selection.get("route") or [])
    if not route:
        result["status"] = "insufficient_data"
        result["status_reason"] = selection.get("status_reason") or (
            "kullanilabilir Google modeli yok; hicbir cagri yapilmadi."
        )
        result["latency_sec"] = round(time.time() - started, 3)
        return result

    try:
        resolved_client = _resolve_client(client, client_factory, credential)
    except GoogleCredentialMissing as exc:
        result["status"] = "insufficient_data"
        result["error_kind"] = ERROR_CREDENTIAL_MISSING
        result["status_reason"] = sanitize_google_error(exc, credential)
        result["latency_sec"] = round(time.time() - started, 3)
        return result

    for position, model_id in enumerate(route):
        spec = MODEL_REGISTRY[model_id]
        blocking = ledger.blocking_reason(model_id, estimated_input_tokens)
        if blocking:
            result["provider_attempts"].append({
                "provider": "google", "model": model_id, "ok": False,
                "error_kind": "locally_blocked", "error": blocking, "calls_made": 0,
            })
            continue

        record = ledger.record_for(model_id)
        if record.get("circuit_state") == "open":
            ledger.note_circuit_half_open(model_id)  # one probe request

        if result["estimated_quota_before"] is None:
            result["estimated_quota_before"] = ledger.snapshot(model_id)

        transient_retries = 0
        corrective_retries = 0
        calls_made = 0
        attempt_error = None
        attempt_error_kind = None

        while True:
            try:
                calls_made += 1
                raw = call_fn(resolved_client, model_id, spec)
                refusal = _detect_safety_refusal(raw)
                if refusal:
                    raise SafetyRefusalError(refusal)
                parsed = validate_fn(raw, model_id) if validate_fn else _response_text(raw)
            except Exception as exc:
                kind = classify_google_error(exc)
                message = sanitize_google_error(exc, credential)
                record_google_usage(
                    ledger, model_id, ok=False, error_kind=kind,
                    estimated_input_tokens=estimated_input_tokens, persist=persist_ledger,
                )

                if kind == ERROR_SAFETY:
                    result["provider_attempts"].append({
                        "provider": "google", "model": model_id, "ok": False,
                        "error_kind": kind, "error": message, "calls_made": calls_made,
                    })
                    result["model"] = model_id
                    result["status"] = "review_required"
                    result["error_kind"] = kind
                    result["status_reason"] = (
                        "GUVENLIK REDDI: model istegi reddetti. Guvenlik filtresini asmak icin "
                        f"model/saglayici DEGISTIRILMEDI. Neden: {message}"
                    )
                    result["estimated_quota_after"] = ledger.snapshot(model_id)
                    result["latency_sec"] = round(time.time() - started, 3)
                    return result

                if kind in (ERROR_AUTH, ERROR_CREDENTIAL_MISSING):
                    result["provider_attempts"].append({
                        "provider": "google", "model": model_id, "ok": False,
                        "error_kind": kind, "error": message, "calls_made": calls_made,
                    })
                    result["status"] = "failed"
                    result["error_kind"] = kind
                    result["status_reason"] = (
                        "Google kimlik dogrulama hatasi (401/403): bu calistirmada Google "
                        "saglayicisi devre disi birakildi. "
                        f"{credential_help_text('google_api')} Ayrinti: {message}"
                    )
                    result["provider_disabled"] = "google"
                    result["estimated_quota_after"] = ledger.snapshot(model_id)
                    result["latency_sec"] = round(time.time() - started, 3)
                    return result

                if kind == ERROR_BILLING_QUOTA:
                    result["provider_attempts"].append({
                        "provider": "google", "model": model_id, "ok": False,
                        "error_kind": kind, "error": message,
                        "calls_made": calls_made,
                        "corrective_retries": corrective_retries,
                        "transient_retries": transient_retries,
                    })
                    result["model"] = model_id
                    result["status"] = "failed"
                    result["error_kind"] = kind
                    result["status_reason"] = (
                        "Google AI Studio plan/billing veya kota yapilandirmasi "
                        "istegi engelliyor. Faturalandirma ve proje kotasini "
                        "duzeltip operator yeniden denemesini acin; otomatik "
                        "tekrar ve model gecisi yapilmadi. "
                        f"Ayrinti: {message}"
                    )
                    result["provider_disabled"] = "google"
                    result["estimated_quota_after"] = ledger.snapshot(model_id)
                    result["latency_sec"] = round(time.time() - started, 3)
                    return result

                if kind in (ERROR_SERVER, ERROR_TIMEOUT) and transient_retries < max_transient_retries:
                    transient_retries += 1
                    _sleep_with_backoff(transient_retries, parse_retry_after_seconds(exc),
                                        sleep_fn, rng=rng)
                    continue

                if kind == ERROR_RATE_LIMIT_RPM and transient_retries < max_transient_retries:
                    transient_retries += 1
                    _sleep_with_backoff(transient_retries, parse_retry_after_seconds(exc),
                                        sleep_fn, rng=rng)
                    continue

                if kind == ERROR_INVALID_OUTPUT and corrective_retries < 1:
                    corrective_retries += 1
                    continue  # exactly ONE corrective retry on the same model

                attempt_error, attempt_error_kind = message, kind
                break

            # success
            usage = normalize_usage_metadata(_response_usage(raw))
            record_google_usage(
                ledger, model_id, ok=True, estimated_input_tokens=estimated_input_tokens,
                usage_metadata=usage, persist=persist_ledger,
            )
            result["provider_attempts"].append({
                "provider": "google", "model": model_id, "ok": True,
                "error_kind": None, "error": None, "calls_made": calls_made,
                "corrective_retries": corrective_retries,
                "transient_retries": transient_retries,
            })
            result["model"] = model_id
            result["output"] = parsed
            result["raw_response"] = raw
            result["usage_metadata"] = usage
            result["selected_attempt"] = len(result["provider_attempts"]) - 1
            result["fallback_used"] = position > 0
            if position > 0:
                previous = [a for a in result["provider_attempts"][:-1] if not a["ok"]]
                result["fallback_reason"] = "; ".join(
                    f"{a['model']}: {a.get('error_kind')}" for a in previous
                ) or "onceki modeller kullanilamadi"
            result["status"] = "ok"
            result["status_reason"] = ""
            result["estimated_quota_after"] = ledger.snapshot(model_id)
            result["latency_sec"] = round(time.time() - started, 3)
            return result

        result["provider_attempts"].append({
            "provider": "google", "model": model_id, "ok": False,
            "error_kind": attempt_error_kind, "error": attempt_error,
            "calls_made": calls_made, "corrective_retries": corrective_retries,
            "transient_retries": transient_retries,
        })
        result["estimated_quota_after"] = ledger.snapshot(model_id)
        result["error_kind"] = attempt_error_kind

    result["status"] = "failed"
    result["status_reason"] = (
        f"tum Google modelleri basarisiz ({len(result['provider_attempts'])} deneme); "
        "cikti UYDURULMADI."
    )
    result["latency_sec"] = round(time.time() - started, 3)
    return result


# ---------------------------------------------------------------------------
# Structured-output validation: syntax first, then SEMANTICS.
# ---------------------------------------------------------------------------

def extract_json_object(raw_text: str):
    """Pulls the first JSON object/array out of a response (models sometimes wrap
    it in prose or a ```json fence). Raises StructuredOutputError, never returns
    a guess."""
    if not raw_text or not raw_text.strip():
        raise StructuredOutputError("bos yanit")
    text = raw_text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    if start == -1:
        raise StructuredOutputError("yanitta JSON blogu yok")
    end = max(text.rfind("}"), text.rfind("]")) + 1
    if end <= start:
        raise StructuredOutputError("JSON blogu kapanmamis")
    try:
        return json.loads(text[start:end])
    except Exception as exc:
        raise StructuredOutputError(f"JSON ayristirilamadi: {exc}") from None


def validate_semantics(parsed, required_fields=(), checks=()):
    """Semantic validation AFTER schema/syntax validation.

    Syntactically valid JSON is NOT semantically valid: `{"script": ""}` parses
    fine and is still useless. `checks` is a sequence of
    (name, predicate(parsed) -> bool) pairs.
    """
    if not isinstance(parsed, dict):
        raise StructuredOutputError(f"beklenen JSON nesnesi, gelen: {type(parsed).__name__}")
    for field in required_fields:
        if field not in parsed:
            raise StructuredOutputError(f"zorunlu alan eksik: {field!r}")
        value = parsed[field]
        if value is None or (isinstance(value, str) and not value.strip()):
            raise StructuredOutputError(f"zorunlu alan bos: {field!r}")
    for name, predicate in checks:
        try:
            ok = bool(predicate(parsed))
        except Exception as exc:
            raise StructuredOutputError(f"anlamsal kontrol '{name}' hata verdi: {exc}") from None
        if not ok:
            raise StructuredOutputError(f"anlamsal kontrol basarisiz: {name}")
    return parsed


def _json_validator(required_fields=(), checks=()):
    def validate(raw_response, model_id):
        return validate_semantics(extract_json_object(_response_text(raw_response)),
                                   required_fields, checks)
    return validate


# ---------------------------------------------------------------------------
# Artifact contract (Phase 10).
# ---------------------------------------------------------------------------

def sha256_text(text) -> str:
    if text is None:
        return hashlib.sha256(b"").hexdigest()
    if not isinstance(text, (str, bytes)):
        text = json.dumps(text, ensure_ascii=False, sort_keys=True)
    if isinstance(text, str):
        text = text.encode("utf-8")
    return hashlib.sha256(text).hexdigest()


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_google_artifact(route_result: dict, *, task: str, capability: str,
                          input_content: str | None = None, output_content=None,
                          output_sha256: str | None = None, status: str | None = None,
                          status_reason: str | None = None, raw_media_submitted: bool = False,
                          include_human_approval: bool = True, extra: dict | None = None) -> dict:
    """CANONICAL artifact contract for every Google-generated result.

    Carries the full provider/route/quota/hash/data-policy record and NEVER a
    key, an authorization header, a credentialed URL, or a raw exception object
    (only sanitized error strings that already went through
    `sanitize_google_error`).

    This function cannot emit 'approved' or 'production_ready'; promotion happens
    only through `local_pipeline.approval.approve_artifact()`.
    """
    resolved_status = status or ("review_required" if route_result.get("status") == "ok"
                                 else route_result.get("status") or "failed")
    if resolved_status in ("approved", "production_ready"):
        raise ValueError(
            "build_google_artifact 'approved'/'production_ready' uretemez; onay ayri "
            "hash-tabanli adimda (approval.approve_artifact) verilir."
        )
    artifact = {
        "provider": route_result.get("provider") or "google",
        "model": route_result.get("model"),
        "endpoint": capability,
        "capability": capability,
        "task": task,
        "route_requested": list(route_result.get("route_requested") or []),
        "route_planned": list(route_result.get("route_planned") or []),
        "route_skipped": list(route_result.get("route_skipped") or []),
        "provider_attempts": list(route_result.get("provider_attempts") or []),
        "selected_attempt": route_result.get("selected_attempt"),
        "fallback_used": bool(route_result.get("fallback_used")),
        "fallback_reason": route_result.get("fallback_reason"),
        "status": resolved_status,
        "status_reason": status_reason or route_result.get("status_reason") or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "latency_sec": route_result.get("latency_sec", 0.0),
        "usage_metadata": route_result.get("usage_metadata"),
        "estimated_quota_before": route_result.get("estimated_quota_before"),
        "estimated_quota_after": route_result.get("estimated_quota_after"),
        "input_content_sha256": sha256_text(input_content) if input_content is not None else None,
        "output_content_sha256": output_sha256 if output_sha256 is not None else (
            sha256_text(output_content) if output_content is not None else None),
        "raw_media_submitted": bool(raw_media_submitted),
        "free_tier_data_notice": FREE_TIER_DATA_NOTICE,
        "account_quota_rotation": "disabled",
        "estimated_quota_note": "kota degerleri YEREL TAHMINDIR, sunucu yetkilisi degildir.",
    }
    if include_human_approval:
        artifact["human_approval"] = {
            "approved": False, "approved_by": None, "approved_at": None,
            "notes": None, "approved_content_sha256": None,
        }
    if extra:
        artifact.update(extra)
    return artifact


# ---------------------------------------------------------------------------
# Per-capability canonical commands (Phase 1, item 10).
#
# Every Google capability gets its OWN named entry point. They all share the
# same router, so there is exactly one place where retries, quota and fallback
# are implemented.
# ---------------------------------------------------------------------------

def _text_task(task, prompt, *, system_instruction=None, response_schema=None,
               required_fields=(), checks=(), max_output_tokens=None, grounding=False,
               **router_kwargs):
    tokens = estimate_input_tokens(prompt) + estimate_input_tokens(system_instruction)
    if max_output_tokens is None:
        max_output_tokens = max_output_tokens_for(task)

    def call(client, model_id, spec):
        contents = prompt
        if system_instruction and spec.get("family") == "gemma":
            contents = f"{system_instruction}\n\n{prompt}"
        config = _build_generate_config(
            task, model_id, response_schema=response_schema,
            system_instruction=system_instruction, max_output_tokens=max_output_tokens,
            grounding=grounding,
        )
        return client.models.generate_content(model=model_id, contents=contents, config=config)

    validate = (_json_validator(required_fields, checks)
                if (response_schema is not None or required_fields or checks)
                else (lambda raw, model_id: _response_text(raw)))
    router_kwargs.setdefault("estimated_input_tokens", tokens)
    return execute_google_route(task, call, validate_fn=validate, **router_kwargs)


def run_google_hotmoment_review(prompt: str, **kwargs) -> dict:
    """CANONICAL COMMAND: final hot-moment review (Route A, thinking_level=medium).

    Receives locally derived transcript windows / scene + audio scores, never the
    raw video (see ALLOW_RAW_MEDIA_TO_FREE_TIER).
    """
    kwargs.setdefault("required_fields", ("moments",))
    kwargs.setdefault("checks", (
        ("moments_nonempty", lambda p: isinstance(p.get("moments"), list) and len(p["moments"]) > 0),
    ))
    return _text_task("hotmoment_review", prompt, **kwargs)


def run_google_script_generation(prompt: str, **kwargs) -> dict:
    """CANONICAL COMMAND: final script generation (Route A)."""
    kwargs.setdefault("required_fields", ("script",))
    kwargs.setdefault("checks", (
        ("script_min_length", lambda p: len(str(p.get("script", "")).strip()) >= 40),
    ))
    return _text_task("script_generation", prompt, **kwargs)


def run_google_bulk_classification(prompt: str, **kwargs) -> dict:
    """CANONICAL COMMAND: high-volume structured work (Route B, thinking_level=minimal):
    trend row classification, title/description/hashtag generation, transcript
    cleanup, metadata normalization, structured JSON conversion."""
    kwargs.setdefault("required_fields", ())
    return _text_task("bulk_classification", prompt, **kwargs)


def run_google_viral_interpretation(prompt: str, **kwargs) -> dict:
    """CANONICAL COMMAND: NONCRITICAL interpretation of the statistical viral
    result (Route B). It never replaces the statistical baseline."""
    return _text_task("viral_interpretation", prompt, **kwargs)


def run_google_grounded_research(prompt: str, *, max_candidates: int = 5, **kwargs) -> dict:
    """CANONICAL COMMAND: Google Search grounded research (Route C).

    Uses the allowlisted Gemini grounded route. Intended for a SMALL number of top
    candidates only -- it may confirm whether a topic is currently in the news,
    but it is NOT a substitute for YouTube metrics or Google Trends time series.
    Source URLs/citations are extracted and kept in the artifact.
    """
    kwargs.setdefault("grounding", True)
    result = _text_task("grounded_research", prompt, **kwargs)
    result["grounding_sources"] = extract_grounding_sources(result.get("raw_response"))
    result["grounding_queries"] = extract_grounding_queries(result.get("raw_response"))
    result["max_candidates"] = max_candidates
    result["research_caveat"] = (
        "Grounded arama SADECE 'bu konu su an haberlerde mi' sorusunu destekler; "
        "YouTube metrikleri veya Google Trends zaman serisinin yerine GECMEZ."
    )
    return result


def extract_grounding_sources(raw_response) -> list:
    """Pulls source URLs/titles out of `candidates[].grounding_metadata`.

    Field names follow the google-genai `GroundingMetadata` /
    `GroundingChunkWeb` types (`grounding_chunks[].web.uri/title/domain`).
    Returns [] when the response carried no grounding metadata.
    """
    if raw_response is None:
        return []
    sources, seen = [], set()
    for candidate in _field(raw_response, "candidates") or []:
        metadata = _field(candidate, "grounding_metadata")
        if not metadata:
            continue
        for chunk in _field(metadata, "grounding_chunks") or []:
            web = _field(chunk, "web")
            uri = _field(web, "uri")
            if not uri or uri in seen:
                continue
            seen.add(uri)
            sources.append({
                "uri": uri,
                "title": _field(web, "title"),
                "domain": _field(web, "domain"),
            })
    return sources


def extract_grounding_queries(raw_response) -> list:
    """The `web_search_queries` the model actually issued, kept in the artifact
    so a grounded claim can be traced back to what was searched."""
    queries: list[str] = []
    for candidate in _field(raw_response, "candidates") or []:
        for query in _field(_field(candidate, "grounding_metadata"), "web_search_queries") or []:
            if query not in queries:
                queries.append(query)
    return queries


# --- TTS (Route D): a SEPARATE audio implementation, not the text parser -----

GEMINI_TTS_VOICES = (
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
    "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
    "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
    "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
    "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
)
DEFAULT_TTS_VOICE = "Kore"
DEFAULT_TTS_LANGUAGE = "tr-TR"
#: Documented Gemini TTS output: 24 kHz, 16-bit, mono raw PCM (no container).
TTS_SAMPLE_RATE = 24_000
TTS_SAMPLE_WIDTH = 2
TTS_CHANNELS = 1
#: Documented TTS context window.
TTS_CONTEXT_TOKEN_LIMIT = 32_000


def _extract_audio_bytes(response) -> tuple[bytes, str | None]:
    for candidate in _field(response, "candidates") or []:
        for part in _field(_field(candidate, "content"), "parts") or []:
            inline = _field(part, "inline_data")
            if not inline:
                continue
            data = _field(inline, "data")
            mime = _field(inline, "mime_type")
            if data:
                if isinstance(data, str):
                    data = base64.b64decode(data)
                return bytes(data), mime
    raise StructuredOutputError("TTS yanitinda inline audio verisi yok")


def pcm_to_wav_bytes(pcm: bytes, sample_rate=TTS_SAMPLE_RATE, channels=TTS_CHANNELS,
                     sample_width=TTS_SAMPLE_WIDTH) -> bytes:
    """Wraps raw PCM in a RIFF/WAVE container.

    Gemini TTS returns headerless PCM, so a `.wav` file only becomes playable
    after this step. Implemented with struct (not `wave`) so it works on a
    bytes buffer with no temp file.
    """
    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + \
        struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate,
                    block_align, sample_width * 8) + \
        b"data" + struct.pack("<I", len(pcm))
    return header + pcm


def _riff_chunk_size(data: bytes, chunk_id: bytes) -> int:
    """Size of a named RIFF chunk, found by WALKING the chunk list.

    WHY NOT A FIXED OFFSET
    ----------------------
    A canonical 44-byte header puts `data` at offset 36, and reading its size
    from bytes 40-44 works. But that is only one legal layout: encoders are
    free to insert `LIST`/`INFO` chunks before `data`, and ffmpeg does. Reading
    the fixed offset then measures the WRONG chunk and reports a duration
    hundreds of times too small -- silently, because the file is otherwise
    valid and plays fine. Walking the list is the only correct reading.
    """
    offset = 12
    while offset + 8 <= len(data):
        current = data[offset:offset + 4]
        size = struct.unpack("<I", data[offset + 4:offset + 8])[0]
        if current == chunk_id:
            # Clamp to what is actually on disk: a truncated file must report
            # the bytes it has, not the size its header claims.
            return min(size, max(0, len(data) - (offset + 8)))
        # Chunks are word-aligned, so an odd size carries one pad byte.
        offset += 8 + size + (size & 1)
    return 0


def validate_audio_file(path, *, min_bytes=1024, expected_sample_rate=TTS_SAMPLE_RATE) -> dict:
    """Container/codec/size/duration/non-emptiness check for a produced WAV.

    Parses the RIFF header itself, so it needs no ffprobe and works in the local
    test suite.
    """
    path = Path(path)
    checks = {"exists": path.is_file(), "nonempty": False, "container_wav": False,
              "codec_pcm_s16le": False, "sample_rate_ok": False, "size_ok": False,
              "duration_positive": False}
    details = {"path": str(path), "size_bytes": 0, "duration_sec": None,
               "sample_rate": None, "channels": None, "bits_per_sample": None}
    if not checks["exists"]:
        return {"passed": False, "checks": checks, "details": details,
                "reason": "ses dosyasi olusmadi"}
    data = path.read_bytes()
    details["size_bytes"] = len(data)
    checks["nonempty"] = len(data) > 44
    checks["size_ok"] = len(data) >= min_bytes
    if len(data) >= 44 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        checks["container_wav"] = True
        audio_format, channels, sample_rate, _, _, bits = struct.unpack("<HHIIHH", data[20:36])
        data_size = _riff_chunk_size(data, b"data")
        details.update({"sample_rate": sample_rate, "channels": channels,
                        "bits_per_sample": bits})
        checks["codec_pcm_s16le"] = (audio_format == 1 and bits == 16)
        checks["sample_rate_ok"] = (sample_rate == expected_sample_rate)
        if sample_rate and channels and bits:
            duration = data_size / float(sample_rate * channels * (bits // 8))
            details["duration_sec"] = round(duration, 3)
            checks["duration_positive"] = duration > 0
    passed = all(checks.values())
    return {"passed": passed, "checks": checks, "details": details,
            "reason": "" if passed else "ses dogrulamasi basarisiz: "
                      + ", ".join(k for k, v in checks.items() if not v)}


def split_script_for_tts(script_text: str, max_chars: int = 4000) -> list:
    """Splits ONLY when required. A script that fits stays a single chunk, so the
    common case makes exactly one TTS call."""
    text = (script_text or "").strip()
    if len(text) <= max_chars:
        return [text] if text else []
    chunks, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(current) + len(sentence) + 1 > max_chars and current:
            chunks.append(current.strip())
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current.strip():
        chunks.append(current.strip())
    return chunks



def run_google_tts_chunk(chunk_text: str, out_path, *, voice: str = DEFAULT_TTS_VOICE,
                         language_code: str = DEFAULT_TTS_LANGUAGE,
                         narration_direction: str = "", **router_kwargs) -> dict:
    """CANONICAL COMMAND: speak ONE chunk in ONE routed call.

    The difference from `run_google_tts` is the whole point of this function.
    `run_google_tts` loops over every chunk INSIDE one `execute_google_route`
    call, so a failure on the twelfth chunk re-speaks the first eleven and the
    quota ledger records one routed job instead of twelve real requests. For an
    eighteen minute narration that is both expensive and untrue.

    Here the route wraps a single request: retries retry only this chunk, and
    the ledger counts one entry per provider call, which is what actually
    happened.

    `run_google_tts` is deliberately left as it is -- the Shorts pipeline calls
    it, its behaviour is covered by existing tests, and a script that fits in
    one request never had this problem.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    body = (chunk_text or "").strip()
    if not body:
        return {"task": "tts", "provider": "google", "status": "insufficient_data",
                "status_reason": "bos parca; TTS cagrisi yapilmadi",
                "provider_attempts": [], "route_requested": list(TASK_ROUTES["tts"]),
                "route_planned": [], "output": None, "latency_sec": 0.0,
                "audio_path": None, "audio_validation": None}

    if voice not in GEMINI_TTS_VOICES:
        raise ValueError(f"bilinmeyen Gemini TTS sesi: {voice!r} (bkz. GEMINI_TTS_VOICES)")

    prefix = (narration_direction.strip() + "\n\n") if (narration_direction or "").strip() else ""
    captured: list[bytes] = []

    def call(client, model_id, spec):
        captured.clear()
        response = client.models.generate_content(
            model=model_id, contents=prefix + body,
            config={
                "response_modalities": ["AUDIO"],
                "speech_config": {
                    "language_code": language_code,
                    "voice_config": {"prebuilt_voice_config": {"voice_name": voice}},
                },
            },
        )
        audio_bytes, _mime = _extract_audio_bytes(response)
        if not audio_bytes:
            raise StructuredOutputError("TTS bos ses verisi dondurdu")
        captured.append(audio_bytes)
        return response

    def validate(raw_response, model_id):
        if not captured or not captured[0]:
            raise StructuredOutputError("TTS ses verisi bos")
        return captured[0]

    router_kwargs.setdefault("estimated_input_tokens", estimate_input_tokens(body))
    result = execute_google_route("tts", call, validate_fn=validate, **router_kwargs)

    if result["status"] != "ok":
        result["audio_path"] = None
        result["audio_validation"] = None
        return result

    # Atomic: a half-written WAV that survives a crash has a size, a hash and a
    # path, so every later check would treat it as a real recording.
    wav_bytes = pcm_to_wav_bytes(result["output"])
    temp_path = out_path.with_suffix(out_path.suffix + ".part")
    temp_path.write_bytes(wav_bytes)
    os.replace(temp_path, out_path)

    validation = validate_audio_file(out_path)
    result["audio_path"] = str(out_path)
    result["audio_validation"] = validation
    result["voice"] = voice
    result["language_code"] = language_code
    result["narration_direction"] = narration_direction or None
    result["source_text_sha256"] = sha256_text(body)
    result["output_audio_sha256"] = sha256_file(out_path)
    # `validate_audio_file` nests measurements under "details"; reading the
    # top level silently produced None for every chunk duration.
    result["duration_sec"] = (validation.get("details") or {}).get("duration_sec")
    result["preview_model"] = True
    result["approval_policy"] = (
        "Gemini TTS modelleri PREVIEW; parca ciktisi her zaman review_required."
    )
    if not validation["passed"]:
        result["status"] = "failed"
        result["status_reason"] = validation["reason"]
        # A file that failed validation must not be left where a resume would
        # mistake it for a finished chunk.
        out_path.unlink(missing_ok=True)
        result["audio_path"] = None
    return result

def run_google_tts(script_text: str, out_path, *, voice: str = DEFAULT_TTS_VOICE,
                   language_code: str = DEFAULT_TTS_LANGUAGE,
                   narration_direction: str = "", max_chars_per_chunk: int = 4000,
                   **router_kwargs) -> dict:
    """CANONICAL COMMAND: Gemini TTS (Route D) with its own audio implementation.

    Turkish by default (`language_code='tr-TR'`), configurable voice and
    narration direction. Raw PCM is wrapped into a valid WAV file and then
    validated (container, codec, sample rate, size, duration, non-emptiness).

    APPROVAL: every Gemini TTS output is `review_required` because both TTS
    models are PREVIEW. Replacing or modifying the audio invalidates approval
    (`approval.check_approval` re-derives the file hash), and a fallback
    voice/model can never inherit the previous voiceover's approval -- the
    artifact records a fresh, unapproved `human_approval` block plus the new
    audio hash.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not (script_text or "").strip():
        return {"task": "tts", "provider": "google", "status": "insufficient_data",
                "status_reason": "bos script; TTS cagrisi yapilmadi",
                "provider_attempts": [], "route_requested": list(TASK_ROUTES["tts"]),
                "route_planned": [], "output": None, "latency_sec": 0.0}

    chunks = split_script_for_tts(script_text, max_chars_per_chunk)
    prompt_prefix = (narration_direction.strip() + "\n\n") if narration_direction.strip() else ""
    tokens = estimate_input_tokens(script_text)

    if voice not in GEMINI_TTS_VOICES:
        raise ValueError(f"bilinmeyen Gemini TTS sesi: {voice!r} (bkz. GEMINI_TTS_VOICES)")

    collected: list[bytes] = []

    def call(client, model_id, spec):
        collected.clear()
        for chunk in chunks:
            response = client.models.generate_content(
                model=model_id, contents=prompt_prefix + chunk,
                config={
                    "response_modalities": ["AUDIO"],
                    "speech_config": {
                        "language_code": language_code,
                        "voice_config": {"prebuilt_voice_config": {"voice_name": voice}},
                    },
                },
            )
            audio_bytes, _mime = _extract_audio_bytes(response)
            if not audio_bytes:
                raise StructuredOutputError("TTS bos ses verisi dondurdu")
            collected.append(audio_bytes)
        return response  # last response carries usage metadata

    def validate(raw_response, model_id):
        if not collected or not b"".join(collected):
            raise StructuredOutputError("TTS ses verisi bos")
        return b"".join(collected)

    router_kwargs.setdefault("estimated_input_tokens", tokens)
    result = execute_google_route("tts", call, validate_fn=validate, **router_kwargs)

    if result["status"] != "ok":
        result["audio_path"] = None
        result["audio_validation"] = None
        return result

    wav_bytes = pcm_to_wav_bytes(result["output"])
    out_path.write_bytes(wav_bytes)
    validation = validate_audio_file(out_path)
    result["audio_path"] = str(out_path)
    result["audio_validation"] = validation
    result["voice"] = voice
    result["language_code"] = language_code
    result["narration_direction"] = narration_direction or None
    result["chunks"] = len(chunks)
    result["source_script_sha256"] = sha256_text(script_text)
    result["output_audio_sha256"] = sha256_file(out_path)
    result["preview_model"] = True
    result["approval_policy"] = (
        "Gemini TTS modelleri PREVIEW; cikti her zaman review_required. Ses "
        "degistirilirse/yeniden uretilirse onceki onay otomatik gecersizdir ve "
        "yedek ses/model onceki onayi DEVRALMAZ."
    )
    if not validation["passed"]:
        result["status"] = "failed"
        result["status_reason"] = validation["reason"]
    return result


def build_tts_artifact(tts_result: dict, script_text: str, *, script_status: str = "review_required",
                       raw_media_submitted=False) -> dict:
    """selected_voiceover-compatible artifact for a Gemini TTS run.

    Always `review_required` (preview model) or `failed`/`insufficient_data`, and
    always with a FRESH unapproved `human_approval` block, so a fallback
    voice/model cannot inherit a previous approval.
    """
    ok = tts_result.get("status") == "ok"
    artifact = build_google_artifact(
        tts_result, task="tts", capability="generate_content(response_modalities=['AUDIO'])",
        input_content=script_text,
        output_sha256=tts_result.get("output_audio_sha256"),
        status="review_required" if ok else (tts_result.get("status") or "failed"),
        status_reason=(
            "Gemini TTS PREVIEW modeli; seslendirme uretildi ve INSAN INCELEMESI BEKLIYOR. "
            "Onay ayri adimda (approval.approve_artifact) verilir."
        ) if ok else tts_result.get("status_reason"),
        raw_media_submitted=raw_media_submitted,
    )
    artifact.update({
        "audio_path": tts_result.get("audio_path"),
        "voice_id": tts_result.get("voice"),
        "language": (tts_result.get("language_code") or DEFAULT_TTS_LANGUAGE).split("-")[0],
        "language_code": tts_result.get("language_code"),
        "narration_direction": tts_result.get("narration_direction"),
        "source_script_sha256": tts_result.get("source_script_sha256"),
        # 16-char truncated digest, matching tts.build_selected_voiceover_artifact,
        # so render_utils' 'voiceover_hash_matches_script' gate works unchanged
        # for a Gemini voiceover.
        "source_script_hash": (tts_result.get("source_script_sha256") or "")[:16] or None,
        "source_script_status": script_status,
        "used_dev_fallback_script": False,
        "duration_sec": ((tts_result.get("audio_validation") or {}).get("details") or {}).get("duration_sec"),
        "cost_usd_estimate": 0.0,
        "cost_usd_actual": None,
        "quality_score_1_5": None,
        "audio_validation": tts_result.get("audio_validation"),
        "chunks": tts_result.get("chunks"),
        "preview_model": True,
        "approval_policy": tts_result.get("approval_policy"),
        "inherits_previous_approval": False,
    })
    return artifact


# --- Embeddings (Route E) ----------------------------------------------------

DEFAULT_EMBEDDING_DIMENSION = 768
DEFAULT_EMBEDDING_MODEL = "gemini-embedding-2"

#: gemini-embedding-2 has no `task_type`; the task goes in the prompt text.
EMBEDDING_TASK_PREFIXES = {
    "semantic_similarity": "task: semantic similarity | text: ",
    "clustering": "task: clustering | text: ",
    "retrieval_document": "task: search result | document: ",
    "retrieval_query": "task: search result | query: ",
}


def run_google_embeddings(texts, *, task_prefix_kind: str = "semantic_similarity",
                          dimension: int = DEFAULT_EMBEDDING_DIMENSION,
                          store_raw_input: bool = False, **router_kwargs) -> dict:
    """CANONICAL COMMAND: embeddings via the EMBED-CONTENT endpoint (not generate-content).

    768 dimensions by default (a documented recommended size). `gemini-embedding-2`
    auto-normalizes truncated dimensions, so no manual renormalization is applied;
    the artifact records model, dimension, task prefix and per-input SHA-256.
    Raw input text is stored only when `store_raw_input=True`.
    """
    items = [texts] if isinstance(texts, str) else list(texts)
    if not items:
        return {"task": "embeddings", "provider": "google", "status": "insufficient_data",
                "status_reason": "gomulecek metin yok", "provider_attempts": [],
                "route_requested": list(TASK_ROUTES["embeddings"]), "route_planned": [],
                "output": None, "latency_sec": 0.0}
    prefix = EMBEDDING_TASK_PREFIXES.get(task_prefix_kind, "")
    prepared = [prefix + str(item) for item in items]

    def call(client, model_id, spec):
        return client.models.embed_content(
            model=model_id, contents=prepared,
            config={"output_dimensionality": int(dimension)},
        )

    def validate(raw_response, model_id):
        embeddings = _field(raw_response, "embeddings")
        if not embeddings:
            raise StructuredOutputError("embed_content bos yanit dondurdu")
        vectors = []
        for entry in embeddings:
            values = _field(entry, "values")
            if not values:
                raise StructuredOutputError("embedding vektoru bos")
            if len(values) != int(dimension):
                raise StructuredOutputError(
                    f"beklenen boyut {dimension}, gelen {len(values)}"
                )
            vectors.append([float(v) for v in values])
        if len(vectors) != len(prepared):
            raise StructuredOutputError(
                f"{len(prepared)} girdi icin {len(vectors)} vektor dondu"
            )
        return vectors

    router_kwargs.setdefault("estimated_input_tokens",
                             sum(estimate_input_tokens(t) for t in prepared))
    result = execute_google_route("embeddings", call, validate_fn=validate, **router_kwargs)
    result["dimension"] = int(dimension)
    result["task_prefix_kind"] = task_prefix_kind
    result["task_prefix"] = prefix
    result["input_hashes"] = [sha256_text(item) for item in items]
    result["input_count"] = len(items)
    if store_raw_input:
        result["inputs"] = items
    return result


def cosine_similarity(a, b) -> float:
    if not a or not b or len(a) != len(b):
        raise ValueError("cosine_similarity ayni uzunlukta bos olmayan vektorler bekler")
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def novelty_score(candidate_vector, previous_vectors) -> float:
    """1 - max cosine similarity against previously published/generated content.

    1.0 = nothing similar seen before, 0.0 = an exact duplicate. Deterministic
    given the same vectors.
    """
    if not previous_vectors:
        return 1.0
    best = max(cosine_similarity(candidate_vector, other) for other in previous_vectors)
    return round(max(0.0, min(1.0, 1.0 - best)), 6)


def find_semantic_duplicates(candidate_vector, previous, threshold: float = 0.92) -> list:
    """Duplicate detection across past scripts/titles.

    `previous` is a sequence of (label, vector). Returns the matches at or above
    `threshold`, most similar first.
    """
    matches = []
    for label, vector in previous:
        score = cosine_similarity(candidate_vector, vector)
        if score >= threshold:
            matches.append({"label": label, "similarity": round(score, 6)})
    return sorted(matches, key=lambda m: -m["similarity"])


def cluster_by_similarity(labelled_vectors, threshold: float = 0.85) -> list:
    """Greedy, deterministic title/topic clustering (single-link, threshold-based)."""
    clusters: list[dict] = []
    for label, vector in labelled_vectors:
        placed = False
        for cluster in clusters:
            if cosine_similarity(vector, cluster["centroid"]) >= threshold:
                cluster["members"].append(label)
                size = len(cluster["members"])
                cluster["centroid"] = [
                    (c * (size - 1) + v) / size for c, v in zip(cluster["centroid"], vector)
                ]
                placed = True
                break
        if not placed:
            clusters.append({"members": [label], "centroid": list(vector)})
    return [{"members": c["members"]} for c in clusters]


def duplicate_penalty(novelty: float, max_penalty: float = 0.35) -> float:
    """Penalty applied to a viral-prediction score for near-duplicate content."""
    novelty = max(0.0, min(1.0, float(novelty)))
    return round((1.0 - novelty) * max_penalty, 6)


# --- Antigravity (Route F): experimental, OFF by default --------------------

def run_antigravity_research_experimental(prompt: str, *, enabled: bool = False,
                                           max_total_tokens: int | None = None,
                                           **router_kwargs) -> dict:
    """CANONICAL COMMAND: experimental Antigravity research report.

    Disabled by default (`RUN_ANTIGRAVITY_RESEARCH = False`). It must NOT run in
    every pipeline execution, must never publish, must never change approval
    status, must never emit a `production_ready` artifact, and is NOT treated as
    structured-output capable. An explicit `max_total_tokens` budget is
    MANDATORY so it cannot silently consume the day's tokens.

    Its output is always `experimental` + `review_required`.
    """
    if not enabled:
        return {
            "task": "antigravity_research", "provider": "google", "model": None,
            "status": "experimental",
            "status_reason": ("RUN_ANTIGRAVITY_RESEARCH=False (varsayilan): deneysel arastirma "
                              "calistirilmadi, hicbir cagri yapilmadi."),
            "provider_attempts": [], "route_requested": list(TASK_ROUTES["antigravity_research"]),
            "route_planned": [], "output": None, "latency_sec": 0.0,
            "max_total_tokens": max_total_tokens,
        }
    if not max_total_tokens or int(max_total_tokens) <= 0:
        raise ValueError(
            "run_antigravity_research_experimental icin acik bir max_total_tokens butcesi "
            "zorunludur (sessizce buyuk token tuketimi yasak)."
        )
    budget = int(max_total_tokens)
    if estimate_input_tokens(prompt) >= budget:
        return {
            "task": "antigravity_research", "provider": "google", "model": None,
            "status": "failed",
            "status_reason": (f"prompt tahmini token sayisi max_total_tokens={budget} "
                              "butcesini asiyor; cagri yapilmadi."),
            "provider_attempts": [], "route_requested": list(TASK_ROUTES["antigravity_research"]),
            "route_planned": [], "output": None, "latency_sec": 0.0,
            "max_total_tokens": budget,
        }

    def call(client, model_id, spec):
        return client.models.generate_content(
            model=model_id, contents=prompt,
            config={"max_output_tokens": max(1, budget - estimate_input_tokens(prompt))},
        )

    router_kwargs.setdefault("estimated_input_tokens", estimate_input_tokens(prompt))
    result = execute_google_route(
        "antigravity_research", call,
        validate_fn=lambda raw, model_id: _response_text(raw), **router_kwargs
    )
    result["max_total_tokens"] = budget
    result["structured_output_capable"] = False
    if result["status"] == "ok":
        result["status"] = "experimental"
        result["status_reason"] = (
            "DENEYSEL antigravity raporu; review_required. Uretim artefakti degildir, "
            "onay durumunu degistirmez, hicbir sey yayinlamaz."
        )
    return result


def build_antigravity_artifact(result: dict) -> dict:
    artifact = build_google_artifact(
        result, task="antigravity_research", capability="generate_content(experimental agent)",
        output_content=result.get("output"), status="experimental",
        status_reason=result.get("status_reason"),
    )
    artifact["review_required"] = True
    artifact["production_ready"] = False
    artifact["max_total_tokens"] = result.get("max_total_tokens")
    artifact["structured_output_capable"] = False
    return artifact


# ---------------------------------------------------------------------------
# Free-tier data policy guard (Phase 9).
# ---------------------------------------------------------------------------

class RawMediaUploadBlocked(RuntimeError):
    pass


def assert_raw_media_allowed(allow_raw_media: bool, description: str = "kaynak video") -> None:
    """Refuses to send raw copyrighted media to the free tier unless the user
    explicitly opted in.

    Local FFmpeg / faster-whisper / PySceneDetect / librosa remain the first
    analysis stage; only derived transcript windows, timestamps, scene/audio
    scores and candidate metadata go to Gemini.
    """
    if not allow_raw_media:
        raise RawMediaUploadBlocked(
            f"ALLOW_RAW_MEDIA_TO_FREE_TIER=False: {description} ucretsiz katmana "
            "YUKLENMEZ. " + FREE_TIER_DATA_NOTICE + " Yuklemek istiyorsan bayragi "
            "acikca ac ve gerekli haklara sahip oldugunu teyit et."
        )


def raw_media_warning() -> str:
    return (
        "UYARI: ALLOW_RAW_MEDIA_TO_FREE_TIER=True. Ham video/ses Gemini ucretsiz "
        "katmanina gonderilecek. " + FREE_TIER_DATA_NOTICE + " Bu icerigi gondermek "
        "icin gerekli haklara sahip oldugundan emin ol."
    )
