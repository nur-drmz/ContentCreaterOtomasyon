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

from . import stage_machine

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

# Re-exported so existing callers keep using `long_form.COMPLETED` etc. There is
# now exactly ONE definition of each, in `stage_machine`.
NOT_STARTED = stage_machine.NOT_STARTED
QUEUED = stage_machine.QUEUED
RUNNING = stage_machine.RUNNING
WAITING_RATE_LIMIT = stage_machine.WAITING_RATE_LIMIT
WAITING_DAILY_QUOTA = stage_machine.WAITING_DAILY_QUOTA
BLOCKED_BILLING = stage_machine.BLOCKED_BILLING
REVIEW_REQUIRED = stage_machine.REVIEW_REQUIRED
COMPLETED = stage_machine.COMPLETED
FAILED = stage_machine.FAILED
CANCELLED = stage_machine.CANCELLED
WAITING_STATES = stage_machine.WAITING_STATES
BLOCKED_STATES = stage_machine.BLOCKED_STATES
STAGE_STATES = stage_machine.STAGE_STATES
TERMINAL_OK = stage_machine.TERMINAL_OK

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
# The mechanics live in `stage_machine.StageMachine`, parameterised by the stage
# list above. Long-form was the only workflow when they were written, so every
# function read the module-level STAGE_KEYS directly and a second workflow could
# not reuse a line of it. The module-level names below are kept EXACTLY as they
# were so `long_form_studio.py` and `verify_long_form_studio.py` do not change.
_MACHINE = stage_machine.StageMachine(
    STAGES, artifact_type="long_form_workflow", schema_version=SCHEMA_VERSION,
    error_cls=LongFormError)


def new_workflow(*, plan_sha256: str = "", ownership=None) -> dict:
    return _MACHINE.new_workflow(ownership=ownership, plan_sha256=plan_sha256)


def stage_state(record, key: str) -> str:
    return _MACHINE.stage_state(record, key)


def set_stage(record: dict, key: str, state: str, *, error=None, detail=None,
              attempt_delta: int = 0, resume_at=None) -> dict:
    return _MACHINE.set_stage(record, key, state, error=error, detail=detail,
                              attempt_delta=attempt_delta, resume_at=resume_at)


def invalidate_from(record: dict, key: str, *, reason: str = "") -> dict:
    return _MACHINE.invalidate_from(record, key, reason=reason)


def next_stage(record):
    return _MACHINE.next_stage(record)


def runnable_stage(record, *, now=None) -> dict:
    return _MACHINE.runnable_stage(record, now=now)


def overall_status(record) -> str:
    return _MACHINE.overall_status(record)


def progress_summary(record) -> dict:
    return _MACHINE.progress_summary(record)
