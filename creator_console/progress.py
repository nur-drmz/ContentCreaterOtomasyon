"""Server-persisted progress for long-running phases.

WHY THIS EXISTS
---------------
Analysis and render run for minutes. Until now the console showed a spinner and
a job status, which answers "is it alive?" but not "how far along, doing what,
and how much longer". Worse, a browser refresh lost even that.

So progress is written to disk by the code that is actually doing the work, and
the browser reads it back. A refresh, a new tab, or a server restart all show the
same state, because the state was never in the browser.

HONESTY RULE
------------
A bar is shown as a real percentage ONLY when something countable is being
counted: stages finished out of a known stage list, or items finished out of a
known item count. Everything else renders as an indeterminate bar. Nothing here
interpolates, extrapolates, or eases a number towards 100 to look busy -- a fake
percentage is worse than no percentage, because the user plans around it.

`determinate=False` is therefore a normal, expected outcome, not a degraded one.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

PROGRESS_DIRNAME = "progress"

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_INTERRUPTED = "interrupted"
#: Not running and not broken -- stopped on purpose, waiting for a human.
STATE_STOPPED = "stopped"
STATES = (STATE_IDLE, STATE_RUNNING, STATE_COMPLETED, STATE_FAILED,
          STATE_INTERRUPTED, STATE_STOPPED)

#: A record whose `updated_at` is older than this while still `running` is
#: reported as stale, so a crashed worker cannot leave a bar spinning forever.
STALE_AFTER_SEC = 180


class OperationCancelled(RuntimeError):
    """Raised only at explicit cooperative cancellation checkpoints."""


def _stages(*pairs):
    return tuple({"key": key, "label": label} for key, label in pairs)


#: Every user-facing activity that can take long enough to need a bar. `tab` is
#: the console screen the panel belongs to -- exactly one panel per screen.
ACTIVITIES = {
    "upload": {
        "label": "Kaynak video yukleme", "tab": "phase0",
        "stages": _stages(("receive", "Dosya aliniyor"),
                          ("store", "Depolamaya yaziliyor"),
                          ("fingerprint", "Parmak izi hesaplaniyor")),
    },
    "source_assessment": {
        "label": "Faz 0 kaynak degerlendirme", "tab": "phase0",
        "stages": _stages(("probe", "Medya inceleniyor"),
                          ("scoring", "Mod skorlari hesaplaniyor"),
                          ("write", "Degerlendirme yaziliyor")),
    },
    "video_analysis": {
        "label": "Video analizi", "tab": "moments",
        "stages": _stages(("preparation", "Hazirlik ve klip cikarma"),
                          ("audio", "Ses akisi ayikliniyor"),
                          ("whisper", "Whisper transkript"),
                          ("scenes", "Sahne kesitleri"),
                          ("scoring", "Moment skorlama"),
                          ("previews", "Onizleme klipleri")),
    },
    "script_generation": {
        "label": "Script uretimi", "tab": "script",
        "stages": _stages(("preparation", "Girdi dogrulama"),
                          ("request", "Gemini yaniti bekleniyor"),
                          ("parse", "Yanit ayristiriliyor"),
                          ("write", "Script yaziliyor")),
    },
    "tts": {
        "label": "Seslendirme", "tab": "voice",
        "stages": _stages(("preparation", "Script hazirlaniyor"),
                          ("synthesize", "Ses sentezleniyor"),
                          ("validate", "Ses dogrulaniyor"),
                          ("write", "Ses yaziliyor")),
    },
    "render": {
        "label": "Render", "tab": "render",
        "stages": _stages(("preparation", "Hazirlik ve klip cikarma"),
                          ("subtitles", "Altyazi uretimi"),
                          ("ffmpeg", "FFmpeg kodlama"),
                          ("validation", "Teknik dogrulama"),
                          ("manifest", "Manifest yaziliyor")),
    },
    "diagnostics": {
        "label": "Yetenek teshisi", "tab": "settings",
        "stages": None,       # one stage per capability, built at runtime
    },
    "automation": {
        "label": "Otomatik akis", "tab": "jobs",
        "stages": None,       # driven by the workflow map
    },
}

ACTIVITY_KEYS = tuple(ACTIVITIES)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


class ProgressTracker:
    """Writes one activity's progress. Never raises into the pipeline.

    A tracker that cannot write must not take the phase down with it: progress
    is an observability feature, and losing the bar is strictly better than
    losing the render. Failures are swallowed here and only here.
    """

    def __init__(self, storage, ownership, activity: str, *, job_id=None,
                 attempt: int = 1, stages=None, cancel_check=None):
        self.storage = storage
        self.ownership = ownership
        self.activity = activity
        self.job_id = job_id
        self.attempt = int(attempt or 1)
        definition = ACTIVITIES.get(activity) or {}
        self.stages = list(stages if stages is not None else (definition.get("stages") or []))
        self._started = time.time()
        self._record = None
        self._cancel_check = cancel_check

    def raise_if_cancelled(self) -> None:
        """Stop the progress record and abort at a safe pipeline boundary."""
        if self._cancel_check is not None and self._cancel_check():
            self.stop("kullanici_istegi", message="Kullanici tarafindan durduruldu.")
            raise OperationCancelled("is kullanici tarafindan durduruldu")

    # -- storage ---------------------------------------------------------
    def _path(self) -> Path:
        return self.storage.resolve(self.ownership, PROGRESS_DIRNAME,
                                     f"{self.activity}.json", create_parent=True)

    def _write(self, record) -> None:
        from .storage import atomic_write_json

        try:
            atomic_write_json(self._path(), record)
        except Exception:  # noqa: BLE001 - see class docstring
            pass
        self._record = record

    def attach(self) -> bool:
        """Adopt the record already on disk, if any.

        Lets a second writer continue ONE run instead of restarting its clock,
        which is what keeps `elapsed_sec` honest across a phase/job boundary.
        """
        try:
            path = self._path()
            if not path.is_file():
                return False
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return False
        if not isinstance(record, dict):
            return False
        started = _parse(record.get("started_at"))
        if started:
            self._started = time.time() - (
                datetime.now(timezone.utc) - started).total_seconds()
        self._record = record
        return True

    # -- lifecycle -------------------------------------------------------
    def start(self, message: str = "") -> None:
        self._started = time.time()
        # A retry number recorded for THIS job by whoever queued it wins: the
        # phase itself has no idea which attempt it is.
        try:
            path = self._path()
            if path.is_file():
                previous = json.loads(path.read_text(encoding="utf-8"))
                if (isinstance(previous, dict)
                        and previous.get("job_id") == self.job_id
                        and int(previous.get("attempt") or 1) > self.attempt):
                    self.attempt = int(previous["attempt"])
        except Exception:  # noqa: BLE001
            pass
        self._write({
            "schema_version": 1,
            "artifact_type": "phase_progress",
            "activity": self.activity,
            "label": (ACTIVITIES.get(self.activity) or {}).get("label", self.activity),
            "tab": (ACTIVITIES.get(self.activity) or {}).get("tab", "jobs"),
            "job_id": self.job_id,
            "attempt": self.attempt,
            "is_retry": self.attempt > 1,
            "state": STATE_RUNNING,
            "stages": [{**stage, "state": "waiting"} for stage in self.stages],
            "step": None, "step_label": "", "step_index": 0,
            "step_total": len(self.stages),
            "completed": 0, "total": len(self.stages) or None,
            "unit": "asama" if self.stages else "",
            "item_completed": None, "item_total": None, "item_unit": "",
            "percent": 0 if self.stages else None,
            "determinate": bool(self.stages),
            "message": message,
            "started_at": _now(), "updated_at": _now(), "finished_at": None,
            "elapsed_sec": 0.0,
            "error": "", "failed_step": "", "failed_step_label": "",
        })

    def stage(self, key: str, message: str = "") -> None:
        """Enter a stage. Everything before it is finished, by definition."""
        self.raise_if_cancelled()
        record = self._record or {}
        if not record:
            self.start()
            record = self._record
        index = next((i for i, s in enumerate(self.stages) if s["key"] == key), None)
        stages = []
        for position, stage in enumerate(record.get("stages") or []):
            if index is None:
                state = stage.get("state", "waiting")
            elif position < index:
                state = "completed"
            elif position == index:
                state = "running"
            else:
                state = "waiting"
            stages.append({**stage, "state": state})
        label = next((s["label"] for s in self.stages if s["key"] == key), key)
        completed = index if index is not None else record.get("completed", 0)
        total = len(self.stages) or None
        record.update({
            "step": key, "step_label": label,
            "step_index": (index + 1) if index is not None else 0,
            "step_total": len(self.stages),
            "stages": stages,
            "completed": completed, "total": total,
            "unit": "asama" if total else "",
            # A new stage has no inner measurement until it declares one.
            "item_completed": None, "item_total": None, "item_unit": "",
            "percent": round(100 * completed / total) if total else None,
            "determinate": bool(total),
            "message": message or label,
            "updated_at": _now(),
            "elapsed_sec": round(time.time() - self._started, 1),
        })
        self._write(record)

    def count(self, completed: int, total: int, *, unit: str = "parca",
              message: str = "") -> None:
        """Report a MEASURED inner count for the current stage.

        Only call this with numbers that were actually counted. There is no
        variant that estimates.
        """
        self.raise_if_cancelled()
        record = self._record or {}
        if not record:
            return
        record.update({
            "item_completed": int(completed), "item_total": int(total) or None,
            "item_unit": unit,
            "message": message or record.get("message", ""),
            "updated_at": _now(),
            "elapsed_sec": round(time.time() - self._started, 1),
        })
        self._write(record)

    def note(self, message: str) -> None:
        record = self._record or {}
        if not record:
            return
        record.update({"message": message, "updated_at": _now(),
                       "elapsed_sec": round(time.time() - self._started, 1)})
        self._write(record)

    def finish(self, *, message: str = "") -> None:
        self.raise_if_cancelled()
        record = self._record or {}
        if not record:
            self.start()
            record = self._record
        total = len(self.stages) or None
        record.update({
            "state": STATE_COMPLETED,
            "stages": [{**s, "state": "completed"} for s in record.get("stages") or []],
            "completed": total or record.get("completed", 0), "total": total,
            "item_completed": None, "item_total": None,
            "percent": 100 if total else None,
            "determinate": bool(total),
            "step_index": len(self.stages),
            "message": message or "Tamamlandi.",
            "updated_at": _now(), "finished_at": _now(),
            "elapsed_sec": round(time.time() - self._started, 1),
        })
        self._write(record)

    def stop(self, reason: str, *, message: str = "") -> None:
        """Stopped deliberately -- a human gate, a pause, a cancel.

        Separate from `fail` on purpose: painting "waiting for your approval"
        red would teach the user to ignore red.
        """
        record = self._record or {}
        if not record:
            self.start()
            record = self._record
        record.update({
            "state": STATE_STOPPED,
            "stages": [{**s, "state": "waiting" if s.get("state") == "running"
                        else s.get("state", "waiting")}
                       for s in record.get("stages") or []],
            "message": message or "Durduruldu.",
            "blocker": str(reason)[:200],
            "updated_at": _now(), "finished_at": _now(),
            "elapsed_sec": round(time.time() - self._started, 1),
        })
        self._write(record)

    def fail(self, error: str, *, message: str = "") -> None:
        record = self._record or {}
        if not record:
            self.start()
            record = self._record
        record.update({
            "state": STATE_FAILED,
            "stages": [{**s, "state": "failed" if s.get("state") == "running"
                        else s.get("state", "waiting")}
                       for s in record.get("stages") or []],
            "message": message or "Faz basarisiz oldu.",
            "error": str(error)[:600],
            "failed_step": record.get("step") or "",
            "failed_step_label": record.get("step_label") or "",
            "updated_at": _now(), "finished_at": _now(),
            "elapsed_sec": round(time.time() - self._started, 1),
        })
        self._write(record)


class NullTracker:
    """Same surface, writes nothing. Lets a caller stay unconditional."""

    def start(self, message: str = "") -> None: ...
    def stage(self, key: str, message: str = "") -> None: ...
    def count(self, completed, total, *, unit="", message="") -> None: ...
    def note(self, message: str) -> None: ...
    def attach(self) -> bool: return False
    def finish(self, *, message: str = "") -> None: ...
    def stop(self, reason: str, *, message: str = "") -> None: ...
    def fail(self, error: str, *, message: str = "") -> None: ...
    def raise_if_cancelled(self) -> None: ...


def load_progress(storage, ownership) -> dict:
    """Every activity's last known progress, keyed by activity.

    Re-derives `elapsed_sec` for running records so the panel keeps ticking
    without the writer having to touch the file every second, and marks records
    stale when their writer stopped updating (a crashed worker).
    """
    records = {}
    for activity in ACTIVITY_KEYS:
        try:
            path = storage.resolve(ownership, PROGRESS_DIRNAME, f"{activity}.json")
            if not path.is_file():
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a corrupt record must not break the page
            continue
        if not isinstance(record, dict):
            continue
        if record.get("state") == STATE_RUNNING:
            started = _parse(record.get("started_at"))
            updated = _parse(record.get("updated_at"))
            now = datetime.now(timezone.utc)
            if started:
                record["elapsed_sec"] = round((now - started).total_seconds(), 1)
            if updated and (now - updated).total_seconds() > STALE_AFTER_SEC:
                record["stale"] = True
                record["message"] = (record.get("message") or "") + \
                    " (guncelleme durdu; is kurtarilmis olabilir)"
        records[activity] = record
    return records


def mark_interrupted(storage, ownership, activity: str, *, reason: str = "") -> None:
    """A running record whose worker died. Called by job recovery.

    Left as `running` it would spin forever; silently deleted it would hide that
    work was lost. `interrupted` says what actually happened.
    """
    from .storage import atomic_write_json

    try:
        path = storage.resolve(ownership, PROGRESS_DIRNAME, f"{activity}.json")
        if not path.is_file():
            return
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("state") != STATE_RUNNING:
            return
        record.update({
            "state": STATE_INTERRUPTED,
            "message": "Calisma yarida kesildi (surec sonlandi).",
            "error": reason or "is kurtarma sirasinda calisir durumda bulundu",
            "failed_step": record.get("step") or "",
            "failed_step_label": record.get("step_label") or "",
            "updated_at": _now(), "finished_at": _now(),
        })
        atomic_write_json(path, record)
    except Exception:  # noqa: BLE001
        pass
