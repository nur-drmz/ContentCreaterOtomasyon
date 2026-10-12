"""Deterministic storage failure injection (test-only).

WHY THIS EXISTS
---------------
"The disk is fine" is an assumption, not a fact. A console that writes job
records, manifests and audit events must behave sanely when the filesystem says
no: a full disk, a permission-denied artifact, a slow network share, or a write
that dies halfway through.

`FaultingStorage` wraps a real `StorageAdapter` and fails on demand, so those
paths can be exercised deterministically instead of hoped about:

    storage = FaultingStorage(real)
    storage.fail_on("results/render/render_manifest.json", DiskFull())

Nothing here is imported by the running console -- it exists for
`verify_faults.py`. Faults are matched by namespace-relative path SUFFIX, so a
test can target one artifact without knowing the absolute layout.
"""
from __future__ import annotations

import errno
import time
from pathlib import Path

from .storage import StorageAdapter, StorageViolation


def disk_full(path="") -> OSError:
    """ENOSPC, exactly as the OS raises it."""
    error = OSError(errno.ENOSPC, "No space left on device", str(path))
    error.errno = errno.ENOSPC
    return error


def permission_denied(path="") -> PermissionError:
    error = PermissionError(errno.EACCES, "Permission denied", str(path))
    error.errno = errno.EACCES
    return error


def io_error(path="") -> OSError:
    error = OSError(errno.EIO, "Input/output error", str(path))
    error.errno = errno.EIO
    return error


class InterruptedWrite(Exception):
    """Raised AFTER a partial payload has hit the disk.

    This is the nastiest of the four: unlike a rejected write, it leaves bytes
    behind. It exists to prove that a reader of that artifact reports corruption
    rather than parsing whatever survived.
    """


class FaultingStorage(StorageAdapter):
    """Storage decorator with per-path, per-count fault injection."""

    def __init__(self, inner):
        self.inner = inner
        self._faults: list = []
        self.calls: list = []
        self.slow_seconds = 0.0
        self.slow_suffix = None

    # -- fault programming -------------------------------------------------
    def fail_on(self, suffix: str, exception, *, times: int = 1, mode: str = "write"):
        """Fail the next `times` operations whose path ends with `suffix`.

        `mode`: 'write' (write_json/write_bytes), 'read' (read_json), or 'any'.
        """
        self._faults.append({"suffix": suffix.replace("\\", "/"), "exception": exception,
                             "remaining": int(times), "mode": mode})
        return self

    def truncate_on(self, suffix: str, keep_bytes: int = 40, *, times: int = 1):
        """Write only the first `keep_bytes`, then raise InterruptedWrite."""
        self._faults.append({"suffix": suffix.replace("\\", "/"),
                             "exception": InterruptedWrite(f"yazim {keep_bytes} baytta kesildi"),
                             "remaining": int(times), "mode": "write",
                             "partial_bytes": int(keep_bytes)})
        return self

    def slow(self, seconds: float, suffix: str | None = None):
        self.slow_seconds = float(seconds)
        self.slow_suffix = suffix.replace("\\", "/") if suffix else None
        return self

    def clear(self):
        self._faults.clear()
        self.slow_seconds = 0.0
        self.slow_suffix = None
        return self

    # -- helpers -----------------------------------------------------------
    def _match(self, path, mode):
        text = str(path).replace("\\", "/")
        for fault in self._faults:
            if fault["remaining"] <= 0:
                continue
            if fault["mode"] not in (mode, "any"):
                continue
            if text.endswith(fault["suffix"]):
                fault["remaining"] -= 1
                return fault
        return None

    def _maybe_slow(self, path):
        if not self.slow_seconds:
            return
        text = str(path).replace("\\", "/")
        if self.slow_suffix and not text.endswith(self.slow_suffix):
            return
        time.sleep(self.slow_seconds)

    # -- StorageAdapter surface (delegating) -------------------------------
    def __getattr__(self, name):
        # Anything not overridden below goes straight to the real adapter, so
        # this decorator never silently drops behaviour as storage.py grows.
        return getattr(self.inner, name)

    def namespace_root(self, ownership):
        return self.inner.namespace_root(ownership)

    def resolve(self, ownership, *parts, **kwargs):
        return self.inner.resolve(ownership, *parts, **kwargs)

    def write_json(self, ownership, relative, data):
        self.calls.append(("write_json", str(relative)))
        self._maybe_slow(relative)
        fault = self._match(relative, "write")
        if fault:
            if "partial_bytes" in fault:
                # Land a partial payload first: the whole point is that bytes
                # survive on disk and a later read must reject them.
                target = self.inner.resolve(ownership, relative, create_parent=True)
                import json as _json

                blob = (_json.dumps(data, ensure_ascii=False, indent=2, default=str)
                        ).encode("utf-8")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(blob[: fault["partial_bytes"]])
            raise fault["exception"]
        return self.inner.write_json(ownership, relative, data)

    def write_bytes(self, ownership, relative, payload):
        self.calls.append(("write_bytes", str(relative)))
        self._maybe_slow(relative)
        fault = self._match(relative, "write")
        if fault:
            if "partial_bytes" in fault:
                target = self.inner.resolve(ownership, relative, create_parent=True)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bytes(payload)[: fault["partial_bytes"]])
            raise fault["exception"]
        return self.inner.write_bytes(ownership, relative, payload)

    def read_json(self, ownership, relative, **kwargs):
        self.calls.append(("read_json", str(relative)))
        self._maybe_slow(relative)
        fault = self._match(relative, "read")
        if fault:
            raise fault["exception"]
        return self.inner.read_json(ownership, relative, **kwargs)


#: Modules that did `from creator_console.storage import atomic_write_json` (or
#: the relative equivalent). Rebinding only the definition in `storage` would
#: NOT affect them -- the name was already bound at import time -- so every
#: holder must be patched. Miss one and the fault silently does not fire, which
#: is worse than no test at all.
_ATOMIC_WRITE_HOLDERS = (
    "creator_console.storage",
    "creator_console.jobs",
    "creator_console.pipeline_service",
    # Artifact writes moved here in the EditorService extraction; without this
    # entry `write_artifact` would bypass injected write faults entirely.
    "infrastructure.persistence.local.artifact_repository",
    # Revision-manifest writes moved here in the MomentService extraction
    # (`LocalRevisionArchive`). Without this entry, archiving a clip-bounds or
    # rewind snapshot could succeed while the fault injector believes it
    # failed -- and vice versa.
    "infrastructure.persistence.local.revision_archive",
)


def patch_atomic_write(*, exception=None, partial_bytes=None, match=None):
    """Context manager that makes `atomic_write_json` fail everywhere it is used.

    This is the fault path for writes BELOW the storage adapter: job records,
    audit events and artifacts all call the module-level atomic writer directly,
    so `FaultingStorage` never sees them.

    `partial_bytes` lands a truncated payload on disk BEFORE raising, which is
    how a genuinely interrupted write behaves -- bytes survive, and a later read
    must reject them rather than parse the fragment.
    """
    import importlib
    from contextlib import contextmanager

    @contextmanager
    def _patch():
        modules = []
        for name in _ATOMIC_WRITE_HOLDERS:
            module = importlib.import_module(name)
            if hasattr(module, "atomic_write_json"):
                modules.append((module, module.atomic_write_json))
        original = modules[0][1] if modules else None

        def faulty(path, data):
            text = str(path).replace("\\", "/")
            if match is None or text.endswith(match):
                if partial_bytes is not None:
                    import json as _json

                    blob = (_json.dumps(data, ensure_ascii=False, indent=2,
                                        default=str)).encode("utf-8")
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    Path(path).write_bytes(blob[:partial_bytes])
                raise exception or disk_full(path)
            return original(path, data)

        for module, _ in modules:
            module.atomic_write_json = faulty
        try:
            yield
        finally:
            for module, previous in modules:
                module.atomic_write_json = previous

    return _patch()
