"""Background music, and the licence facts that must travel with it.

WHY THIS REFUSES TO SAY "ROYALTY FREE"
---------------------------------------
A music file on disk carries no proof of anything. The only thing this module
can honestly report is whether a human recorded, in a sidecar file, where the
track came from and under what licence -- and who checked. Everything else is a
guess, and a guess about copyright is the kind that ends in a takedown.

So `license_status` returns one of three facts and never a boolean "safe":

  verified   -- a sidecar exists, is complete, and names who verified it
  unverified -- a sidecar exists but is incomplete or unattested
  missing    -- there is no sidecar at all

The UI renders `unverified` and `missing` as warnings. No code path in this
project may translate any of these into a claim that a track is clear to use.

MoneyPrinterTurbo ships bundled music. None of it is copied here: its own
repository does not establish the provenance of those files, so importing them
would mean shipping exactly the unverifiable claim this module exists to avoid.
Two sources are supported instead -- a library directory the operator
configures, and a file the operator uploads to a project -- and both go through
the same contract.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
LICENSE_SUFFIX = ".license.json"
AUDIO_SUFFIXES = frozenset({".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"})

#: Every one of these must be present and non-empty for a sidecar to count as
#: complete. `verified_by` is deliberately included: a licence record nobody
#: signed is a claim without an author.
REQUIRED_LICENSE_FIELDS = ("title", "artist", "license_id", "source_url",
                           "verified_by")

STATUS_VERIFIED = "verified"
STATUS_UNVERIFIED = "unverified"
STATUS_MISSING = "missing"

#: Shown next to a track. Never says a track is safe -- only what is known.
STATUS_LABELS = {
    STATUS_VERIFIED: "lisans kaydi tam ve imzali",
    STATUS_UNVERIFIED: "lisans kaydi eksik -- kullanmadan once dogrulayin",
    STATUS_MISSING: "lisans kaydi YOK -- kaynagi ve lisansi bilinmiyor",
}


class MusicLibraryError(ValueError):
    """A rejected music operation, phrased for the operator."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def license_path(track_path) -> Path:
    """The sidecar for a track: `calm.mp3` -> `calm.mp3.license.json`."""
    return Path(str(track_path) + LICENSE_SUFFIX)


def read_license(track_path):
    path = license_path(track_path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def validate_license(data) -> dict:
    """Report what a sidecar establishes, and what it is missing.

    Never raises for an incomplete record: an incomplete licence is a fact to
    surface, not an error to swallow. It raises only for a record that is not a
    record at all.
    """
    if data is None:
        return {"status": STATUS_MISSING, "missing": list(REQUIRED_LICENSE_FIELDS),
                "label": STATUS_LABELS[STATUS_MISSING]}
    if not isinstance(data, dict):
        raise MusicLibraryError("lisans kaydi bir JSON nesnesi olmali")
    missing = [field for field in REQUIRED_LICENSE_FIELDS
               if not _clean(data.get(field))]
    status = STATUS_VERIFIED if not missing else STATUS_UNVERIFIED
    return {
        "status": status,
        "label": STATUS_LABELS[status],
        "missing": missing,
        "title": _clean(data.get("title"), 200),
        "artist": _clean(data.get("artist"), 200),
        "license_id": _clean(data.get("license_id"), 80),
        "source_url": _clean(data.get("source_url"), 500),
        "verified_by": _clean(data.get("verified_by"), 120),
        "verified_at": _clean(data.get("verified_at"), 60),
        "attribution_required": bool(data.get("attribution_required")),
        "attribution_text": _clean(data.get("attribution_text"), 300),
        "notes": _clean(data.get("notes"), 500),
    }


def license_status(track_path) -> dict:
    return validate_license(read_license(track_path))


def describe_track(track_path, *, origin: str, display_name: str = "") -> dict:
    """One track as the manifest and the UI both want it.

    `display_name` is carried separately because `storage.resolve` runs every
    path segment through `safe_filename`, so a file the operator named
    `Sakin Parca - Final.mp3` may not be called that on disk.
    """
    path = Path(track_path)
    if not path.is_file():
        raise MusicLibraryError(f"muzik dosyasi bulunamadi: {path.name}")
    if path.suffix.lower() not in AUDIO_SUFFIXES:
        raise MusicLibraryError(
            f"desteklenmeyen ses bicimi: {path.suffix or '(uzantisiz)'}; "
            f"kabul edilenler: {', '.join(sorted(AUDIO_SUFFIXES))}")
    licence = license_status(path)
    return {
        "origin": origin,
        "display_name": _clean(display_name) or licence.get("title") or path.stem,
        "filename": path.name,
        "bytes": path.stat().st_size,
        "license": licence,
        # Surfaced explicitly so a caller cannot forget to ask. There is no
        # "safe" field anywhere in this structure, by design.
        "needs_review": licence["status"] != STATUS_VERIFIED,
        "described_at": _now(),
    }


def scan_library(root) -> dict:
    """Every usable track under a configured library root.

    Unverified tracks are LISTED, not hidden: the operator may legitimately own
    music whose paperwork lives elsewhere. They are listed with their status so
    the choice is informed rather than made for them.
    """
    root = Path(root) if root else None
    if root is None or not root.is_dir():
        return {"configured": bool(root), "root_exists": False, "tracks": [],
                "verified_count": 0,
                "note": ("muzik kutuphanesi kokü tanimli degil; "
                         "CREATOR_CONSOLE_MUSIC_ROOT ayarlayin veya projeye "
                         "dosya yukleyin")}
    tracks = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        try:
            tracks.append(describe_track(path, origin="global_library"))
        except MusicLibraryError:
            continue
    verified = sum(1 for track in tracks
                   if track["license"]["status"] == STATUS_VERIFIED)
    return {
        "configured": True,
        "root_exists": True,
        "tracks": tracks,
        "verified_count": verified,
        "note": (f"{len(tracks)} parca bulundu, {verified} tanesinin lisans "
                 "kaydi tam. Lisansi dogrulanmamis parcalar da listelenir; "
                 "bu proje hicbir parcayi telifsiz olarak nitelemez."),
    }


def manifest_entry(track: dict, *, relative_path: str, sha256: str,
                   duration_sec: float, volume: float = 0.18,
                   loop: bool = True, ducking: bool = True) -> dict:
    """The `audio.music` block a timeline manifest carries."""
    if not (0.0 <= float(volume) <= 1.0):
        raise MusicLibraryError("muzik ses duzeyi 0.0 ile 1.0 arasinda olmali")
    return {
        "present": True,
        "origin": track["origin"],
        "relative_path": relative_path,
        "display_name": track["display_name"],
        "sha256": sha256,
        "duration_sec": round(float(duration_sec), 2),
        "license": track["license"],
        "needs_review": track["needs_review"],
        "volume": round(float(volume), 3),
        "loop": bool(loop),
        "ducking": bool(ducking),
    }


def absent() -> dict:
    """No music. An explicit decision, not an unanswered question."""
    return {"present": False, "reason": "operator muzik kullanmamayi secti"}
