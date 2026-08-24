"""Human-editable subtitle tracks derived from rendered narration captions."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone


MAX_CUES = 1000
MAX_CUE_CHARS = 300
MAX_TOTAL_CHARS = 50000
_TIMING = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})$")


class SubtitleEditError(ValueError):
    pass


def _seconds(parts) -> float:
    hours, minutes, seconds, millis = (int(value) for value in parts)
    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def cue_id(index: int, cue: dict) -> str:
    basis = f"{index}|{float(cue['start']):.3f}|{float(cue['end']):.3f}"
    return "cue_" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]


def normalize_cues(cues) -> list[dict]:
    if not isinstance(cues, list) or not cues:
        raise SubtitleEditError("duzenlenecek altyazi satiri bulunamadi")
    if len(cues) > MAX_CUES:
        raise SubtitleEditError(f"altyazi en fazla {MAX_CUES} satir olabilir")
    normalized = []
    total = 0
    for index, raw in enumerate(cues, start=1):
        try:
            start = round(float(raw.get("start")), 3)
            end = round(float(raw.get("end")), 3)
        except (AttributeError, TypeError, ValueError):
            raise SubtitleEditError(f"{index}. satirin zaman damgasi gecersiz") from None
        text = str((raw or {}).get("text") or "").strip()
        if start < 0 or end <= start:
            raise SubtitleEditError(f"{index}. satirin zaman araligi gecersiz")
        if not text:
            raise SubtitleEditError(f"{index}. altyazi satiri bos olamaz")
        if len(text) > MAX_CUE_CHARS:
            raise SubtitleEditError(
                f"{index}. altyazi satiri {MAX_CUE_CHARS} karakteri gecemez")
        total += len(text)
        normalized.append({"start": start, "end": end, "text": text})
    if total > MAX_TOTAL_CHARS:
        raise SubtitleEditError(
            f"altyazi metni toplam {MAX_TOTAL_CHARS} karakteri gecemez")
    return normalized


def parse_srt(text: str) -> list[dict]:
    blocks = re.split(r"\n\s*\n", str(text or "").replace("\r\n", "\n").strip())
    cues = []
    for block in blocks:
        lines = [line.rstrip() for line in block.split("\n")]
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        if len(lines) < 2:
            continue
        match = _TIMING.match(lines[0].strip())
        if not match:
            raise SubtitleEditError("SRT zaman satiri okunamadi")
        cues.append({
            "start": _seconds(match.groups()[:4]),
            "end": _seconds(match.groups()[4:]),
            "text": "\n".join(lines[1:]).strip(),
        })
    return normalize_cues(cues)


def public_cues(cues) -> list[dict]:
    normalized = normalize_cues(cues)
    return [
        {"id": cue_id(index, cue), "index": index, **cue}
        for index, cue in enumerate(normalized, start=1)
    ]


def content_sha256(cues) -> str:
    payload = json.dumps(normalize_cues(cues), ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def apply_text_edits(cues, updates) -> tuple[list[dict], list[str]]:
    current = public_cues(cues)
    by_id = {cue["id"]: cue for cue in current}
    changed = []
    for update in updates or []:
        identifier = str((update or {}).get("id") or "").strip()
        if identifier not in by_id:
            raise SubtitleEditError(f"bilinmeyen altyazi satiri: {identifier!r}")
        text = str((update or {}).get("text") or "").strip()
        if text != by_id[identifier]["text"]:
            by_id[identifier]["text"] = text
            changed.append(identifier)
    rebuilt = [
        {"start": cue["start"], "end": cue["end"], "text": cue["text"]}
        for cue in current
    ]
    return normalize_cues(rebuilt), changed


def build_artifact(cues, *, source: str, actor: str, notes: str = "",
                   previous: dict | None = None, revision_id: str | None = None) -> dict:
    normalized = normalize_cues(cues)
    history = list((previous or {}).get("revisions") or [])
    history.insert(0, {
        "edited_by": str(actor or "console")[:120],
        "edited_at": datetime.now(timezone.utc).isoformat(),
        "notes": str(notes or "").strip()[:1000] or None,
        "revision_id": revision_id,
        "previous_content_sha256": (
            content_sha256((previous or {}).get("cues")) if (previous or {}).get("cues") else None),
    })
    return {
        "schema_version": 1,
        "artifact_type": "subtitle_track",
        "status": "review_required",
        "status_reason": "altyazi metni duzenlendi; video yeniden render edilip izlenmeli",
        "source": source,
        "revision": int((previous or {}).get("revision") or 0) + 1,
        "cues": normalized,
        "cue_count": len(normalized),
        "content_sha256": content_sha256(normalized),
        "revisions": history[:50],
        "not_uploaded": True,
    }
