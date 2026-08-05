"""Ownership identifiers and the safe-name rules every path segment must obey.

WHY THIS EXISTS
---------------
Pre-alpha is single-user, but the ownership chain is real from day one:

    user_id -> workspace_id -> project_id -> job_id

Every stored artifact lives under that chain, so adding real authentication later
is a matter of resolving `user_id` from a session instead of from config -- not a
storage migration. Identifiers are validated HERE, once, because they are used to
build filesystem paths: an unvalidated id is a path-traversal bug waiting to
happen (`../../other-workspace`).
"""
from __future__ import annotations

import re
import secrets
import unicodedata

#: A single path segment: letters, digits, dash, underscore. No dots (so `..`
#: cannot be spelled), no separators, no spaces, no leading dash.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

#: Prefixes make ids self-describing in logs, audit events and directory listings.
ID_PREFIXES = {
    "user": "usr",
    "workspace": "wsp",
    "project": "prj",
    "job": "job",
    "source": "src",
    "output": "out",
    "event": "evt",
    "revision": "rev",
}


class InvalidIdentifier(ValueError):
    """Raised instead of silently sanitizing: a bad id is a bug, not a typo to fix."""


def new_id(kind: str, *, entropy_bytes: int = 8) -> str:
    prefix = ID_PREFIXES.get(kind)
    if not prefix:
        raise InvalidIdentifier(f"bilinmeyen kimlik turu: {kind!r} (beklenen: {sorted(ID_PREFIXES)})")
    return f"{prefix}_{secrets.token_hex(entropy_bytes)}"


def validate_id(value, *, kind: str | None = None, field: str = "id") -> str:
    """Returns `value` when it is a safe single path segment, else raises.

    Never sanitizes. Callers that accept ids from HTTP MUST route them through
    here before they reach the storage layer.
    """
    if not isinstance(value, str):
        raise InvalidIdentifier(f"{field} metin olmali, gelen: {type(value).__name__}")
    candidate = value.strip()
    if not _ID_RE.fullmatch(candidate):
        raise InvalidIdentifier(
            f"{field} gecersiz: {value!r}. Yalnizca harf, rakam, '-' ve '_' kullanilabilir; "
            "yol ayraci, '..' ve mutlak yol kabul edilmez."
        )
    if kind:
        prefix = ID_PREFIXES.get(kind)
        if prefix and not candidate.startswith(f"{prefix}_"):
            raise InvalidIdentifier(f"{field} '{prefix}_' onekiyle baslamali: {value!r}")
    return candidate


def safe_filename(value, *, default: str = "dosya", max_length: int = 120) -> str:
    """Turns arbitrary user text (an uploaded file's name) into ONE safe segment.

    Unlike `validate_id` this DOES sanitize, because a display filename is
    untrusted input we still want to keep something readable from. It strips
    directory parts, control characters, leading dots and Windows-reserved names,
    so the result can never escape its directory or shadow a device file.
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.replace("\\", "/").split("/")[-1]  # drop any directory component
    text = "".join(ch for ch in text if ch.isprintable() and ch not in '<>:"|?*')
    text = re.sub(r"\s+", "_", text).strip("._ ")
    text = re.sub(r"[^A-Za-z0-9._-]", "_", text)
    text = re.sub(r"_{2,}", "_", text)
    while ".." in text:
        text = text.replace("..", ".")
    stem, _, suffix = text.rpartition(".")
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                *(f"lpt{i}" for i in range(1, 10))}
    if (stem or text).lower() in reserved:
        text = f"_{text}"
    if not text or text.strip(".") == "":
        text = default
    if len(text) > max_length:
        stem, _, suffix = text.rpartition(".")
        keep = max_length - (len(suffix) + 1 if suffix else 0)
        text = (stem[:keep] + "." + suffix) if suffix else text[:max_length]
    return text


class Ownership:
    """The immutable ownership tuple carried by every request, job and artifact."""

    __slots__ = ("user_id", "workspace_id", "project_id")

    def __init__(self, user_id: str, workspace_id: str, project_id: str | None = None):
        self.user_id = validate_id(user_id, kind="user", field="user_id")
        self.workspace_id = validate_id(workspace_id, kind="workspace", field="workspace_id")
        self.project_id = (
            validate_id(project_id, kind="project", field="project_id")
            if project_id is not None else None
        )

    def with_project(self, project_id: str) -> "Ownership":
        return Ownership(self.user_id, self.workspace_id, project_id)

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
        }

    def matches(self, other: "Ownership", *, require_project: bool = True) -> bool:
        if self.user_id != other.user_id or self.workspace_id != other.workspace_id:
            return False
        if require_project and self.project_id != other.project_id:
            return False
        return True

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (f"<Ownership user={self.user_id} workspace={self.workspace_id} "
                f"project={self.project_id}>")

    def __eq__(self, other) -> bool:
        return isinstance(other, Ownership) and self.to_dict() == other.to_dict()

    def __hash__(self) -> int:
        return hash((self.user_id, self.workspace_id, self.project_id))
