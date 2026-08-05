"""Persistent job model, store, and pluggable runner.

WHY THIS EXISTS
---------------
Analysis, script generation, TTS and render all take far longer than an HTTP
request, so the UI submits a JOB and polls it. That means the job state -- not
browser memory -- is the source of truth, and it must survive a page refresh, a
backend restart and a worker crash.

Guarantees implemented here:

  idempotency   A submission carries an `idempotency_key` derived from
                (project, job_type, inputs). Submitting the same key while an
                equivalent job is queued/running/finished returns THAT job
                instead of creating a second one, so a double-click or a retried
                POST cannot cause two provider calls.

  single claim  A worker must `claim()` a job before running it. Claiming is a
                compare-and-set under a per-project file lock, so two concurrent
                workers can never both execute the same job.

  lease         A claim carries `lease_expires_at`. A worker that dies leaves an
                expired lease; `recover_abandoned()` then moves the job out of
                `running` into `failed` + `recoverable=True` instead of leaving
                it "running" forever.

  atomic state  Every write goes through `atomic_write_json`, so a crash cannot
                leave a half-written job record that later parses as garbage.

Statuses mirror the project's existing vocabulary and keep the execution/artifact
split: `status` is EXECUTION state; `artifact_status` is whatever the pipeline
wrote (review_required / insufficient_data / failed / approved ...).
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

from .ids import Ownership, new_id, validate_id
from .redaction import scrub_text
from .storage import atomic_write_json

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_REVIEW_REQUIRED = "review_required"
STATUS_FAILED = "failed"
STATUS_COMPLETED = "completed"

JOB_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_REVIEW_REQUIRED,
                STATUS_FAILED, STATUS_COMPLETED)
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)
TERMINAL_STATUSES = (STATUS_REVIEW_REQUIRED, STATUS_FAILED, STATUS_COMPLETED)

#: Job types the console can submit. Each maps to one canonical pipeline entry
#: point in `pipeline_service`; there is no free-form "run arbitrary code" job.
JOB_TYPES = (
    "source_assessment",
    "video_analysis",
    "script_generation",
    "tts",
    "render",
)

JOBS_DIRNAME = "jobs"
DEFAULT_LEASE_SECONDS = 900
MAX_ATTEMPTS = 3


class JobError(RuntimeError):
    pass


class JobNotFound(JobError):
    pass


class JobConflict(JobError):
    """The requested transition is not legal from the job's current state."""


def _now():
    return datetime.now(timezone.utc)


def _iso(moment=None):
    return (moment or _now()).isoformat()


def _parse_iso(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def process_alive(pid, host=None) -> bool:
    """Is `pid` still running ON THIS HOST?

    A pid from another machine tells us nothing, so it is treated as ALIVE --
    refusing to reclaim someone else's job is the safe default. Uses the Win32
    API on Windows because `os.kill(pid, 0)` is not a liveness probe there.
    """
    if pid is None:
        return False
    if host and host != socket.gethostname()[:120]:
        return True  # different machine: never assume it died
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            return bool(ok) and exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    return True


def new_job_record(ownership: Ownership, job_type: str, *, idempotency_key: str,
                   params=None, submitted_by: str = "console") -> dict:
    if job_type not in JOB_TYPES:
        raise JobError(f"bilinmeyen is turu: {job_type!r} (beklenen: {list(JOB_TYPES)})")
    return {
        "schema_version": 1,
        "artifact_type": "console_job",
        "job_id": new_id("job"),
        **ownership.to_dict(),
        "job_type": job_type,
        "status": STATUS_QUEUED,
        "artifact_status": None,
        "idempotency_key": str(idempotency_key),
        "params": dict(params or {}),
        "attempts": 0,
        "max_attempts": MAX_ATTEMPTS,
        "submitted_by": str(submitted_by)[:120],
        "created_at": _iso(),
        "updated_at": _iso(),
        "started_at": None,
        "finished_at": None,
        "claimed_by": None,
        "owner_pid": None,
        "owner_host": None,
        "lease_expires_at": None,
        "error": None,
        "error_kind": None,
        "recoverable": False,
        "retry_of": None,
        "result": None,
        "artifacts": [],
        "not_uploaded": True,
    }


class _ProjectLock:
    """Cross-process lock for one project's job index.

    O_CREAT|O_EXCL works identically on Windows and POSIX and needs no extra
    dependency. A stale lock (crashed holder) is broken after `stale_after`, so a
    crash cannot wedge the project forever.
    """

    def __init__(self, path, timeout=10.0, poll=0.02, stale_after=60.0):
        self.path = path
        self.timeout, self.poll, self.stale_after = timeout, poll, stale_after
        self._fd = None

    def __enter__(self):
        deadline = time.time() + self.timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode("ascii"))
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale_after:
                        self.path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.time() >= deadline:
                    raise JobConflict("is kilidi alinamadi (zaman asimi)")
                time.sleep(self.poll)

    def __exit__(self, *exc_info):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self.path.unlink(missing_ok=True)
        return False


class JobStore:
    """Filesystem-backed job store, one directory per project namespace."""

    def __init__(self, storage, ownership: Ownership, *, lease_seconds: int = DEFAULT_LEASE_SECONDS):
        if not ownership.project_id:
            raise JobError("is deposu proje kimligi olmadan acilamaz")
        self.storage = storage
        self.ownership = ownership
        self.lease_seconds = int(lease_seconds)
        self._thread_lock = threading.RLock()

    # -- paths -------------------------------------------------------------
    def _job_path(self, job_id: str):
        return self.storage.resolve(
            self.ownership, JOBS_DIRNAME, f"{validate_id(job_id, field='job_id')}.json",
            create_parent=True,
        )

    def _lock(self):
        return _ProjectLock(self.storage.resolve(self.ownership, JOBS_DIRNAME, ".index.lock",
                                                  create_parent=True))

    # -- reads -------------------------------------------------------------
    def get(self, job_id: str) -> dict:
        path = self._job_path(job_id)
        if not path.is_file():
            raise JobNotFound(f"is bulunamadi: {job_id}")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise JobError(f"is kaydi okunamadi (bozuk JSON): {job_id}") from exc
        if not isinstance(record, dict) or record.get("job_id") != job_id:
            raise JobError(f"is kaydi tutarsiz: {job_id}")
        return record

    def list(self, *, statuses=None, job_type=None, limit: int | None = None) -> list:
        directory = self.storage.resolve(self.ownership, JOBS_DIRNAME)
        if not directory.is_dir():
            return []
        records = []
        for path in directory.glob("job_*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                # A corrupt job file is surfaced as a corrupt job, never skipped
                # silently and never treated as success.
                records.append({
                    "job_id": path.stem, "status": STATUS_FAILED,
                    "job_type": "unknown", "corrupt": True,
                    "error": "is kaydi okunamadi (bozuk JSON); yeniden calistirilabilir",
                    "recoverable": True, **self.ownership.to_dict(),
                })
                continue
            records.append(record)
        if statuses:
            records = [r for r in records if r.get("status") in statuses]
        if job_type:
            records = [r for r in records if r.get("job_type") == job_type]
        records.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return records[:limit] if limit else records

    # -- writes ------------------------------------------------------------
    def _save(self, record: dict) -> dict:
        record["updated_at"] = _iso()
        atomic_write_json(self._job_path(record["job_id"]), record)
        return record

    def submit(self, job_type: str, *, idempotency_key: str, params=None,
               submitted_by: str = "console") -> tuple:
        """Create a job, or return the existing one for this idempotency key.

        Returns `(record, created)`. `created=False` means the caller's request
        was a duplicate and NO new work was scheduled -- which is what makes a
        double-clicked button harmless.
        """
        with self._thread_lock, self._lock():
            for existing in self.list():
                if existing.get("idempotency_key") != idempotency_key:
                    continue
                if existing.get("status") in ACTIVE_STATUSES:
                    return existing, False
                if existing.get("status") in (STATUS_COMPLETED, STATUS_REVIEW_REQUIRED):
                    # Re-running a finished job with identical inputs would repeat
                    # provider calls for a result we already have.
                    return existing, False
            record = new_job_record(self.ownership, job_type,
                                     idempotency_key=idempotency_key, params=params,
                                     submitted_by=submitted_by)
            return self._save(record), True

    def claim(self, job_id: str, worker_id: str) -> dict:
        """Atomically move queued -> running for exactly one worker."""
        with self._thread_lock, self._lock():
            record = self.get(job_id)
            status = record.get("status")
            lease = _parse_iso(record.get("lease_expires_at"))
            if status == STATUS_RUNNING and lease and lease > _now():
                raise JobConflict(
                    f"is zaten calisiyor (claimed_by={record.get('claimed_by')}); "
                    "ikinci bir worker ayni isi calistiramaz"
                )
            if status not in (STATUS_QUEUED, STATUS_RUNNING):
                raise JobConflict(f"is '{status}' durumundayken claim edilemez")
            record.update({
                "status": STATUS_RUNNING,
                "claimed_by": str(worker_id)[:120],
                # Recorded so a restarting server can tell "my own dead thread"
                # from "another machine's live worker" instead of guessing.
                "owner_pid": os.getpid(),
                "owner_host": socket.gethostname()[:120],
                "started_at": record.get("started_at") or _iso(),
                "lease_expires_at": _iso(_now() + timedelta(seconds=self.lease_seconds)),
                "attempts": int(record.get("attempts") or 0) + 1,
                "error": None,
                "error_kind": None,
                "recoverable": False,
            })
            return self._save(record)

    def heartbeat(self, job_id: str, worker_id: str) -> dict:
        with self._thread_lock:
            record = self.get(job_id)
            if record.get("claimed_by") != worker_id:
                raise JobConflict("bu isi baska bir worker tutuyor")
            record["lease_expires_at"] = _iso(_now() + timedelta(seconds=self.lease_seconds))
            return self._save(record)

    def finish(self, job_id: str, status: str, *, artifact_status=None, result=None,
               artifacts=None, error=None, error_kind=None, recoverable: bool = False) -> dict:
        if status not in TERMINAL_STATUSES:
            raise JobError(f"gecersiz bitis durumu: {status!r}")
        with self._thread_lock, self._lock():
            record = self.get(job_id)
            record.update({
                "status": status,
                "artifact_status": artifact_status,
                "result": result,
                "artifacts": list(artifacts or []),
                "finished_at": _iso(),
                "claimed_by": None,
                "owner_pid": None,
                "owner_host": None,
                "lease_expires_at": None,
                # Sanitized here as well as at the raise site: an error string is
                # the most likely place for a credential to escape.
                "error": scrub_text(error) if error else None,
                "error_kind": error_kind,
                "recoverable": bool(recoverable),
            })
            return self._save(record)

    def request_retry(self, job_id: str, *, requested_by: str = "console") -> dict:
        """Safe retry: only a terminal, non-successful job may be requeued."""
        with self._thread_lock, self._lock():
            record = self.get(job_id)
            status = record.get("status")
            if status in ACTIVE_STATUSES:
                raise JobConflict(f"is halen '{status}'; once bitmesini bekle")
            if status in (STATUS_COMPLETED, STATUS_REVIEW_REQUIRED):
                raise JobConflict(
                    f"is zaten '{status}' ile bitti; ayni girdilerle tekrar calistirmak "
                    "saglayici cagrisini bosuna tekrarlar"
                )
            if int(record.get("attempts") or 0) >= int(record.get("max_attempts") or MAX_ATTEMPTS):
                raise JobConflict(
                    f"deneme limiti asildi ({record.get('attempts')}/{record.get('max_attempts')})"
                )
            record.update({
                "status": STATUS_QUEUED,
                "error": None,
                "error_kind": None,
                "recoverable": False,
                "finished_at": None,
                "claimed_by": None,
                "lease_expires_at": None,
                "retry_of": record.get("retry_of") or record["job_id"],
                "submitted_by": str(requested_by)[:120],
            })
            return self._save(record)

    def recover_abandoned(self, *, ignore_lease: bool = False,
                          reclaim_dead_owners: bool = True) -> list:
        """Move jobs whose worker is genuinely gone out of `running`.

        A job is reclaimed when EITHER:
          * its lease has expired (the worker stopped heartbeating), or
          * `reclaim_dead_owners` and its recorded owner process is provably dead
            ON THIS HOST (a server restart kills its in-process thread workers,
            and their leases may still look fresh).

        A job whose owner process is ALIVE is never touched, however old the
        lease looks -- that is what makes an external `creator_console.worker`
        safe to run alongside the HTTP server. `ignore_lease=True` is retained
        only for tests that need the old blanket behaviour.

        Leaving a job stuck in `running` forever is the worst outcome: the UI
        spins on work nobody is doing and the user cannot retry.
        """
        recovered = []
        with self._thread_lock:
            for record in self.list(statuses=(STATUS_RUNNING,)):
                lease = _parse_iso(record.get("lease_expires_at"))
                lease_live = bool(lease and lease > _now())
                owner_pid = record.get("owner_pid")
                owner_host = record.get("owner_host")
                owner_live = process_alive(owner_pid, owner_host) if owner_pid else False

                if ignore_lease:
                    reason = "Sunucu yeniden baslatildi; bu isi yuruten surec artik yok."
                elif owner_pid and not owner_live and reclaim_dead_owners:
                    reason = (f"Isi yuruten surec (pid={owner_pid}) sonlanmis; "
                              "lease taze gorunse bile is sahipsiz.")
                elif not lease_live:
                    reason = "Isi yuruten surec beklenmedik sekilde durdu (lease suresi doldu)."
                else:
                    continue  # healthy: live owner and/or live lease

                attempts = int(record.get("attempts") or 0)
                max_attempts = int(record.get("max_attempts") or MAX_ATTEMPTS)
                recovered.append(self.finish(
                    record["job_id"], STATUS_FAILED,
                    error=(f"{reason} Durum kalici olarak kaydedildi; "
                           "is guvenle yeniden calistirilabilir."),
                    error_kind="worker_lost",
                    recoverable=attempts < max_attempts,
                ))
        return recovered


class JobRunner:
    """Interface a background execution backend must satisfy.

    Pre-alpha ships `ThreadJobRunner`; a process/queue-based worker can replace it
    without touching the API layer, which is the whole point of the split.
    """

    def submit(self, store: JobStore, job_id: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def shutdown(self, timeout: float = 5.0) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class ThreadJobRunner(JobRunner):
    """In-process thread worker: real concurrency, no external dependency."""

    def __init__(self, execute, *, worker_id: str | None = None, max_workers: int = 2):
        self._execute = execute
        self.worker_id = worker_id or f"worker-{os.getpid()}"
        self._threads: list = []
        self._semaphore = threading.BoundedSemaphore(max_workers)
        self._lock = threading.Lock()

    def submit(self, store: JobStore, job_id: str) -> None:
        thread = threading.Thread(target=self._run, args=(store, job_id),
                                  name=f"console-job-{job_id}", daemon=True)
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
        thread.start()

    def _run(self, store: JobStore, job_id: str) -> None:
        with self._semaphore:
            try:
                store.claim(job_id, self.worker_id)
            except JobConflict:
                return  # another worker already owns it; not an error
            except JobError:
                return
            try:
                self._execute(store, job_id, self.worker_id)
            except Exception as exc:  # noqa: BLE001 - a worker must never die silently
                try:
                    store.finish(
                        job_id, STATUS_FAILED,
                        error=f"{type(exc).__name__}: {exc}",
                        error_kind="unhandled_worker_error", recoverable=True,
                    )
                except JobError:
                    pass

    def wait(self, timeout: float = 30.0) -> None:
        """Test helper: block until in-flight jobs finish."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                alive = [t for t in self._threads if t.is_alive()]
            if not alive:
                return
            time.sleep(0.02)

    def shutdown(self, timeout: float = 5.0) -> None:
        self.wait(timeout)


class InlineJobRunner(JobRunner):
    """Deterministic runner: executes synchronously on submit.

    Used by the test suite so a job's outcome is observable without sleeping,
    and by `--inline` mode when someone wants a single-threaded console.
    """

    def __init__(self, execute, *, worker_id: str = "inline"):
        self._execute = execute
        self.worker_id = worker_id

    def submit(self, store: JobStore, job_id: str) -> None:
        try:
            store.claim(store.get(job_id)["job_id"], self.worker_id)
        except JobConflict:
            return
        try:
            self._execute(store, job_id, self.worker_id)
        except Exception as exc:  # noqa: BLE001
            store.finish(job_id, STATUS_FAILED, error=f"{type(exc).__name__}: {exc}",
                         error_kind="unhandled_worker_error", recoverable=True)

    def shutdown(self, timeout: float = 5.0) -> None:
        return None
