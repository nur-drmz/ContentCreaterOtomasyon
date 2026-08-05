"""Storage adapter with hard namespace isolation.

WHY THIS EXISTS
---------------
Everything the console writes -- source videos, artifacts, job records, audit
events, rendered Shorts -- lives under one namespace:

    <root>/workspaces/<workspace_id>/projects/<project_id>/...

`LocalFilesystemStorage.resolve()` is the ONLY way to turn a relative name into a
real path, and it refuses to return anything outside that namespace. Traversal
(`../`), absolute paths, drive letters, UNC paths, symlink escapes and unsafe
filenames are rejected rather than sanitized-and-accepted, so a bug in a caller
surfaces as a loud error instead of a silent cross-workspace read.

The adapter is an interface on purpose: swapping local/Drive storage for object
storage later means implementing `StorageAdapter`, not rewriting call sites.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import threading
import time
from pathlib import Path, PurePosixPath

from .ids import Ownership, safe_filename, validate_id


class StorageViolation(RuntimeError):
    """A path escaped, or tried to escape, its assigned namespace root."""


class StorageAdapter:
    """Interface every storage backend must satisfy."""

    def namespace_root(self, ownership: Ownership) -> Path:  # pragma: no cover - interface
        raise NotImplementedError

    def resolve(self, ownership: Ownership, *parts) -> Path:  # pragma: no cover - interface
        raise NotImplementedError

    def write_json(self, ownership: Ownership, relative, data) -> Path:  # pragma: no cover
        raise NotImplementedError

    def read_json(self, ownership: Ownership, relative):  # pragma: no cover - interface
        raise NotImplementedError


#: Windows fails an otherwise-valid `os.replace` with PermissionError while ANY
#: other handle is open on the destination -- and `Path.read_text` does not open
#: with FILE_SHARE_DELETE, so a reader makes the swap fail. That is transient
#: contention, not corruption, so it is retried with jittered backoff.
#:
#: If the budget is exhausted the error is RAISED, never swallowed: a caller must
#: never be told "written" when the swap did not happen. Integrity is unaffected
#: either way -- a reader sees either the old document or the new one, never a
#: partial one, which is the guarantee this function exists to provide.
_REPLACE_ATTEMPTS = 25
_REPLACE_BACKOFF_SEC = 0.01
_REPLACE_BACKOFF_CAP_SEC = 0.25


def atomic_write_bytes(path: Path, payload: bytes) -> Path:
    """Write via a same-directory temp file + os.replace.

    Atomic on both POSIX and Windows for same-filesystem replaces, so a crash (or
    a concurrent reader) can never observe a half-written manifest -- which is
    exactly how a "completed" job ends up pointing at truncated JSON.

    The temp file carries the thread id as well as the pid: two threads writing
    the same artifact concurrently must not share (and then delete) one temp file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}.{threading.get_ident()}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        last_error = None
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                last_error = None
                break
            except PermissionError as exc:  # Windows reader holding the target
                last_error = exc
                delay = min(_REPLACE_BACKOFF_SEC * (2 ** attempt), _REPLACE_BACKOFF_CAP_SEC)
                # Jitter: without it several writers retry in lockstep and keep
                # colliding with the same reader window.
                time.sleep(delay * (0.5 + random.random() / 2))
        if last_error is not None:
            raise last_error
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return path


def atomic_write_json(path, data) -> Path:
    return atomic_write_bytes(
        Path(path), (json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8")
    )


class LocalFilesystemStorage(StorageAdapter):
    """Local/Drive-backed storage with per-workspace/per-project namespaces."""

    WORKSPACES_DIR = "workspaces"
    PROJECTS_DIR = "projects"

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # -- namespaces --------------------------------------------------------
    def workspace_root(self, ownership: Ownership) -> Path:
        return self.root / self.WORKSPACES_DIR / ownership.workspace_id

    def namespace_root(self, ownership: Ownership) -> Path:
        """The deepest namespace this ownership may touch."""
        base = self.workspace_root(ownership)
        if ownership.project_id:
            base = base / self.PROJECTS_DIR / ownership.project_id
        return base

    def ensure_namespace(self, ownership: Ownership) -> Path:
        root = self.namespace_root(ownership)
        root.mkdir(parents=True, exist_ok=True)
        return root

    # -- the single safe path builder --------------------------------------
    def resolve(self, ownership: Ownership, *parts, create_parent: bool = False) -> Path:
        """Resolve `parts` inside the ownership namespace, or raise.

        `parts` are treated as RELATIVE segments even if they look absolute --
        an absolute path is a violation, never an override.
        """
        base = self.namespace_root(ownership).resolve()
        segments: list[str] = []
        for part in parts:
            if part is None:
                continue
            text = str(part)
            if not text:
                continue
            if _looks_absolute(text):
                raise StorageViolation(
                    f"mutlak yol reddedildi: {text!r}; depolama yalnizca namespace icinde calisir"
                )
            for raw in PurePosixPath(text.replace("\\", "/")).parts:
                if raw in ("", "."):
                    continue
                if raw == "..":
                    raise StorageViolation(f"yol gezinme denemesi reddedildi: {text!r}")
                segments.append(safe_filename(raw))
        if not segments:
            return base

        candidate = base.joinpath(*segments)
        # Resolve against symlinks too: a symlink inside the namespace must not be
        # able to point outside it.
        resolved = Path(os.path.realpath(candidate))
        real_base = Path(os.path.realpath(base))
        if resolved != real_base and real_base not in resolved.parents:
            raise StorageViolation(
                f"cozulen yol namespace disina cikti: {resolved} (kok: {real_base})"
            )
        if create_parent:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    # -- convenience -------------------------------------------------------
    def write_json(self, ownership: Ownership, relative, data) -> Path:
        return atomic_write_json(self.resolve(ownership, relative, create_parent=True), data)

    def read_json(self, ownership: Ownership, relative, *, default=None):
        path = self.resolve(ownership, relative)
        if not path.is_file():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            # A corrupt artifact is reported by the caller as corrupt; it is NOT
            # silently replaced with a plausible-looking default.
            raise

    def write_bytes(self, ownership: Ownership, relative, payload: bytes) -> Path:
        return atomic_write_bytes(self.resolve(ownership, relative, create_parent=True), payload)

    def exists(self, ownership: Ownership, relative) -> bool:
        try:
            return self.resolve(ownership, relative).exists()
        except StorageViolation:
            return False

    def list_dir(self, ownership: Ownership, relative="") -> list:
        target = self.resolve(ownership, relative) if relative else self.namespace_root(ownership)
        if not target.is_dir():
            return []
        return sorted(p.name for p in target.iterdir())

    def relative_to_namespace(self, ownership: Ownership, path) -> str:
        """Namespace-relative POSIX string, for artifacts and API responses.

        Absolute host paths are deliberately NOT returned to the browser: they
        leak the operator's directory layout and are useless to the client.
        """
        resolved = Path(os.path.realpath(Path(path)))
        base = Path(os.path.realpath(self.namespace_root(ownership)))
        try:
            return resolved.relative_to(base).as_posix()
        except ValueError as exc:
            raise StorageViolation(f"{path} bu namespace'e ait degil") from exc

    def delete_namespace(self, ownership: Ownership) -> None:
        """Only ever used by tests/fixtures; refuses to delete above a project."""
        if not ownership.project_id:
            raise StorageViolation("proje kimligi olmadan namespace silinemez")
        shutil.rmtree(self.namespace_root(ownership), ignore_errors=True)

    def trash_namespace(self, ownership: Ownership) -> str:
        """Atomically move one exact project namespace into workspace trash.

        The UI calls this "delete", but the bytes stay recoverable.  The target
        is derived only from validated ownership components; callers cannot
        supply a path, glob or parent directory.
        """
        if not ownership.project_id:
            raise StorageViolation("proje kimligi olmadan namespace silinemez")
        source = self.namespace_root(ownership)
        if not source.is_dir():
            raise FileNotFoundError("proje namespace'i bulunamadi")
        trash_root = self.workspace_root(ownership) / ".trash"
        trash_root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        suffix = f"{random.randrange(16 ** 6):06x}"
        recovery_token = f"{ownership.project_id}__{stamp}__{suffix}"
        destination = trash_root / recovery_token
        os.replace(source, destination)
        return recovery_token


def _looks_absolute(text: str) -> bool:
    normalized = text.replace("\\", "/")
    if normalized.startswith("/"):
        return True
    if normalized.startswith("//"):  # UNC
        return True
    if len(normalized) >= 2 and normalized[1] == ":" and normalized[0].isalpha():
        return True  # Windows drive letter
    return False


def build_ownership(user_id: str, workspace_id: str, project_id: str | None = None) -> Ownership:
    """Validated ownership from raw (possibly hostile) request values."""
    return Ownership(
        validate_id(user_id, field="user_id"),
        validate_id(workspace_id, field="workspace_id"),
        validate_id(project_id, field="project_id") if project_id else None,
    )
