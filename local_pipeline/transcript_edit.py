"""Reviewable transcript: segments, human edits, and revision history.

WHY THIS EXISTS
---------------
The caption screen used to show a summary -- word count, language, confidence --
and the reader could not see, let alone fix, the words themselves. The only edit
channel was a word-level find/replace table, which cannot fix "Steelers Rider!"
into "Sheepstealer's rider" (different token counts) and cannot fix a sentence
at all.

THE INVARIANT THIS MODULE PROTECTS
----------------------------------
Per-word start/end times drive the active-word highlight. Free-text editing is
the natural way for a person to fix a transcript and the fastest way to destroy
those timings. So edits are applied SEGMENT BY SEGMENT and the segment's own
[start, end] is never moved:

  * same token count  -> each word keeps the exact time the aligner gave it;
  * different count   -> the segment's span is redistributed across the new
    tokens by length, and every affected word is marked `retimed` so nothing
    downstream mistakes an estimate for an alignment.

RAW RECOGNITION IS NEVER OVERWRITTEN
------------------------------------
The previous correction path rebuilt the artifact from `artifact["words"]`,
which already held the corrected text, so a correction could not be undone and
the original recognition was gone after the first edit -- despite a docstring
claiming otherwise. Here the untouched ASR output is copied into `asr` once and
every later revision is computed against the CURRENT content while `asr` stays
exactly as the recogniser produced it.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from . import approval

SCHEMA_VERSION = 2

#: A caption line longer than this does not fit a 9:16 phone frame at the sizes
#: this pipeline renders. Reported as a rejection with the offending length, not
#: silently wrapped, because wrapping changes what the reviewer approved.
MAX_SEGMENT_CHARS = 220

#: Guardrail against a paste that would turn one Short's caption into an essay.
MAX_TOTAL_CHARS = 20000
MAX_SEGMENTS = 400

_TOKEN_RE = re.compile(r"\S+")


class TranscriptEditError(ValueError):
    """A rejected transcript edit, phrased for the person editing it."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def segment_id(index: int) -> str:
    """Positional and stable: no edit in this module changes the segment count,
    so an id stays attached to the same moment in the clip across revisions."""
    return f"seg_{int(index) + 1:04d}"


def content_sha256(artifact) -> str | None:
    """The SAME digest the approval gate binds to, or None when there is no
    approvable content. Computed here so a revision records the identity the
    approval will later check, rather than a second, subtly different hash."""
    try:
        return approval.content_hash_for("transcript", artifact or {})
    except (ValueError, KeyError):
        return None


# ------------------------------------------------------------------ segments
def _words_in(words, start, end):
    """Words whose midpoint falls inside the segment.

    Midpoint rather than overlap: a word straddling a cue boundary belongs to
    exactly one cue, and counting it twice would double it in the edited text.
    """
    picked = []
    for word in words or []:
        try:
            word_start = float(word.get("start"))
            word_end = float(word.get("end"))
        except (TypeError, ValueError):
            continue
        middle = (word_start + word_end) / 2.0
        if start is None or end is None or start <= middle <= end:
            picked.append(word)
    return picked


def build_segments(artifact, *, low_confidence_threshold: float) -> list:
    """Chronological review units: what the screen shows, one row each.

    Cues are the unit rather than words, because a cue is what appears on screen
    at one time -- which is what a reviewer is actually judging.
    """
    artifact = artifact or {}
    words = artifact.get("words") or []
    cues = artifact.get("cues") or []
    edited_ids = set(artifact.get("edited_segment_ids") or [])
    segments = []

    if not cues and words:
        # Word-level result with no cues: present the whole thing as one segment
        # rather than inventing groupings the renderer would not reproduce.
        cues = [{
            "start": words[0].get("start"), "end": words[-1].get("end"),
            "text": " ".join(str(w.get("text", "")) for w in words).strip(),
        }]

    for index, cue in enumerate(cues):
        start = cue.get("start")
        end = cue.get("end")
        members = _words_in(words, start, end) if words else []
        probabilities = [float(w.get("probability") or 0.0) for w in members
                         if w.get("probability") is not None]
        low = [
            {"text": w.get("text"), "probability": w.get("probability"),
             "start_sec": w.get("start")}
            for w in members
            if float(w.get("probability") or 0.0) < low_confidence_threshold
        ]
        identifier = segment_id(index)
        segments.append({
            "id": identifier,
            "index": index,
            "start_sec": start,
            "end_sec": end,
            "text": str(cue.get("text") or ""),
            "word_count": len(members),
            "average_probability": (round(sum(probabilities) / len(probabilities), 4)
                                    if probabilities else None),
            "low_confidence_words": low,
            "low_confidence_count": len(low),
            "retimed": any(w.get("retimed") for w in members),
            "edited": identifier in edited_ids,
            # Preserve what ASR heard while allowing a reviewer to keep sung
            # lyrics or other non-dialogue vocals out of burned-in captions.
            "excluded_from_subtitles": bool(cue.get("excluded_from_subtitles")),
        })
    return segments


def segments_to_text(segments) -> str:
    """One segment per line: the shape the full-text editor round-trips."""
    return "\n".join(str(segment.get("text") or "") for segment in segments)


# --------------------------------------------------------------- validation
def validate_segment_texts(texts) -> list:
    """Every problem with a proposed edit, phrased for the editor.

    Returns ALL of them rather than the first: fixing one error only to be told
    about the next is the worst possible review loop.
    """
    problems = []
    if not texts:
        return ["duzenlenecek segment verilmedi"]
    if len(texts) > MAX_SEGMENTS:
        problems.append(f"en fazla {MAX_SEGMENTS} segment islenebilir ({len(texts)} verildi)")
    total = 0
    non_empty = 0
    for identifier, text in texts:
        value = str(text if text is not None else "")
        stripped = value.strip()
        total += len(value)
        if stripped:
            non_empty += 1
        if not stripped:
            problems.append(f"{identifier}: altyazi metni bos olamaz")
        if len(value) > MAX_SEGMENT_CHARS:
            problems.append(
                f"{identifier}: satir cok uzun ({len(value)} karakter, en fazla "
                f"{MAX_SEGMENT_CHARS}); dikey videoda ekrana sigmaz")
        if "\n" in value:
            problems.append(f"{identifier}: segment metni tek satir olmali")
    if total > MAX_TOTAL_CHARS:
        problems.append(f"transkript cok uzun ({total} karakter, en fazla {MAX_TOTAL_CHARS})")
    if non_empty == 0:
        problems.append("transkriptin tamami bos; onaylanacak altyazi kalmaz")
    return problems


# ------------------------------------------------------------------ retiming
def _redistribute(tokens, start, end, template):
    """Spread `tokens` across [start, end] proportionally to their length.

    Used only when the edit changed the token count, so there is no aligner
    output to preserve. Length-weighted rather than uniform because a long word
    genuinely takes longer to say, and the highlight tracks the voice better
    for it.
    """
    try:
        span_start = float(start)
        span_end = float(end)
    except (TypeError, ValueError):
        span_start, span_end = 0.0, float(len(tokens)) * 0.3
    if span_end <= span_start:
        span_end = span_start + max(0.08 * len(tokens), 0.08)
    weights = [max(1, len(token)) for token in tokens]
    total = float(sum(weights))
    produced = []
    cursor = span_start
    for token, weight in zip(tokens, weights):
        share = (span_end - span_start) * (weight / total)
        word_end = min(span_end, cursor + share)
        if word_end <= cursor:
            word_end = cursor + 0.08
        produced.append({
            "text": token,
            "start": round(cursor, 3),
            "end": round(word_end, 3),
            # Confidence belonged to the recogniser's word, not to this one.
            "probability": None,
            "retimed": True,
            "edited": True,
        })
        cursor = word_end
    if produced and template:
        # Keep any non-timing extras the pipeline attached to the originals.
        extras = {k: v for k, v in (template[0] or {}).items()
                  if k not in {"text", "start", "end", "probability", "retimed",
                               "edited", "corrected"}}
        for item in produced:
            item.update(extras)
    return produced


def _rewrite_segment_words(members, new_text, start, end):
    """New word list for one segment, preserving alignment where it survives."""
    tokens = _TOKEN_RE.findall(str(new_text or "").strip())
    if not tokens:
        return []
    if len(tokens) == len(members):
        rewritten = []
        for token, original in zip(tokens, members):
            item = dict(original)
            changed = str(original.get("text", "")) != token
            item["text"] = token
            if changed:
                item["edited"] = True
                # The recogniser's confidence described the word it heard, not
                # the one a human just put there. Keeping it would leave the
                # corrected word highlighted as "unsure" and send the reviewer
                # back to re-check something they already fixed.
                item["probability"] = None
            rewritten.append(item)
        return rewritten
    return _redistribute(tokens, start, end, members)


def apply_segment_texts(artifact, texts) -> dict:
    """Return {words, cues, text, changed_segment_ids} for the proposed edit.

    Pure: it computes the new content and does not touch the artifact, so the
    caller can validate, hash and record a revision around it.
    """
    artifact = artifact or {}
    words = list(artifact.get("words") or [])
    cues = list(artifact.get("cues") or [])
    if not cues and words:
        cues = [{"start": words[0].get("start"), "end": words[-1].get("end"),
                 "text": " ".join(str(w.get("text", "")) for w in words).strip()}]

    wanted = {}
    for identifier, text in texts:
        wanted[str(identifier)] = str(text if text is not None else "")
    known = {segment_id(index) for index in range(len(cues))}
    unknown = sorted(set(wanted) - known)
    if unknown:
        raise TranscriptEditError(
            f"bilinmeyen segment: {unknown} (mevcut: {len(cues)} segment)")

    new_words, new_cues, changed = [], [], []
    for index, cue in enumerate(cues):
        identifier = segment_id(index)
        start, end = cue.get("start"), cue.get("end")
        members = _words_in(words, start, end) if words else []
        if identifier in wanted and str(cue.get("text") or "") != wanted[identifier]:
            text = wanted[identifier].strip()
            changed.append(identifier)
            new_cue = dict(cue)
            new_cue["text"] = text
            new_cue["edited"] = True
            new_cues.append(new_cue)
            if words:
                new_words.extend(_rewrite_segment_words(members, text, start, end))
        else:
            new_cues.append(dict(cue))
            new_words.extend(dict(word) for word in members)

    if words and not new_words:
        raise TranscriptEditError("duzenleme sonrasi hicbir kelime kalmadi")
    text = " ".join(str(w.get("text", "")) for w in new_words).strip()
    if not text:
        text = " ".join(str(c.get("text", "")) for c in new_cues).strip()
    return {"words": new_words, "cues": new_cues, "text": text,
            "changed_segment_ids": changed}


def apply_segment_exclusions(artifact, segment_ids, *, excluded: bool) -> dict:
    """Toggle caption visibility without deleting recognised text or timings."""
    record = artifact or {}
    cues = [dict(cue) for cue in (record.get("cues") or [])]
    words = [dict(word) for word in (record.get("words") or [])]
    wanted = {str(value or "").strip() for value in (segment_ids or [])}
    wanted.discard("")
    known = {segment_id(index) for index in range(len(cues))}
    unknown = sorted(wanted - known)
    if unknown:
        raise TranscriptEditError(
            f"bilinmeyen segment: {unknown} (mevcut: {len(cues)} segment)")
    if not wanted:
        raise TranscriptEditError("en az bir segment secilmeli")

    changed = []
    for index, cue in enumerate(cues):
        identifier = segment_id(index)
        if identifier not in wanted:
            continue
        if bool(cue.get("excluded_from_subtitles")) == bool(excluded):
            continue
        cue["excluded_from_subtitles"] = bool(excluded)
        changed.append(identifier)
        start, end = cue.get("start"), cue.get("end")
        for word in words:
            try:
                middle = (float(word.get("start")) + float(word.get("end"))) / 2.0
                inside = ((start is None or float(start) <= middle)
                          and (end is None or middle <= float(end)))
            except (TypeError, ValueError):
                inside = False
            if inside:
                word["excluded_from_subtitles"] = bool(excluded)
    return {"words": words, "cues": cues, "changed_segment_ids": changed}


def parse_full_text(artifact, full_text, *, low_confidence_threshold: float) -> list:
    """Turn the whole-transcript editor's contents back into per-segment texts.

    One line per segment, in order. A different line count is REJECTED rather
    than re-segmented: re-segmenting would invent cue boundaries and throw away
    the timings that make the highlight work, which is precisely what the
    segment-wise design exists to prevent.
    """
    segments = build_segments(artifact, low_confidence_threshold=low_confidence_threshold)
    lines = str(full_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    # A trailing newline from a textarea is not an extra empty segment.
    while lines and not lines[-1].strip() and len(lines) > len(segments):
        lines.pop()
    if len(lines) != len(segments):
        raise TranscriptEditError(
            f"satir sayisi segment sayisiyla eslesmeli: {len(lines)} satir, "
            f"{len(segments)} segment. Zaman damgalari satirlara bagli oldugu "
            "icin satir ekleyip cikaramazsin; metni satir icinde duzenle.")
    return [(segment["id"], line) for segment, line in zip(segments, lines)]


# ----------------------------------------------------------------- revisions
def ensure_v2(artifact) -> dict:
    """Upgrade a v1 transcript in memory, without rewriting history it lacks.

    Projects created before revisions existed have only the current words. Their
    raw recognition is unrecoverable -- it was overwritten by the old correction
    path -- so `asr` is seeded from the current content and marked
    `reconstructed`, which is the honest label: it is the earliest state still
    on disk, not proof of what the recogniser said.
    """
    record = dict(artifact or {})
    if record.get("asr") is None:
        record["asr"] = {
            "words": [dict(word) for word in (record.get("words") or [])],
            "cues": [dict(cue) for cue in (record.get("cues") or [])],
            "text": record.get("text"),
            "language": record.get("language"),
            "whisper_model_size": record.get("whisper_model_size"),
            "source": record.get("source"),
            "captured_at": record.get("asr_captured_at") or _now(),
            "reconstructed": record.get("schema_version", 1) < SCHEMA_VERSION,
        }
    record.setdefault("revisions", [])
    record.setdefault("revision", len(record["revisions"]))
    record.setdefault("edited_segment_ids", [])
    record["schema_version"] = SCHEMA_VERSION
    return record


def append_revision(artifact, *, actor, kind, previous_sha256,
                    changed_segment_ids=()) -> dict:
    """Record who changed what, against which prior content."""
    record = dict(artifact)
    history = list(record.get("revisions") or [])
    history.append({
        "revision": len(history) + 1,
        "kind": kind,
        "actor": actor,
        "at": _now(),
        "previous_content_sha256": previous_sha256,
        "content_sha256": content_sha256(record),
        "changed_segment_ids": list(changed_segment_ids or []),
        "segment_count": len(record.get("cues") or []),
    })
    record["revisions"] = history[-100:]
    record["revision"] = history[-1]["revision"]
    merged = set(record.get("edited_segment_ids") or []) | set(changed_segment_ids or [])
    record["edited_segment_ids"] = sorted(merged)
    return record


def revision_summary(artifact) -> dict:
    """What the review screen shows about history and raw output."""
    record = artifact or {}
    asr = record.get("asr") or {}
    history = list(record.get("revisions") or [])
    return {
        "revision": record.get("revision") or 0,
        "revisions": [
            {"revision": entry.get("revision"), "kind": entry.get("kind"),
             "actor": entry.get("actor"), "at": entry.get("at"),
             "changed_segment_ids": entry.get("changed_segment_ids") or [],
             "content_sha256_16": (entry.get("content_sha256") or "")[:16],
             "previous_content_sha256_16": (entry.get("previous_content_sha256") or "")[:16]}
            for entry in history[-20:]
        ],
        "has_manual_edits": bool(history),
        "asr_available": bool(asr.get("words") or asr.get("cues")),
        "asr_reconstructed": bool(asr.get("reconstructed")),
        "asr_text": asr.get("text"),
        "asr_word_count": len(asr.get("words") or []),
    }
