"""Reusable LLM script-generation logic shared by 02_script_generation.ipynb
and 00_youtube_automation_master.ipynb.

Notebook 02 keeps its own multi-provider/multi-model benchmarking UI (running
several OpenAI/Anthropic/Google models side by side and letting a human score
them); the prompt construction, JSON-response parsing, and per-provider call
wrappers it benchmarks all live here so both notebooks share one
implementation instead of duplicating fragile JSON-parsing logic.

Never uploads or publishes anything. Never prints/logs/persists an API key.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

STATUS_VALUES = {
    "experimental", "development_placeholder", "review_required",
    "approved", "insufficient_data", "failed", "production_ready",
}


def build_user_prompt(selected_moment: dict, content_mode: str = "spoiler-free-recap",
                       tone: str = "gergin ve merak uyandirici", target_duration_sec: int = 60,
                       content_language: str | None = None) -> str:
    """Builds the exact user prompt sent to the LLM, from a hot_moments.json
    moment dict (rank/source_start_sec/source_end_sec/reason/transcript)."""
    summary = (
        f"An (rank {selected_moment['rank']}, "
        f"{selected_moment.get('source_start_sec', selected_moment.get('aligned_start_sec'))}-"
        f"{selected_moment.get('source_end_sec', selected_moment.get('aligned_end_sec'))}s): "
        f"{selected_moment.get('reason', '')}. Transcript: {selected_moment.get('transcript', '')}"
    )
    from .source_strategy import CONTENT_LANGUAGES, DEFAULT_CONTENT_LANGUAGE

    profile = CONTENT_LANGUAGES.get(content_language or DEFAULT_CONTENT_LANGUAGE) \
        or CONTENT_LANGUAGES[DEFAULT_CONTENT_LANGUAGE]
    # The language rule is stated FIRST and in the target language itself: a
    # trailing "oh and write it in English" after a Turkish instruction block is
    # exactly the kind of thing a model half-follows, producing a Turkish hook
    # on an English script.
    return (
        f"{profile['prompt_rule']} "
        f"Asagidaki sahne ozetinden, {target_duration_sec} saniyelik bir YouTube Shorts scripti yaz. "
        f"Ton: {tone}. Mod: {content_mode}. Spoiler verme, merak uyandir, guclu bir hook cumlesiyle basla. "
        f"Cikti dili: {profile['prompt_name']} ({profile['code']}). "
        f'SADECE JSON don: {{"script": "...", "hook": "...", "estimated_duration_sec": <int>}}\n\n'
        f"Sahne ozeti:\n{summary}"
    )


def script_extras(parsed: dict) -> dict:
    """The non-script fields the prompt asks for, normalised.

    The prompt requests `hook` and `estimated_duration_sec`; every provider path
    used to drop them on the floor, so a reviewer never saw the hook the model
    was told to write. Absent or unusable values stay absent -- nothing here
    invents a hook from the script text.
    """
    extras = {}
    hook = str(parsed.get("hook") or "").strip()
    if hook:
        extras["hook"] = hook[:400]
    try:
        estimated = int(parsed["estimated_duration_sec"])
    except (KeyError, TypeError, ValueError):
        estimated = None
    if estimated and estimated > 0:
        extras["estimated_duration_sec"] = estimated
    return extras


def parse_script_json(raw: str, candidate_name: str = "") -> dict | None:
    """Parses a raw LLM response into {'script': str, ...}. Returns None (and
    the caller should log a 'failed' result) rather than raising or silently
    fabricating a script when the JSON is malformed or missing 'script'."""
    try:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end <= start:
            raise ValueError("JSON blogu bulunamadi")
        parsed = json.loads(raw[start:end])
        if "script" not in parsed or not str(parsed["script"]).strip():
            raise ValueError("'script' alani eksik veya bos")
        return parsed
    except Exception as exc:
        print(f"[{candidate_name}] JSON ayristirma HATASI: {exc}. Ham cikti ilk 200 karakter: {raw[:200]!r}")
        return None


# Approximate per-1M-token prices; callers should treat these as ESTIMATES
# and check the provider's live pricing page for current values.
#
# These lists are the COMPARISON CATALOG used by notebook 02's deliberate
# benchmarking loop. The master pipeline must NEVER iterate them -- doing so
# fans out one script request into six paid calls. The master path goes through
# `build_script_candidates()` / `run_script_generation()`, which pick exactly
# one model and only advance on genuine failure.
OPENAI_SCRIPT_MODELS = [("gpt-5.6-luna", 1.00, 6.00), ("gpt-5.6-sol", 5.00, 30.00)]
ANTHROPIC_SCRIPT_MODELS = [("claude-haiku-4-5", 1.00, 5.00), ("claude-sonnet-5", 3.00, 15.00)]

# GOOGLE: the free-tier Route A models, in router order. Prices are 0.00 because
# these run on the free tier -- the real budget is RPM/TPM/RPD, which
# `google_ai.QuotaLedger` tracks, not dollars.
#
# `gemini-2.5-pro` was REMOVED: this free project has 0 quota for it, so it could
# never have succeeded and must never enter an automatic route
# (google_ai.MODEL_REGISTRY keeps it registered purely so the exclusion is
# auditable).
GOOGLE_SCRIPT_MODELS = [
    ("gemini-3.6-flash", 0.00, 0.00),
    ("gemini-3.5-flash", 0.00, 0.00),
    ("gemini-3.5-flash-lite", 0.00, 0.00),
]

SCRIPT_PROVIDER_CATALOG = {
    "openai": OPENAI_SCRIPT_MODELS,
    "anthropic": ANTHROPIC_SCRIPT_MODELS,
    "google": GOOGLE_SCRIPT_MODELS,
}

# The single default the master pipeline uses when SCRIPT_PROVIDER is not set.
# Google is now the CANONICAL default here (not only via a notebook override),
# because the free Gemini tier is this project's primary provider.
DEFAULT_SCRIPT_PROVIDER = "google"

# Deterministic order in which OTHER providers are tried when fallback is on.
SCRIPT_FALLBACK_PROVIDER_ORDER = ("google", "anthropic", "openai")


def primary_model_for(provider: str) -> tuple[str, float, float]:
    """Returns the (model, price_in, price_out) triple that is this provider's
    PRIMARY (cheapest-first) model. Fallback only ever uses primary models --
    never a provider's premium comparison sibling -- so enabling fallback
    cannot silently escalate cost."""
    catalog = SCRIPT_PROVIDER_CATALOG.get(provider)
    if not catalog:
        raise ValueError(
            f"bilinmeyen script saglayicisi: {provider!r} (beklenen: {sorted(SCRIPT_PROVIDER_CATALOG)})"
        )
    return catalog[0]


def resolve_model_entry(provider: str, model: str | None) -> tuple[str, float, float]:
    """Resolves an explicit (provider, model) pair to its priced catalog entry.
    model=None means 'this provider's primary model'."""
    catalog = SCRIPT_PROVIDER_CATALOG.get(provider)
    if not catalog:
        raise ValueError(
            f"bilinmeyen script saglayicisi: {provider!r} (beklenen: {sorted(SCRIPT_PROVIDER_CATALOG)})"
        )
    if model is None:
        return catalog[0]
    for entry in catalog:
        if entry[0] == model:
            return entry
    raise ValueError(
        f"'{provider}' saglayicisinda '{model}' modeli yok. Mevcut: {[e[0] for e in catalog]}"
    )


def build_script_candidates(provider: str | None, model: str | None, allow_fallback: bool,
                             available_providers) -> list[tuple[str, str, float, float]]:
    """Builds the ORDERED, DE-DUPLICATED candidate list for one script request.

    Position 0 is always the explicitly selected (provider, model). When
    allow_fallback is True, each OTHER available provider's primary model is
    appended in SCRIPT_FALLBACK_PROVIDER_ORDER. When it is False the list has
    exactly one entry, so a single request can cost at most one paid call.

    Providers without a configured client are excluded, so no candidate can be
    attempted with a missing credential.
    """
    available = {p for p in available_providers}
    selected_provider = provider or DEFAULT_SCRIPT_PROVIDER
    if selected_provider not in available:
        # Selected provider has no client; fall back to the first available one
        # in the deterministic order (only if fallback is permitted).
        if not allow_fallback:
            return []
        selected_provider = next((p for p in SCRIPT_FALLBACK_PROVIDER_ORDER if p in available), None)
        if selected_provider is None:
            return []
        model = None  # the requested model belonged to a different provider

    candidates: list[tuple[str, str, float, float]] = []
    model_name, price_in, price_out = resolve_model_entry(selected_provider, model)
    candidates.append((selected_provider, model_name, price_in, price_out))

    if allow_fallback:
        for other in SCRIPT_FALLBACK_PROVIDER_ORDER:
            if other == selected_provider or other not in available:
                continue
            other_model, other_in, other_out = primary_model_for(other)
            candidates.append((other, other_model, other_in, other_out))

    seen, unique = set(), []
    for candidate in candidates:
        key = (candidate[0], candidate[1])
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def call_openai_script(client, model_name: str, price_in: float, price_out: float, user_prompt: str) -> dict:
    """Calls one OpenAI chat-completions model with JSON response format.
    Returns {'candidate','script','cost_usd','latency_sec','provider','model'}
    or {'candidate','error'} on parse/API failure. `client` is an
    openai.OpenAI-compatible object (or a test double with the same surface)
    so this function never constructs its own client / imports the SDK
    directly, making it trivially mockable."""
    candidate = f"openai-{model_name}"
    try:
        t0 = time.time()
        resp = client.chat.completions.create(
            model=model_name, messages=[{"role": "user", "content": user_prompt}],
            temperature=0.7, response_format={"type": "json_object"},
        )
        latency = time.time() - t0
        parsed = parse_script_json(resp.choices[0].message.content, candidate)
        if parsed is None:
            return {"candidate": candidate, "error": "JSON ayristirma hatasi", "latency_sec": latency}
        usage = resp.usage
        cost = (usage.prompt_tokens / 1_000_000) * price_in + (usage.completion_tokens / 1_000_000) * price_out
        return {"candidate": candidate, "script": parsed["script"], "cost_usd": cost, "latency_sec": latency,
                "provider": "openai", "model": model_name, **script_extras(parsed)}
    except Exception as exc:
        return {"candidate": candidate, "error": f"API hatasi: {exc}", "latency_sec": 0.0}


def call_anthropic_script(client, model_name: str, price_in: float, price_out: float, user_prompt: str) -> dict:
    candidate = f"anthropic-{model_name}"
    try:
        t0 = time.time()
        resp = client.messages.create(model=model_name, max_tokens=500,
                                       messages=[{"role": "user", "content": user_prompt}])
        latency = time.time() - t0
        parsed = parse_script_json(resp.content[0].text, candidate)
        if parsed is None:
            return {"candidate": candidate, "error": "JSON ayristirma hatasi", "latency_sec": latency}
        cost = (resp.usage.input_tokens / 1_000_000) * price_in + (resp.usage.output_tokens / 1_000_000) * price_out
        return {"candidate": candidate, "script": parsed["script"], "cost_usd": cost, "latency_sec": latency,
                "provider": "anthropic", "model": model_name, **script_extras(parsed)}
    except Exception as exc:
        return {"candidate": candidate, "error": f"API hatasi: {exc}", "latency_sec": 0.0}


def call_google_script(client, model_name: str, price_in: float, price_out: float, user_prompt: str,
                        ledger=None, manifest=None, google_route_mode: str = "explicit",
                        sleep_fn=None) -> dict:
    """Google script generation -- ALWAYS through the canonical router.

    There is no direct `client.models.generate_content(...)` call here any more:
    quota accounting, retry/backoff, error classification, structured-output
    validation and sanitized errors all live in `local_pipeline.google_ai`, so a
    Google request made from notebook 02's benchmark obeys exactly the same
    policy as one made from the master pipeline.

    `google_route_mode='explicit'` pins the router to THIS model only, so this
    wrapper still costs exactly one model's worth of attempts and can never fan
    out across Route A.
    """
    from . import google_ai

    candidate = f"google-{model_name}"
    result = google_ai.run_google_script_generation(
        user_prompt, client=client, mode=google_route_mode,
        overrides={"script_generation": [model_name]}, manifest=manifest,
        ledger=ledger, enable_model_fallback=False, max_model_attempts=1,
        sleep_fn=sleep_fn, persist_ledger=ledger is not None,
    )
    if result["status"] == "ok":
        parsed = result["output"]
        approx_in = len(user_prompt) / 4
        approx_out = len(str(parsed.get("script", ""))) / 4
        cost = (approx_in / 1_000_000) * price_in + (approx_out / 1_000_000) * price_out
        return {"candidate": candidate, "script": parsed["script"], "cost_usd": cost,
                "latency_sec": result["latency_sec"], "provider": "google",
                "model": result["model"], "google_route": _google_route_summary(result),
                **script_extras(parsed)}
    return {
        "candidate": candidate,
        "error": result.get("status_reason") or "Google rotasi basarisiz",
        "latency_sec": result.get("latency_sec", 0.0),
        "google_route": _google_route_summary(result),
    }


def call_google_script_routed(client, user_prompt: str, *, ledger=None, manifest=None,
                               mode: str = "auto", overrides=None,
                               enable_model_fallback: bool = True,
                               max_model_attempts: int = 4, sleep_fn=None,
                               max_transient_retries: int = 2) -> dict:
    """Master-pipeline Google script generation: the FULL Route A chain.

    Unlike `call_google_script()` (one pinned model, for notebook 02's
    benchmark), this walks
    gemini-3.6-flash -> gemini-3.5-flash -> gemini-3.5-flash-lite -> gemma-4-31b-it,
    stopping at the first success. Still no fan-out: a later model is contacted
    only after the previous one genuinely failed.
    """
    from . import google_ai

    result = google_ai.run_google_script_generation(
        user_prompt, client=client, mode=mode, overrides=overrides, manifest=manifest,
        ledger=ledger, enable_model_fallback=enable_model_fallback,
        max_model_attempts=max_model_attempts, sleep_fn=sleep_fn,
        max_transient_retries=max_transient_retries,
        persist_ledger=ledger is not None,
    )
    summary = _google_route_summary(result)
    if result["status"] == "ok":
        return {"candidate": f"google-{result['model']}", "script": result["output"]["script"],
                "cost_usd": 0.0, "latency_sec": result["latency_sec"], "provider": "google",
                "model": result["model"], "google_route": summary,
                **script_extras(result["output"])}
    return {"candidate": "google-route", "error": result.get("status_reason") or "Google rotasi basarisiz",
            "latency_sec": result.get("latency_sec", 0.0), "google_route": summary}


def _google_route_summary(result: dict) -> dict:
    """Artifact-safe slice of a router result (no raw response, no exception)."""
    return {
        "route_requested": result.get("route_requested"),
        "route_planned": result.get("route_planned"),
        "provider_attempts": result.get("provider_attempts"),
        "selected_attempt": result.get("selected_attempt"),
        "fallback_used": result.get("fallback_used"),
        "fallback_reason": result.get("fallback_reason"),
        "usage_metadata": result.get("usage_metadata"),
        "estimated_quota_before": result.get("estimated_quota_before"),
        "estimated_quota_after": result.get("estimated_quota_after"),
        "status": result.get("status"),
        "error_kind": result.get("error_kind"),
    }


def build_selected_script_artifact(script_text, provider, model, cost_usd, latency_sec, selected_moment: dict,
                                    moment_review: dict, source_video_meta: dict, hot_moments_source_path: str,
                                    quality_score_1_5=None, language: str = "tr",
                                    prompt_package: dict | None = None,
                                    status_reason_override: str | None = None,
                                    attempts: list | None = None,
                                    hook: str = "",
                                    estimated_duration_sec: int | None = None,
                                    content_language: str | None = None) -> dict:
    """Builds the selected_script.json contract dict. If script_text is None
    (no provider available / moment not approved), a prompt-only package is
    recorded instead of fabricating a fake script.

    This function CANNOT produce status='approved' -- it deliberately takes no
    approval argument. Freshly generated content is always 'review_required',
    because nobody has read it yet. Promotion to 'approved' happens only in a
    separate, later step via `local_pipeline.approval.approve_artifact()`, which
    requires the SHA-256 of the content the human actually reviewed."""
    source_video_identifier = (
        source_video_meta.get("raw_video_path") or source_video_meta.get("analysis_clip_path") or "unknown"
    )
    selected_moment_summary = {
        "rank": selected_moment["rank"],
        "source_start_sec": selected_moment.get("source_start_sec", selected_moment.get("aligned_start_sec")),
        "source_end_sec": selected_moment.get("source_end_sec", selected_moment.get("aligned_end_sec")),
        "score": selected_moment.get("score"),
        "reason": selected_moment.get("reason"),
    }
    base = {
        "provider": provider,
        "model": model,
        "script_text": script_text,
        # Recorded verbatim when the model supplied one; never derived from the
        # script, so an empty hook honestly means "the model did not give one".
        "hook": str(hook or "").strip()[:400] or None,
        "estimated_duration_sec": estimated_duration_sec,
        # Recorded so TTS, the voice checklist and any later reader agree on
        # what language this text is, instead of inferring it.
        "language": language,
        "content_language": content_language,
        "selected_moment": selected_moment_summary,
        "moment_review": moment_review,
        "source_video_identifier": source_video_identifier,
        "source_video": source_video_meta,
        "hot_moments_source_path": str(hot_moments_source_path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "quality_score_1_5": quality_score_1_5,
        "cost_usd_estimate": cost_usd or 0.0,
        "latency_sec": latency_sec or 0.0,
    }
    # Every attempted provider/model and its failure reason is recorded, so a
    # fallback chain is auditable after the fact (including which candidates
    # were never tried because an earlier one succeeded).
    base["provider_attempts"] = list(attempts or [])
    base["human_approval"] = {
        "approved": False,
        "approved_by": None,
        "approved_at": None,
        "notes": None,
        "approved_content_sha256": None,
    }
    if script_text:
        base["status"] = "review_required"
        base["status_reason"] = status_reason_override or (
            "script uretildi; INSAN INCELEMESI BEKLIYOR (onay ayri bir adimda, "
            "approval.approve_artifact ile verilir)"
        )
    else:
        base["status"] = "review_required"
        base["status_reason"] = status_reason_override or (
            "no script produced; prompt package exported for external/manual generation"
        )
        base["prompt_package"] = prompt_package
    return base


def run_script_generation(hot_moments_data: dict, hot_moments_source_path, selected_moment_rank=None,
                           openai_client=None, anthropic_client=None, google_client=None,
                           content_mode: str = "spoiler-free-recap", tone: str = "gergin ve merak uyandirici",
                           target_duration_sec: int = 60, moment_approved: bool = False,
                           moment_approved_by: str = "", approval_notes: str = "",
                           script_provider: str | None = None, script_model: str | None = None,
                           allow_provider_fallback: bool = False,
                           google_ledger=None, google_manifest=None,
                           google_route_mode: str = "auto",
                           google_task_model_overrides=None,
                           enable_google_model_fallback: bool = True,
                           max_google_model_attempts: int = 4,
                           max_google_transient_retries: int = 2,
                           sleep_fn=None,
                           enable_cross_provider_fallback: bool = False,
                           max_provider_attempts: int = 3,
                           cross_provider_secret_getter=None,
                           cross_provider_transport=None,
                           content_language: str | None = None) -> dict:
    """Master-pipeline script generation: exactly ONE paid LLM call on the happy
    path.

    Cost contract (this is the whole point of the function):
      - Builds a candidate list via `build_script_candidates()`. With
        allow_provider_fallback=False that list has exactly ONE entry.
      - Calls candidates STRICTLY SEQUENTIALLY and RETURNS on the first valid
        result. A later candidate is contacted only when the previous one
        genuinely failed (API error, or a response that would not parse).
      - Never iterates a provider's comparison catalog, so one script request can
        never fan out into six paid calls. Notebook 02's deliberate benchmarking
        goes through the `call_*_script()` wrappers directly instead.

    Approval contract: the returned artifact is ALWAYS 'review_required' (or a
    prompt-only package). There is no approval argument; nothing generated here
    can be approved in this same call.

    `*_client` are pre-constructed SDK clients (or test doubles), so this
    function never reads a secret or builds a client itself.
    """
    moments_by_rank = {m["rank"]: m for m in hot_moments_data["hot_moments"]}
    if selected_moment_rank is None:
        selected_moment = min(hot_moments_data["hot_moments"], key=lambda m: m["rank"])
    else:
        if selected_moment_rank not in moments_by_rank:
            raise ValueError(
                f"selected_moment_rank={selected_moment_rank} hot_moments icinde yok. "
                f"Mevcut ranklar: {sorted(moments_by_rank)}"
            )
        selected_moment = moments_by_rank[selected_moment_rank]

    moment_review = {
        "moment_rank": selected_moment["rank"], "approved": moment_approved,
        "approved_by": moment_approved_by or None, "approval_notes": approval_notes or None,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    user_prompt = build_user_prompt(selected_moment, content_mode, tone, target_duration_sec,
                                    content_language=content_language)
    source_video_meta = hot_moments_data.get("source") or {}
    prompt_package = {
        "user_prompt": user_prompt, "content_mode": content_mode, "tone": tone,
        "target_duration_sec": target_duration_sec,
    }

    def _prompt_only(reason, attempts=None):
        return build_selected_script_artifact(
            None, None, None, 0.0, 0.0, selected_moment, moment_review, source_video_meta,
            hot_moments_source_path, prompt_package=prompt_package,
            status_reason_override=reason, attempts=attempts,
        )

    # The moment itself must be human-approved before ANY paid call is made.
    if not moment_approved:
        return _prompt_only("moment not approved (moment_approved=False); no paid LLM call was made")

    clients = {"openai": openai_client, "anthropic": anthropic_client, "google": google_client}
    callers = {
        "openai": call_openai_script,
        "anthropic": call_anthropic_script,
        "google": call_google_script,
    }
    available = [name for name, client in clients.items() if client is not None]
    if not available and not enable_cross_provider_fallback:
        return _prompt_only("no LLM client configured; prompt package exported for external/manual generation")

    candidates = build_script_candidates(script_provider, script_model, allow_provider_fallback, available)
    if not candidates and not enable_cross_provider_fallback:
        return _prompt_only(
            f"secilen saglayici ({script_provider or DEFAULT_SCRIPT_PROVIDER!r}) icin istemci yok ve "
            "provider fallback kapali; hicbir cagri yapilmadi"
        )

    attempts = []
    for provider_name, model_name, price_in, price_out in candidates:
        if provider_name == "google":
            # Google ALWAYS goes through the canonical router, which owns Route A
            # (3.6-flash -> 3.5-flash -> 3.5-flash-lite -> gemma-4-31b-it), the
            # quota ledger and the error/fallback policy. An explicit
            # `script_model` pins the router to that one model.
            overrides = dict(google_task_model_overrides or {})
            mode = google_route_mode
            if model_name and script_model:
                overrides["script_generation"] = [model_name]
                mode = "explicit"
            result = call_google_script_routed(
                clients["google"], user_prompt, ledger=google_ledger,
                manifest=google_manifest, mode=mode, overrides=overrides,
                enable_model_fallback=enable_google_model_fallback,
                max_model_attempts=max_google_model_attempts, sleep_fn=sleep_fn,
                max_transient_retries=max_google_transient_retries,
            )
        else:
            result = callers[provider_name](
                clients[provider_name], model_name, price_in, price_out, user_prompt,
            )
        if "script" in result:
            attempts.append({"provider": provider_name, "model": result.get("model", model_name),
                              "ok": True, "error": None,
                              "google_route": result.get("google_route")})
            return build_selected_script_artifact(
                result["script"], result["provider"], result["model"], result["cost_usd"],
                result["latency_sec"], selected_moment, moment_review, source_video_meta,
                hot_moments_source_path, attempts=attempts,
                hook=result.get("hook", ""),
                estimated_duration_sec=result.get("estimated_duration_sec"),
                content_language=content_language,
            )
        # Genuine failure -> record it and only THEN consider the next candidate.
        attempts.append({
            "provider": provider_name, "model": model_name, "ok": False,
            "error": result.get("error", "bilinmeyen hata"),
            "google_route": result.get("google_route"),
        })

    # Safety refusals are terminal across providers. A provider switch must not
    # be used to bypass a model's safety decision.
    safety_refused = any(
        ((attempt.get("google_route") or {}).get("error_kind") == "safety_refusal")
        for attempt in attempts
    )
    if enable_cross_provider_fallback and not safety_refused:
        from . import provider_adapters

        fallback = provider_adapters.run_cross_provider_text_fallback(
            user_prompt, capability="high_quality_text",
            secret_getter=cross_provider_secret_getter,
            transport=cross_provider_transport,
            max_provider_attempts=max_provider_attempts,
            enabled=True,
            validator=lambda raw: parse_script_json(raw, "cross-provider"),
        )
        attempts.extend({
            "provider": item.get("provider"), "model": item.get("model"),
            "ok": item.get("ok", False), "error": item.get("error"),
            "skipped": item.get("skipped", False), "cross_provider": True,
        } for item in fallback.get("provider_attempts", []))
        if fallback.get("status") == "ok":
            parsed = fallback["output"]
            extras = script_extras(parsed)
            return build_selected_script_artifact(
                parsed["script"], fallback["provider"], fallback["model"], 0.0, 0.0,
                selected_moment, moment_review, source_video_meta,
                hot_moments_source_path, attempts=attempts,
                hook=extras.get("hook", ""),
                estimated_duration_sec=extras.get("estimated_duration_sec"),
                content_language=content_language,
            )

    return _prompt_only(
        f"tum aday saglayicilar basarisiz oldu ({len(attempts)} deneme); prompt paketi disa aktarildi",
        attempts=attempts,
    )


