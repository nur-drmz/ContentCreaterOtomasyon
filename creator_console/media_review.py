"""Digest-bound human review checklists, shared by every reviewable medium.

WHY THIS EXISTS
---------------
`render_review.py` already solved this problem once: a hash gate proves the
reviewer saw *some* bytes, but not that they checked the things that actually go
wrong. So a review became an ARTIFACT whose every entry records the digest of
the media it was ticked against, and a regenerated file resets the list instead
of silently carrying a verdict about something nobody watched.

Voice review needs exactly the same rules with a different list of questions.
Copying the module would have meant two implementations of "is this entry
stale?" drifting apart — the first bug being one of them forgetting that a
digest change invalidates the WHOLE list, not just one entry.

So the rules live here once, parameterised by a `ReviewSpec`, and each medium
supplies only what genuinely differs: its artifact path, its checklist, and the
tabs a revision can point at.

WHAT THIS MODULE MUST NOT DO
----------------------------
It cannot approve anything, it never touches the media, and "needs revision" is
a note plus a pointer to the tab that owns the fix — it does not re-run a phase
or mutate any artifact.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

VERDICT_PENDING = "pending"
VERDICT_PASS = "pass"
VERDICT_NEEDS_REVISION = "needs_revision"


@dataclass(frozen=True)
class ReviewSpec:
    """Everything that differs between one reviewable medium and another."""

    kind: str                 #: "render" | "voice"
    artifact_path: str        #: namespace-relative json path
    artifact_type: str        #: `artifact_type` written into the record
    items: tuple              #: the checklist definitions
    fix_tabs: tuple           #: tabs a revision request may point at
    missing_media_blocker: str
    revision_blocker: str
    stale_blocker: str

    @property
    def keys(self) -> tuple:
        return tuple(item["key"] for item in self.items)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_review(spec: ReviewSpec, ownership, media_sha256: str | None = None) -> dict:
    return {
        "schema_version": 1,
        "artifact_type": spec.artifact_type,
        **ownership.to_dict(),
        "media_kind": spec.kind,
        "media_sha256": media_sha256,
        "items": {},
        "revision_requests": [],
        "updated_at": _now(),
        "not_uploaded": True,
    }


def evaluate(spec: ReviewSpec, review: dict | None, media_sha256: str | None,
             ownership, current_snapshot: dict | None = None) -> dict:
    """Current checklist state for THIS media.

    An entry recorded against a different digest is reported as `stale` and does
    NOT count as done -- the reviewer looked at a different file.
    """
    review = review if isinstance(review, dict) else new_review(spec, ownership, media_sha256)
    recorded = _recorded_digest(review)
    binds = bool(media_sha256) and recorded == media_sha256
    raw_items = review.get("items") or {}

    items, done, stale = [], 0, 0
    for definition in spec.items:
        entry = raw_items.get(definition["key"]) or {}
        entry_hash = _entry_digest(entry)
        checked = bool(entry.get("checked"))
        item_binds = checked and bool(media_sha256) and entry_hash == media_sha256
        if checked and not item_binds:
            stale += 1
        if item_binds:
            done += 1
        items.append({
            **definition,
            "checked": item_binds,
            "stale": checked and not item_binds,
            "checked_by": entry.get("checked_by") if item_binds else None,
            "checked_at": entry.get("checked_at") if item_binds else None,
            "note": entry.get("note") if item_binds else None,
        })

    outstanding = [item["key"] for item in items if not item["checked"]]
    revisions = [r for r in (review.get("revision_requests") or [])
                 if _entry_digest(r) == media_sha256 and not r.get("resolved")]
    open_revision = revisions[0] if revisions else None
    total = len(spec.items)
    return {
        "kind": spec.kind,
        "media_sha256": media_sha256,
        "recorded_media_sha256": recorded,
        "binds_current_media": binds,
        "items": items,
        "done_count": done,
        "total_count": total,
        "stale_count": stale,
        "outstanding": outstanding,
        "complete": done == total,
        "verdict": (VERDICT_NEEDS_REVISION if open_revision else
                    VERDICT_PASS if done == total else VERDICT_PENDING),
        "open_revision": open_revision,
        "revision_history": list(review.get("revision_requests") or [])[:20],
        # What the last revision changed, once the fix has actually landed.
        "revision_report": revision_report(review, current_snapshot, media_sha256),
        "blocker": _blocker(spec, done, total, stale, open_revision, media_sha256),
    }


#: Fields worth reporting on, in the order a reviewer reads them. Anything not
#: listed is ignored rather than dumped: a diff of every key would bury the two
#: lines that matter under timestamps and hashes nobody reads.
REPORTABLE_FIELDS = (
    ("resolution", "Cozunurluk"),
    ("duration_sec", "Sure (sn)"),
    ("video_codec", "Video codec"),
    ("audio_present", "Ses akisi"),
    ("subtitle_source", "Altyazi kaynagi"),
    ("caption_theme", "Altyazi temasi"),
    ("word_count", "Kelime sayisi"),
    ("voice", "Ses"),
    ("preset", "Anlatim onayari"),
    ("status", "Durum"),
)


def revision_report(review: dict, current: dict | None,
                    media_sha256: str | None) -> dict | None:
    """What the last revision actually changed, or None when there is nothing
    to report yet.

    Reported only once the media has genuinely been replaced: while the digest
    still matches the one the revision was raised against, no fix has landed and
    claiming otherwise would be worse than saying nothing.
    """
    entries = review.get("revision_requests") or []
    if not entries:
        return None
    entry = entries[0]
    before = entry.get("before") or {}
    if not before or not media_sha256:
        return None
    if entry.get("media_sha256") == media_sha256:
        return None  # nothing re-ran yet; the reviewer is looking at the same file

    now = dict(current or {})
    changes = []
    for key, label in REPORTABLE_FIELDS:
        if key not in before and key not in now:
            continue
        was, became = before.get(key), now.get(key)
        if was != became:
            changes.append({"field": key, "label": label,
                            "before": was, "after": became})
    return {
        "notes": entry.get("notes"),
        "requested_by": entry.get("requested_by"),
        "requested_at": entry.get("requested_at"),
        "target_tab": entry.get("target_tab"),
        "resolved": bool(entry.get("resolved")),
        "changes": changes,
        # An empty list is a RESULT, not a missing report: the phase re-ran and
        # produced something byte-different whose reportable facts are identical.
        # Saying so is more useful than an empty card.
        "unchanged": not changes,
        "before_media_sha256_16": (entry.get("media_sha256") or "")[:16],
        "after_media_sha256_16": (media_sha256 or "")[:16],
    }


def _recorded_digest(review: dict):
    """Digest this record is bound to.

    `video_sha256` is read as a fallback so render reviews written before this
    module existed keep binding instead of silently resetting every checklist
    that was already complete.
    """
    return review.get("media_sha256") or review.get("video_sha256")


def _entry_digest(entry: dict):
    return entry.get("media_sha256") or entry.get("video_sha256")


def _blocker(spec, done, total, stale, open_revision, media_sha256) -> str:
    if not media_sha256:
        return spec.missing_media_blocker
    if open_revision:
        return spec.revision_blocker.format(
            notes=open_revision.get("notes") or "not yok")
    if stale:
        return spec.stale_blocker.format(count=stale)
    if done < total:
        return f"Insan inceleme listesi tamamlanmadi ({done}/{total})."
    return ""


def set_item(spec: ReviewSpec, review: dict, key: str, *, checked: bool,
             media_sha256: str, reviewed_by: str, note: str = "") -> dict:
    if key not in spec.keys:
        raise ValueError(f"bilinmeyen kontrol maddesi: {key!r} (beklenen: {list(spec.keys)})")
    if not media_sha256:
        raise ValueError(spec.missing_media_blocker)
    if checked and not str(reviewed_by or "").strip():
        raise ValueError("inceleyen kisi zorunlu: kontrol maddesi bir insan karari")

    # A digest change invalidates the whole list, not just one entry: the
    # reviewer is now looking at a different file.
    if _recorded_digest(review) != media_sha256:
        review["media_sha256"] = media_sha256
        review.pop("video_sha256", None)
        review["items"] = {}

    items = review.setdefault("items", {})
    if checked:
        items[key] = {
            "checked": True, "media_sha256": media_sha256,
            "checked_by": str(reviewed_by).strip()[:120],
            "checked_at": _now(), "note": str(note or "").strip()[:500] or None,
        }
    else:
        items.pop(key, None)
    review["updated_at"] = _now()
    return review


def request_revision(spec: ReviewSpec, review: dict, *, media_sha256: str,
                     requested_by: str, notes: str, target_tab: str,
                     before: dict | None = None) -> dict:
    """Record 'needs revision', and remember what the media looked like.

    This module still changes nothing about the media. `before` is a snapshot
    the CALLER took, kept so that once the fix has run the console can say what
    actually changed instead of leaving the reviewer to compare two videos from
    memory. A revision nobody can verify is a note, not a fix.
    """
    if not str(requested_by or "").strip():
        raise ValueError("revizyon isteyen kisi zorunlu")
    if not str(notes or "").strip():
        raise ValueError("revizyon notu zorunlu: neyin duzeltilecegi yazilmali")
    if target_tab not in spec.fix_tabs:
        raise ValueError(f"gecersiz hedef sekme: {target_tab!r} "
                         f"(beklenen: {list(spec.fix_tabs)})")
    entry = {
        "media_sha256": media_sha256,
        "requested_by": str(requested_by).strip()[:120],
        "notes": str(notes).strip()[:1000],
        "target_tab": target_tab,
        "requested_at": _now(),
        "resolved": False,
        "before": dict(before or {}),
    }
    review.setdefault("revision_requests", []).insert(0, entry)
    review["updated_at"] = _now()
    return review


def clear_revision(spec: ReviewSpec, review: dict, *, media_sha256: str,
                   resolved_by: str) -> dict:
    for entry in review.get("revision_requests") or []:
        if _entry_digest(entry) == media_sha256 and not entry.get("resolved"):
            entry["resolved"] = True
            entry["resolved_by"] = str(resolved_by or "").strip()[:120] or None
            entry["resolved_at"] = _now()
    review["updated_at"] = _now()
    return review
