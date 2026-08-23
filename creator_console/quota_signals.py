"""Turning a provider refusal into a scheduling decision, once, for everyone.

WHY THIS IS SHARED
------------------
"The provider said no" has at least four meanings and they call for opposite
responses: a per-minute limit means wait a minute; a daily limit means come back
tomorrow and retrying now just burns what is left; a billing/plan block means no
amount of waiting helps and a person has to change a setting; anything else is a
genuine failure worth reporting as one.

The first live long-form pilot ran straight into this: a 429 was recorded as
`failed`, which invited a retry that spent more of the quota that had just run
out. Getting it right is subtle enough that a second copy in a second workflow
would drift, and the drift would only show up in production. So the
classification lives here and every workflow asks the same question of it.

These functions decide WHAT a signal means. They do not persist anything and
they do not know which workflow is asking.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from local_pipeline import google_ai, stage_machine, tts_chunks


def _aware(value):
    """An ISO string as an aware datetime, or None. Never raises."""
    try:
        moment = datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


def quota_kind_from_result(result: dict):
    """Classify a routed call's own report of why it failed.

    The route can retry across models, so the decisive error may be buried in
    `provider_attempts` rather than at the top level -- reading only the top
    level once classified a 401 as transient.
    """
    kinds = {str((result or {}).get("error_kind") or "")}
    kinds.update(str(item.get("error_kind") or "")
                 for item in ((result or {}).get("provider_attempts") or []))
    if google_ai.ERROR_BILLING_QUOTA in kinds:
        return stage_machine.BLOCKED_BILLING
    if google_ai.ERROR_RATE_LIMIT_RPD in kinds:
        return stage_machine.WAITING_DAILY_QUOTA
    if google_ai.ERROR_RATE_LIMIT_RPM in kinds:
        return stage_machine.WAITING_RATE_LIMIT
    return None


def quota_kind_from_exception(exc):
    """Classify a raised provider error the route did not wrap.

    The pilot's 429 arrived as a bare exception, so reading only
    `provider_attempts` missed it entirely.
    """
    kind = google_ai.classify_google_error(exc)
    if kind == google_ai.ERROR_BILLING_QUOTA:
        return stage_machine.BLOCKED_BILLING
    if kind == google_ai.ERROR_RATE_LIMIT_RPD:
        return stage_machine.WAITING_DAILY_QUOTA
    if kind == google_ai.ERROR_RATE_LIMIT_RPM:
        return stage_machine.WAITING_RATE_LIMIT
    return None


def quota_resume_at(quota_kind: str) -> str:
    """When it is honest to try again. A daily limit resets on Google's
    Pacific-time boundary, not 24 hours from now."""
    now = datetime.now(timezone.utc)
    if quota_kind == stage_machine.WAITING_DAILY_QUOTA:
        return google_ai.next_pacific_reset(now).isoformat()
    return (now + timedelta(seconds=60)).isoformat()


def tts_quota_wait(ledger, model: str, estimated_tokens: int) -> dict:
    """Ask the ledger, before spending anything, whether a call may proceed.

    Checking first rather than calling and handling the rejection matters
    because a rejected call still counts against some providers' limits, and
    because a persisted `resume_at` lets the workflow be closed and reopened
    instead of holding a process open to sleep.
    """
    reason = ledger.blocking_reason(model, estimated_tokens)
    if not reason:
        return {"ready": True, "model": model}

    record = ledger.record_for(model)
    now = ledger._now()  # the ledger owns the injectable clock
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    daily_until = _aware(record.get("daily_exhausted_until"))
    rpd = int(record.get("configured_rpd") or 0)
    if ((daily_until and now < daily_until)
            or (rpd and int(record.get("requests_current_day") or 0) >= rpd)):
        resume_at = daily_until or google_ai.next_pacific_reset(now)
        return {"ready": False, "state": stage_machine.WAITING_DAILY_QUOTA,
                "chunk_state": tts_chunks.WAITING_DAILY_QUOTA,
                "resume_at": resume_at.isoformat(), "reason": reason,
                "model": model}

    minute_start = _aware(record.get("minute_window_start")) or now
    rpm = int(record.get("configured_rpm") or 0)
    tpm = int(record.get("configured_tpm") or 0)
    rpm_full = rpm and int(record.get("requests_current_minute") or 0) >= rpm
    tpm_full = (tpm and estimated_tokens
                and int(record.get("estimated_input_tokens_current_minute") or 0)
                + int(estimated_tokens) > tpm)
    cooldown = _aware(record.get("cooldown_until"))
    if rpm_full or tpm_full or (cooldown and now < cooldown):
        resume_at = (cooldown if cooldown and now < cooldown
                     else minute_start + timedelta(seconds=60))
        if resume_at <= now:
            resume_at = now + timedelta(seconds=1)
        return {"ready": False, "state": stage_machine.WAITING_RATE_LIMIT,
                "chunk_state": tts_chunks.WAITING_RATE_LIMIT,
                "resume_at": resume_at.isoformat(), "reason": reason,
                "model": model}

    # A disabled or not-found model has no honest automatic resume time, so
    # none is invented: the caller must surface the reason to a person.
    return {"ready": False, "state": None, "resume_at": None,
            "reason": reason, "model": model}
