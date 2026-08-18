"""Long-form (15-20 minute) video workflow: stages, state and estimates.

WHY A STAGE MACHINE AND NOT ONE BIG JOB
---------------------------------------
An 18 minute narration is ~2500 words, a dozen text calls and twenty-odd TTS
requests. Run as a single job, one failure in the last minute throws away every
success before it -- and on a metered API that is money, not just time. So the
work is twelve PERSISTED stages: a restart resumes at the first stage that is
not `completed`, and a stage that failed is the only thing retried.

The stage list is deliberately longer than the number of provider calls. Human
review points are stages too, because "waiting for a person" is a real state the
workflow can be restarted into, not a gap between jobs.

WHAT THIS MODULE DOES NOT DO
----------------------------
It calls no provider and touches no file. It decides what SHOULD happen next and
what a brief implies; the service layer performs it. That keeps every transition
testable without a network and lets the notebook reuse the same rules.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

SCHEMA_VERSION = 1
WORKFLOW_ARTIFACT = "results/long_form/workflow.json"

#: Ordered. Position in this tuple IS the dependency order: a stage may only run
#: when every earlier stage is `completed`.
STAGES = (
    ("research", "Arastirma paketi", True),
    ("outline", "Outline", True),
    ("chapters", "Bolum scriptleri", True),
    ("consistency", "Tam script tutarlilik incelemesi", True),
    ("script_review", "Insan script incelemesi", False),
    ("tts", "TTS parcalari", True),
    ("audio_review", "Ses incelemesi", False),
    ("broll", "Kaynak / B-roll eslemesi", False),
    ("timeline", "16:9 zaman cizelgesi", True),
    ("preview_render", "Onizleme render", True),
    ("final_render", "Final render", True),
    ("archive", "Arsiv / disa aktarim", False),
)

STAGE_KEYS = tuple(key for key, _label, _auto in STAGES)
STAGE_LABELS = {key: label for key, label, _auto in STAGES}
#: Stages a job may run on its own. The rest wait for a person and reach
#: `completed` only through an explicit approval.
AUTOMATIC_STAGES = frozenset(key for key, _label, auto in STAGES if auto)

NOT_STARTED = "not_started"
QUEUED = "queued"
RUNNING = "running"
WAITING_RATE_LIMIT = "waiting_rate_limit"
WAITING_DAILY_QUOTA = "waiting_daily_quota"
BLOCKED_BILLING = "blocked_billing"
REVIEW_REQUIRED = "review_required"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
WAITING_STATES = frozenset({WAITING_RATE_LIMIT, WAITING_DAILY_QUOTA})
BLOCKED_STATES = frozenset({BLOCKED_BILLING})
STAGE_STATES = (NOT_STARTED, QUEUED, RUNNING, WAITING_RATE_LIMIT,
                WAITING_DAILY_QUOTA, BLOCKED_BILLING, REVIEW_REQUIRED,
                COMPLETED, FAILED, CANCELLED)
TERMINAL_OK = frozenset({COMPLETED})

#: Narration pace band. Used for word targets AND for turning a word count back
#: into an expected duration, so the two cannot drift apart.
WPM_MIN = 130
WPM_MAX = 150
DURATION_MIN_MIN = 15
DURATION_MAX_MIN = 20
DEFAULT_DURATION_MIN = 18
CHAPTER_MIN = 5
CHAPTER_MAX = 7

#: One TTS request should carry 60-120 seconds of audio. Shorter wastes requests
#: on a per-request-priced API; longer makes a single failure expensive to redo.
TTS_CHUNK_TARGET_SEC = 90
TTS_CHUNK_MIN_SEC = 60
TTS_CHUNK_MAX_SEC = 120

CONTENT_TYPES = ("theory", "commentary", "explainer", "general_knowledge")
DEFAULT_ASPECT_RATIO = "16:9"


class LongFormError(ValueError):
    """A rejected long-form request, phrased for the person who made it."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(text) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def count_words(text) -> int:
    return len(re.findall(r"\S+", str(text or "")))


# ---------------------------------------------------------------- estimates
def word_target(duration_min) -> dict:
    """Word band for a duration, derived from the narration pace.

    A band rather than a single number: a script is not wrong for landing at
    2500 instead of 2520 words, and pretending otherwise would make every
    chapter look like a failure.
    """
    duration = int(duration_min)
    return {"minimum": duration * WPM_MIN, "maximum": duration * WPM_MAX,
            "target": round(duration * (WPM_MIN + WPM_MAX) / 2)}


def duration_estimate_sec(word_count) -> dict:
    """Seconds a word count would take to read, as a band."""
    words = max(0, int(word_count or 0))
    return {"minimum_sec": round(words / WPM_MAX * 60, 1),
            "maximum_sec": round(words / WPM_MIN * 60, 1),
            "expected_sec": round(words / ((WPM_MIN + WPM_MAX) / 2) * 60, 1)}


def chapter_targets(duration_min, chapter_count) -> list:
    """Per-chapter word targets that ADD UP to the whole.

    The remainder is spread over the first chapters rather than dumped on the
    last one, so no single chapter silently carries every other chapter's
    rounding error.
    """
    count = int(chapter_count)
    total = word_target(duration_min)["target"]
    base, remainder = divmod(total, count)
    targets = []
    for index in range(count):
        words = base + (1 if index < remainder else 0)
        targets.append({"index": index + 1, "target_words": words,
                        "minimum_words": int(words * 0.85),
                        "maximum_words": int(words * 1.15)})
    return targets


def estimate_tts_chunks(total_words) -> int:
    """How many TTS requests a narration of this size implies."""
    words_per_chunk = TTS_CHUNK_TARGET_SEC / 60 * ((WPM_MIN + WPM_MAX) / 2)
    return max(1, int(round(max(1, int(total_words or 0)) / words_per_chunk)))


def estimate_api_calls(chapter_count, total_words) -> dict:
    """Provider requests a full run implies, itemised.

    Shown BEFORE the run, because on a metered API the honest question is not
    "shall I start" but "shall I spend this". A single total would hide that
    almost all of it is TTS.
    """
    chapters = int(chapter_count)
    tts = estimate_tts_chunks(total_words)
    return {
        "research": 1, "outline": 1, "chapters": chapters, "consistency": 1,
        "tts_chunks": tts, "total": 3 + chapters + tts,
        "note": ("Yeniden denemeler ve elle yeniden uretimler bu sayiya dahil "
                 "degildir; her gercek saglayici cagrisi ayrica sayilir."),
    }


def validate_brief(*, duration_min, chapter_count, content_type) -> None:
    """Reject a brief this workflow cannot honour, with the reason."""
    try:
        duration = int(duration_min)
        chapters = int(chapter_count)
    except (TypeError, ValueError) as exc:
        raise LongFormError("sure ve bolum sayisi tam sayi olmali") from exc
    if not DURATION_MIN_MIN <= duration <= DURATION_MAX_MIN:
        raise LongFormError(
            f"hedef sure {DURATION_MIN_MIN}-{DURATION_MAX_MIN} dakika olmali "
            f"({duration} verildi)")
    if not CHAPTER_MIN <= chapters <= CHAPTER_MAX:
        raise LongFormError(
            f"bolum sayisi {CHAPTER_MIN}-{CHAPTER_MAX} olmali ({chapters} verildi)")
    if content_type not in CONTENT_TYPES:
        raise LongFormError(
            f"bilinmeyen icerik turu: {content_type!r} (beklenen {list(CONTENT_TYPES)})")


def validate_final_duration(measured_sec) -> dict:
    """Does the finished narration land inside the 15-20 minute promise?

    Reported rather than enforced: a 14:50 narration is a real artifact and
    deleting it would help nobody. The reviewer is told, and decides.
    """
    seconds = float(measured_sec or 0.0)
    minimum = DURATION_MIN_MIN * 60
    maximum = DURATION_MAX_MIN * 60
    inside = minimum <= seconds <= maximum
    return {
        "measured_sec": round(seconds, 1),
        "measured_min": round(seconds / 60.0, 2),
        "target_min": [DURATION_MIN_MIN, DURATION_MAX_MIN],
        "within_target": inside,
        "reason": ("" if inside else
                   f"Anlatim {round(seconds / 60.0, 2)} dakika; hedef "
                   f"{DURATION_MIN_MIN}-{DURATION_MAX_MIN} dakika."),
    }


# ----------------------------------------------------------------- workflow
def new_workflow(*, plan_sha256: str = "", ownership=None) -> dict:
    record = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "long_form_workflow",
        "status": NOT_STARTED,
        "not_uploaded": True,
        "plan_sha256": plan_sha256,
        "paused": False,
        "cancelled": False,
        "stages": {key: {"state": NOT_STARTED, "started_at": None,
                         "updated_at": None, "attempts": 0, "error": None,
                         "detail": None}
                   for key in STAGE_KEYS},
        "created_at": _now(),
        "updated_at": _now(),
    }
    if ownership is not None:
        record.update(ownership.to_dict())
    return record


def stage_state(record, key: str) -> str:
    return ((record or {}).get("stages") or {}).get(key, {}).get("state", NOT_STARTED)


def set_stage(record: dict, key: str, state: str, *, error=None, detail=None,
              attempt_delta: int = 0, resume_at=None) -> dict:
    if key not in STAGE_KEYS:
        raise LongFormError(f"bilinmeyen asama: {key!r}")
    if state not in STAGE_STATES:
        raise LongFormError(f"bilinmeyen asama durumu: {state!r}")
    record = dict(record or new_workflow())
    stages = {name: dict(value) for name, value in (record.get("stages") or {}).items()}
    entry = dict(stages.get(key) or {})
    entry["state"] = state
    entry["updated_at"] = _now()
    entry["attempts"] = int(entry.get("attempts") or 0) + max(0, int(attempt_delta))
    if state == RUNNING and not entry.get("started_at"):
        entry["started_at"] = _now()
    # A sanitized message only: provider payloads and prompts never land here.
    entry["error"] = (str(error)[:400] if error else None)
    if state in WAITING_STATES:
        entry["resume_at"] = str(resume_at or "") or None
    else:
        entry["resume_at"] = None
    if detail is not None:
        entry["detail"] = detail
    stages[key] = entry
    record["stages"] = stages
    record["updated_at"] = _now()
    record["status"] = overall_status(record)
    return record


def invalidate_from(record: dict, key: str, *, reason: str = "") -> dict:
    """Reset `key` and every stage after it.

    Editing chapter 3 cannot leave the consistency review, the audio and the
    render claiming to describe a script that no longer exists. Resetting
    FORWARD is the honest response; resetting backward would throw away research
    that is still perfectly valid.
    """
    if key not in STAGE_KEYS:
        raise LongFormError(f"bilinmeyen asama: {key!r}")
    record = dict(record or new_workflow())
    for name in STAGE_KEYS[STAGE_KEYS.index(key):]:
        if stage_state(record, name) != NOT_STARTED:
            record = set_stage(record, name, NOT_STARTED,
                               detail=(reason or "yukari-akis degisti"))
    return record


def next_stage(record):
    """The first stage that still has work, or None when everything is done.

    Blocked stages stop the walk instead of being skipped: rendering while the
    script review is still open is exactly the failure the review exists to
    prevent.
    """
    if (record or {}).get("cancelled"):
        return None
    for key in STAGE_KEYS:
        if stage_state(record, key) in TERMINAL_OK:
            continue
        return key
    return None


def _parse_moment(value):
    try:
        moment = datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def runnable_stage(record, *, now=None) -> dict:
    """What a job may pick up right now, and if nothing, why not."""
    if (record or {}).get("cancelled"):
        return {"stage": None, "blocked": True, "reason": "is akisi iptal edildi"}
    if (record or {}).get("paused"):
        return {"stage": None, "blocked": True, "reason": "is akisi duraklatildi"}
    key = next_stage(record)
    if key is None:
        return {"stage": None, "blocked": False, "reason": "tum asamalar tamam"}
    state = stage_state(record, key)
    if key not in AUTOMATIC_STAGES:
        return {"stage": key, "blocked": True,
                "reason": f"{STAGE_LABELS[key]} insan onayi bekliyor"}
    if state == RUNNING:
        return {"stage": key, "blocked": True,
                "reason": f"{STAGE_LABELS[key]} zaten calisiyor"}
    if state == BLOCKED_BILLING:
        return {
            "stage": key, "blocked": True, "billing_blocked": True,
            "reason": (f"{STAGE_LABELS[key]} Google AI Studio plan/billing veya "
                       "kota yapilandirmasi nedeniyle durdu; ayarlari duzelttikten "
                       "sonra acikca yeniden deneyin"),
        }
    if state in WAITING_STATES:
        entry = ((record or {}).get("stages") or {}).get(key, {})
        resume_at = _parse_moment(entry.get("resume_at"))
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        if resume_at is not None and current < resume_at:
            wait_seconds = max(1, int((resume_at - current).total_seconds()))
            label = ("dakika kotasi" if state == WAITING_RATE_LIMIT
                     else "gunluk kota")
            return {"stage": key, "blocked": True, "waiting": True,
                    "wait_state": state, "resume_at": resume_at.isoformat(),
                    "wait_seconds": wait_seconds,
                    "reason": (f"{STAGE_LABELS[key]} {label} nedeniyle "
                               f"bekliyor; {resume_at.isoformat()} sonrasinda "
                               "devam edebilir")}
    return {"stage": key, "blocked": False,
            "reason": f"{STAGE_LABELS[key]} calistirilabilir"}


def overall_status(record) -> str:
    if (record or {}).get("cancelled"):
        return CANCELLED
    states = [stage_state(record, key) for key in STAGE_KEYS]
    if all(state == COMPLETED for state in states):
        return COMPLETED
    if any(state == FAILED for state in states):
        return FAILED
    if any(state == RUNNING for state in states):
        return RUNNING
    if any(state == BLOCKED_BILLING for state in states):
        return BLOCKED_BILLING
    if any(state == WAITING_DAILY_QUOTA for state in states):
        return WAITING_DAILY_QUOTA
    if any(state == WAITING_RATE_LIMIT for state in states):
        return WAITING_RATE_LIMIT
    if any(state == REVIEW_REQUIRED for state in states):
        return REVIEW_REQUIRED
    if all(state == NOT_STARTED for state in states):
        return NOT_STARTED
    return QUEUED


def progress_summary(record) -> dict:
    stages = (record or {}).get("stages") or {}
    states = {key: stage_state(record, key) for key in STAGE_KEYS}
    done = len([key for key, state in states.items() if state == COMPLETED])
    return {
        "stages": [
            {"key": key, "label": STAGE_LABELS[key], "state": states[key],
             "automatic": key in AUTOMATIC_STAGES,
             "attempts": stages.get(key, {}).get("attempts", 0),
             "error": stages.get(key, {}).get("error"),
             "detail": stages.get(key, {}).get("detail"),
             "resume_at": stages.get(key, {}).get("resume_at"),
             "started_at": stages.get(key, {}).get("started_at"),
             "updated_at": stages.get(key, {}).get("updated_at")}
            for key in STAGE_KEYS
        ],
        "completed_count": done,
        "total_count": len(STAGE_KEYS),
        "percent": round(done / len(STAGE_KEYS) * 100),
        "status": overall_status(record),
        "paused": bool((record or {}).get("paused")),
        "cancelled": bool((record or {}).get("cancelled")),
        "next": runnable_stage(record),
    }
