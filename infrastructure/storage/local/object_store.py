"""Local filesystem implementation of `application.ports.ObjectStore`.

WHY THIS WRAPS THE EXISTING STORAGE ADAPTER
-------------------------------------------
Namespace isolation, traversal rejection and atomic writes are already solved and
already tested in `creator_console.storage`. Reimplementing them here would mean
two independently-drifting copies of the security rules, so this adapter delegates
to `LocalFilesystemStorage` and adds only the object-store surface on top.

WHY `materialize()` IS A NO-OP HERE
----------------------------------
In the local backend the stored file IS the working copy, so ffmpeg can read the
real path directly and nothing needs copying or cleaning up. The context manager
exists for the future cloud adapter, which must download to a temporary path and
delete it deterministically. Making the local case free is the point: development
pays nothing for an abstraction that only matters in production.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path

from creator_console.storage import StorageViolation, atomic_write_bytes

#: Read in chunks so a multi-gigabyte source video is never loaded into memory
#: just to be hashed.
_HASH_CHUNK_BYTES = 1024 * 1024


class ObjectNotFound(FileNotFoundError):
    """The requested key holds no object.

    A distinct type so a caller cannot confuse "absent" with "empty": returning
    empty bytes for a missing video is how a later stage ends up treating a
    zero-byte file as valid media.
    """


class LocalObjectStore:
    """Namespace-isolated object storage backed by the local filesystem."""

    def __init__(self, storage):
        #: A `creator_console.storage.StorageAdapter`. Path safety is its job.
        self.storage = storage

    # -- internals ---------------------------------------------------------
    def _path(self, ownership, key: str, *, create_parent: bool = False) -> Path:
        return Path(self.storage.resolve(ownership, key, create_parent=create_parent))

    def _stat(self, ownership, key: str, path: Path, *,
              content_type: str | None = None, with_digest: bool = True):
        if not path.is_file():
            return {"key": key, "size_bytes": 0, "exists": False,
                    "sha256": None, "content_type": content_type}
        return {
            "key": key,
            "size_bytes": path.stat().st_size,
            "exists": True,
            "sha256": _sha256_file(path) if with_digest else None,
            "content_type": content_type,
        }

    # -- writes ------------------------------------------------------------
    def put_bytes(self, ownership, key: str, payload: bytes, *,
                  content_type: str | None = None):
        path = self._path(ownership, key, create_parent=True)
        atomic_write_bytes(path, payload)
        return self._stat(ownership, key, path, content_type=content_type)

    def put_file(self, ownership, key: str, source_path, *,
                 content_type: str | None = None):
        source = Path(source_path)
        if not source.is_file():
            raise ObjectNotFound(f"kaynak dosya bulunamadi: {source}")
        target = self._path(ownership, key, create_parent=True)
        resolved_source = Path(source).resolve()
        if resolved_source == target.resolve():
            # Already the stored object: publishing it again would copy a file
            # onto itself and truncate it.
            return self._stat(ownership, key, target, content_type=content_type)
        atomic_write_bytes(target, source.read_bytes())
        return self._stat(ownership, key, target, content_type=content_type)

    # -- reads -------------------------------------------------------------
    @contextmanager
    def open(self, ownership, key: str):
        path = self._path(ownership, key)
        if not path.is_file():
            raise ObjectNotFound(f"nesne bulunamadi: {key}")
        handle = path.open("rb")
        try:
            yield handle
        finally:
            handle.close()

    def stat(self, ownership, key: str):
        try:
            path = self._path(ownership, key)
        except StorageViolation:
            return {"key": key, "size_bytes": 0, "exists": False,
                    "sha256": None, "content_type": None}
        return self._stat(ownership, key, path)

    def exists(self, ownership, key: str) -> bool:
        try:
            return self._path(ownership, key).is_file()
        except StorageViolation:
            return False

    def delete(self, ownership, key: str) -> bool:
        try:
            path = self._path(ownership, key)
        except StorageViolation:
            return False
        if not path.is_file():
            return False
        path.unlink()
        return True

    # -- the ffmpeg lifecycle ---------------------------------------------
    @contextmanager
    def materialize(self, ownership, key: str):
        """Yield a local path for `key` so ffmpeg/local_pipeline can read it.

        Local backend: the stored file already IS the working copy, so the real
        path is yielded and nothing is cleaned up. A cloud adapter downloads to a
        temporary file here and deletes it on exit.
        """
        path = self._path(ownership, key)
        if not path.is_file():
            raise ObjectNotFound(f"nesne bulunamadi: {key}")
        yield path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()
