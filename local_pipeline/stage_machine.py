"""A persisted, resumable stage machine shared by every long-running workflow.

WHY THIS WAS EXTRACTED
----------------------
`long_form.py` grew a complete stage machine -- persisted per-stage state,
forward invalidation, quota waits with `resume_at`, billing blocks, cancellation
and a progress view model. Every one of its functions read the module-level
`STAGE_KEYS`, so a second workflow with a different stage list could not reuse a
single line of it.

Copying it would have been the cheap move and the wrong one: waiting, blocking
and cancellation semantics are subtle, and two copies drift until the same bug
has to be fixed twice. So the mechanics live here, parameterised by the stage
list, and each workflow supplies only its own stages.

WHAT A WORKFLOW STILL OWNS
--------------------------
Its stage names and labels, which stages a machine may run unattended, and what
each stage actually does. This module knows none of that -- it calls no
provider, touches no file, and decides only what SHOULD happen next.
"""
from __future__ import annotations

from datetime import datetime, timezone

#: Every state a stage can be persisted in. `waiting_*` and `blocked_billing`
#: exist because "stopped because the provider said no" is a different fact from
#: "failed", and only one of the two is worth retrying immediately.
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


class StageError(ValueError):
    """A rejected stage transition, phrased for the person who caused it."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_moment(value):
    """An ISO timestamp as an aware datetime, or None. Never raises."""
    try:
        moment = datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


class StageMachine:
    """The mechanics of one ordered, persisted stage list.

    `stages` is an ordered sequence of `(key, label, automatic)`. Order IS the
    dependency order: a stage may only run once every earlier stage is
    `completed`.
    """

    def __init__(self, stages, *, artifact_type: str, schema_version: int = 1,
                 error_cls=StageError):
        self.stages = tuple(stages)
        self.keys = tuple(key for key, _label, _auto in self.stages)
        self.labels = {key: label for key, label, _auto in self.stages}
        self.automatic = frozenset(key for key, _label, auto in self.stages if auto)
        self.artifact_type = artifact_type
        self.schema_version = schema_version
        self.error_cls = error_cls

    # ------------------------------------------------------------ creation
    def new_workflow(self, *, ownership=None, **extra) -> dict:
        record = {
            "schema_version": self.schema_version,
            "artifact_type": self.artifact_type,
            "status": NOT_STARTED,
            "not_uploaded": True,
            "paused": False,
            "cancelled": False,
            "stages": {key: {"state": NOT_STARTED, "started_at": None,
                             "updated_at": None, "attempts": 0, "error": None,
                             "detail": None}
                       for key in self.keys},
            "created_at": _now(),
            "updated_at": _now(),
        }
        record.update(extra)
        if ownership is not None:
            record.update(ownership.to_dict())
        return record

    # ------------------------------------------------------------ mutation
    def stage_state(self, record, key: str) -> str:
        return ((record or {}).get("stages") or {}).get(key, {}).get(
            "state", NOT_STARTED)

    def set_stage(self, record: dict, key: str, state: str, *, error=None,
                  detail=None, attempt_delta: int = 0, resume_at=None) -> dict:
        if key not in self.keys:
            raise self.error_cls(f"bilinmeyen asama: {key!r}")
        if state not in STAGE_STATES:
            raise self.error_cls(f"bilinmeyen asama durumu: {state!r}")
        record = dict(record or self.new_workflow())
        stages = {name: dict(value)
                  for name, value in (record.get("stages") or {}).items()}
        entry = dict(stages.get(key) or {})
        entry["state"] = state
        entry["updated_at"] = _now()
        entry["attempts"] = int(entry.get("attempts") or 0) + max(0, int(attempt_delta))
        if state == RUNNING and not entry.get("started_at"):
            entry["started_at"] = _now()
        # A sanitized message only: provider payloads and prompts never land here.
        entry["error"] = (str(error)[:400] if error else None)
        entry["resume_at"] = (str(resume_at or "") or None
                              if state in WAITING_STATES else None)
        if detail is not None:
            entry["detail"] = detail
        stages[key] = entry
        record["stages"] = stages
        record["updated_at"] = _now()
        record["status"] = self.overall_status(record)
        return record

    def invalidate_from(self, record: dict, key: str, *, reason: str = "") -> dict:
        """Reset `key` and every stage after it.

        Editing an upstream artifact cannot leave the stages after it claiming
        to describe something that no longer exists. Resetting FORWARD is the
        honest response; resetting backward would throw away work that is still
        perfectly valid.
        """
        if key not in self.keys:
            raise self.error_cls(f"bilinmeyen asama: {key!r}")
        record = dict(record or self.new_workflow())
        for name in self.keys[self.keys.index(key):]:
            if self.stage_state(record, name) != NOT_STARTED:
                record = self.set_stage(record, name, NOT_STARTED,
                                        detail=(reason or "yukari-akis degisti"))
        return record

    # ------------------------------------------------------------- queries
    def next_stage(self, record):
        """The first stage that still has work, or None when everything is done.

        Blocked stages stop the walk instead of being skipped: running a later
        stage while a review is still open is exactly the failure the review
        exists to prevent.
        """
        if (record or {}).get("cancelled"):
            return None
        for key in self.keys:
            if self.stage_state(record, key) in TERMINAL_OK:
                continue
            return key
        return None

    def runnable_stage(self, record, *, now=None) -> dict:
        """What a job may pick up right now, and if nothing, why not."""
        if (record or {}).get("cancelled"):
            return {"stage": None, "blocked": True, "reason": "is akisi iptal edildi"}
        if (record or {}).get("paused"):
            return {"stage": None, "blocked": True, "reason": "is akisi duraklatildi"}
        key = self.next_stage(record)
        if key is None:
            return {"stage": None, "blocked": False, "reason": "tum asamalar tamam"}
        state = self.stage_state(record, key)
        label = self.labels[key]
        if key not in self.automatic:
            return {"stage": key, "blocked": True,
                    "reason": f"{label} insan onayi bekliyor"}
        if state == RUNNING:
            return {"stage": key, "blocked": True,
                    "reason": f"{label} zaten calisiyor"}
        if state == BLOCKED_BILLING:
            return {
                "stage": key, "blocked": True, "billing_blocked": True,
                "reason": (f"{label} Google AI Studio plan/billing veya kota "
                           "yapilandirmasi nedeniyle durdu; ayarlari duzelttikten "
                           "sonra acikca yeniden deneyin"),
            }
        if state in WAITING_STATES:
            entry = ((record or {}).get("stages") or {}).get(key, {})
            resume_at = parse_moment(entry.get("resume_at"))
            current = now or datetime.now(timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            if resume_at is not None and current < resume_at:
                wait_seconds = max(1, int((resume_at - current).total_seconds()))
                kind = ("dakika kotasi" if state == WAITING_RATE_LIMIT
                        else "gunluk kota")
                return {"stage": key, "blocked": True, "waiting": True,
                        "wait_state": state, "resume_at": resume_at.isoformat(),
                        "wait_seconds": wait_seconds,
                        "reason": (f"{label} {kind} nedeniyle bekliyor; "
                                   f"{resume_at.isoformat()} sonrasinda devam "
                                   "edebilir")}
        return {"stage": key, "blocked": False,
                "reason": f"{label} calistirilabilir"}

    def overall_status(self, record) -> str:
        if (record or {}).get("cancelled"):
            return CANCELLED
        states = [self.stage_state(record, key) for key in self.keys]
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

    def progress_summary(self, record) -> dict:
        stages = (record or {}).get("stages") or {}
        states = {key: self.stage_state(record, key) for key in self.keys}
        done = len([key for key, state in states.items() if state == COMPLETED])
        return {
            "stages": [
                {"key": key, "label": self.labels[key], "state": states[key],
                 "automatic": key in self.automatic,
                 "attempts": stages.get(key, {}).get("attempts", 0),
                 "error": stages.get(key, {}).get("error"),
                 "detail": stages.get(key, {}).get("detail"),
                 "resume_at": stages.get(key, {}).get("resume_at"),
                 "started_at": stages.get(key, {}).get("started_at"),
                 "updated_at": stages.get(key, {}).get("updated_at")}
                for key in self.keys
            ],
            "completed_count": done,
            "total_count": len(self.keys),
            "percent": round(done / len(self.keys) * 100),
            "status": self.overall_status(record),
            "paused": bool((record or {}).get("paused")),
            "cancelled": bool((record or {}).get("cancelled")),
            "next": self.runnable_stage(record),
        }
