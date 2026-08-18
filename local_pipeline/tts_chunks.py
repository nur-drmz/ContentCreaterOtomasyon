"""Resumable, per-chunk TTS for long narration.

THE DEFECT THIS REPLACES
------------------------
`run_google_tts` splits a script into ~4000 character pieces and then speaks
every piece INSIDE ONE routed call. Two consequences, both bad once a narration
is eighteen minutes long:

  1. If the twelfth chunk fails, the route retries the whole `call_fn`, which
     re-speaks chunks one to eleven. On a metered API that is eleven paid
     requests thrown away and eleven more spent to get back to where it was.
  2. Quota accounting sees ONE routed job, not the twenty-odd provider requests
     actually issued, so the ledger under-counts by an order of magnitude.

Here each chunk is its OWN routed call and its own persisted artifact. A restart
regenerates only what is missing or failed; a chunk whose text, voice or model
did not change is never spoken twice.

WHY THE HASH IS THE IDENTITY
----------------------------
A chunk is identified by the hash of (text, voice, model, provider, direction,
language). Editing chapter 3 changes the hash of exactly the chunks that carry
chapter 3's words, so those are invalidated and the rest are kept. A sequence
number alone could not do this: inserting a sentence shifts every later chunk
and would look like everything changed.

SHORT-FORM IS UNTOUCHED
-----------------------
`run_google_tts` keeps its behaviour and its callers. This module is additive;
the long-form workflow uses it, the Shorts pipeline does not.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_ARTIFACT = "results/long_form/tts_chunks.json"
CHUNK_DIRNAME = "results/long_form/tts"
SCHEMA_VERSION = 1

PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
STALE = "stale"
WAITING_RATE_LIMIT = "waiting_rate_limit"
WAITING_DAILY_QUOTA = "waiting_daily_quota"
BLOCKED_BILLING = "blocked_billing"
WAITING_STATES = frozenset({WAITING_RATE_LIMIT, WAITING_DAILY_QUOTA})

#: Sentence terminators for Turkish and English narration. Splitting is only ever
#: allowed at one of these or at a paragraph break: cutting mid-sentence makes
#: the seam audible, which is the one thing a listener always notices.
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|(?<=[.!?…][\"'’”])\s+")
_PARAGRAPH = re.compile(r"\n\s*\n")

#: Errors the provider will not resolve by being asked again. Retrying them
#: burns quota and hides a configuration problem behind a transient-looking
#: failure.
NON_RETRYABLE_STATUS = frozenset({400, 401, 403, 404})
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class TtsChunkError(ValueError):
    """A rejected chunk request, phrased for the operator."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(text) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def count_words(text) -> int:
    return len(re.findall(r"\S+", str(text or "")))


# ------------------------------------------------------------------ splitting
def split_narration(text, *, words_per_chunk: int, hard_max_chars: int = 4000) -> list:
    """Split narration into speakable pieces at SENTENCE or paragraph edges.

    `words_per_chunk` comes from the target seconds and the narration pace, so
    the caller decides the duration and this decides where it is safe to cut.

    A sentence longer than `hard_max_chars` on its own is emitted alone rather
    than split: a mid-sentence cut is worse than one oversized request, and the
    provider's own limit will report it honestly if it is genuinely too long.
    """
    body = str(text or "").strip()
    if not body:
        return []
    target = max(20, int(words_per_chunk))

    units: list[str] = []
    for paragraph in _PARAGRAPH.split(body):
        stripped = paragraph.strip()
        if not stripped:
            continue
        for sentence in _SENTENCE_END.split(stripped):
            cleaned = sentence.strip()
            if cleaned:
                units.append(cleaned)

    chunks: list[str] = []
    current: list[str] = []
    current_words = 0
    for unit in units:
        unit_words = count_words(unit)
        candidate_chars = len(" ".join(current + [unit]))
        if current and (current_words + unit_words > target
                        or candidate_chars > hard_max_chars):
            chunks.append(" ".join(current))
            current, current_words = [], 0
        current.append(unit)
        current_words += unit_words
    if current:
        chunks.append(" ".join(current))
    return chunks


def chunk_identity(*, text, voice, model, provider, narration_direction,
                   language_code) -> str:
    """Everything that changes the AUDIO, hashed together.

    Model and provider are included on purpose: the same words in the same voice
    from a different model are a different recording, and reusing the old file
    would silently mix two voices inside one narration.
    """
    payload = "\x00".join([
        str(text or ""), str(voice or ""), str(model or ""), str(provider or ""),
        str(narration_direction or ""), str(language_code or ""),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def chunk_id(sequence: int) -> str:
    return f"tts_{int(sequence):04d}"


# ------------------------------------------------------------------ manifest
def new_manifest(*, ownership=None) -> dict:
    record = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "long_form_tts_chunks",
        "status": "review_required",
        "not_uploaded": True,
        "chunks": [],
        "combined": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    if ownership is not None:
        record.update(ownership.to_dict())
    return record


def plan_chunks(chapters, *, words_per_chunk: int, voice: str, model: str,
                provider: str, narration_direction: str = "",
                language_code: str = "tr-TR") -> list:
    """Turn approved chapters into an ordered chunk plan.

    Chunks never straddle a chapter boundary. That costs a few extra requests
    and buys two things: a chapter edit invalidates only its own audio, and the
    B-roll mapping can line audio up with chapters without guessing.
    """
    planned = []
    sequence = 0
    for chapter in (chapters or []):
        chapter_id = str(chapter.get("id") or f"ch_{chapter.get('index', 0):02d}")
        for piece in split_narration(chapter.get("text") or "",
                                     words_per_chunk=words_per_chunk):
            sequence += 1
            planned.append({
                "id": chunk_id(sequence),
                "sequence": sequence,
                "chapter_id": chapter_id,
                "chapter_index": chapter.get("index"),
                "text": piece,
                "word_count": count_words(piece),
                "input_text_sha256": sha256_text(piece),
                "identity_sha256": chunk_identity(
                    text=piece, voice=voice, model=model, provider=provider,
                    narration_direction=narration_direction,
                    language_code=language_code),
                "provider": provider,
                "model": model,
                "voice": voice,
                "narration_direction": narration_direction or None,
                "language_code": language_code,
            })
    return planned


def merge_plan(manifest: dict, planned: list) -> dict:
    """Apply a new plan while KEEPING audio that is still correct.

    This is the whole point of the module. A chunk whose identity hash matches
    an existing completed chunk keeps its file, its duration and its hash; every
    other chunk is (re)queued. Nothing is deleted here -- a superseded file stays
    on disk until an explicit cleanup, so a mistaken edit is recoverable.
    """
    manifest = dict(manifest or new_manifest())
    existing = {}
    for chunk in manifest.get("chunks") or []:
        identity = chunk.get("identity_sha256")
        if identity:
            current = existing.get(identity)
            # A completed artifact is always the strongest reusable record.
            if current is None or chunk.get("status") == COMPLETED:
                existing[identity] = chunk

    merged = []
    reused = 0
    for entry in planned:
        previous = existing.get(entry["identity_sha256"])
        if previous is not None:
            carried = dict(previous)
            # Position may have moved even though the audio did not.
            carried.update({"id": entry["id"], "sequence": entry["sequence"],
                            "chapter_id": entry["chapter_id"],
                            "chapter_index": entry["chapter_index"],
                            "updated_at": _now()})
            # A process crash can leave a chunk marked running. It was never
            # committed as completed, so a restart safely queues that one
            # chunk again. Explicit quota waits, failures and attempt counters
            # are preserved across a re-plan.
            if carried.get("status") == RUNNING:
                carried["status"] = PENDING
            merged.append(carried)
            if carried.get("status") == COMPLETED:
                reused += 1
            continue
        merged.append({
            **entry,
            "status": PENDING,
            "attempts": 0,
            "error": None,
            "error_kind": None,
            "output_path": None,
            "output_sha256": None,
            "duration_sec": None,
            "provider_latency_sec": None,
            "created_at": _now(),
            "updated_at": _now(),
        })

    planned_identities = {entry["identity_sha256"] for entry in planned}
    superseded = [dict(chunk, status=STALE, updated_at=_now())
                  for chunk in (manifest.get("chunks") or [])
                  if chunk.get("identity_sha256") not in planned_identities
                  and chunk.get("status") == COMPLETED]

    manifest["chunks"] = merged
    manifest["superseded"] = (manifest.get("superseded") or []) + superseded
    manifest["reused_count"] = reused
    manifest["updated_at"] = _now()
    # The combined file describes the OLD chunk list; keeping it would let a
    # stale narration be served for an edited script.
    if reused != len(merged) or superseded:
        manifest["combined"] = None
    return manifest


def pending_chunks(manifest) -> list:
    """Only what still needs a provider call, in order."""
    return [chunk for chunk in ((manifest or {}).get("chunks") or [])
            if chunk.get("status") in (PENDING, FAILED, RUNNING)
            or chunk.get("status") in WAITING_STATES]


def completed_chunks(manifest) -> list:
    return [chunk for chunk in ((manifest or {}).get("chunks") or [])
            if chunk.get("status") == COMPLETED]


def find_chunk(manifest, chunk_id_value: str):
    for index, chunk in enumerate((manifest or {}).get("chunks") or []):
        if chunk.get("id") == chunk_id_value:
            return index, chunk
    raise TtsChunkError(f"bilinmeyen TTS parcasi: {chunk_id_value!r}")


def update_chunk(manifest: dict, chunk_id_value: str, **fields) -> dict:
    manifest = dict(manifest or new_manifest())
    chunks = [dict(chunk) for chunk in (manifest.get("chunks") or [])]
    index, _current = find_chunk({"chunks": chunks}, chunk_id_value)
    chunks[index].update(fields)
    chunks[index]["updated_at"] = _now()
    manifest["chunks"] = chunks
    manifest["updated_at"] = _now()
    return manifest


def mark_running(manifest: dict, chunk_id_value: str) -> dict:
    index, current = find_chunk(manifest, chunk_id_value)
    return update_chunk(manifest, chunk_id_value, status=RUNNING,
                        attempts=int(current.get("attempts") or 0) + 1,
                        error=None, error_kind=None, resume_at=None,
                        wait_reason=None)


def mark_completed(manifest: dict, chunk_id_value: str, *, output_path,
                   output_sha256, duration_sec, latency_sec=None) -> dict:
    return update_chunk(manifest, chunk_id_value, status=COMPLETED,
                        output_path=str(output_path),
                        output_sha256=output_sha256,
                        duration_sec=round(float(duration_sec or 0.0), 3),
                        provider_latency_sec=latency_sec, error=None,
                        error_kind=None, resume_at=None, wait_reason=None)


def mark_failed(manifest: dict, chunk_id_value: str, *, error, error_kind=None) -> dict:
    # Sanitized: the message a person can act on, never a provider payload.
    return update_chunk(manifest, chunk_id_value, status=FAILED,
                        error=str(error)[:400], error_kind=error_kind,
                        resume_at=None, wait_reason=None)


def mark_waiting(manifest: dict, chunk_id_value: str, *, state: str,
                 resume_at, reason: str) -> dict:
    """Persist a provider quota pause without pretending the chunk failed."""
    if state not in WAITING_STATES:
        raise TtsChunkError(f"gecersiz TTS bekleme durumu: {state!r}")
    return update_chunk(
        manifest, chunk_id_value, status=state,
        resume_at=str(resume_at or "") or None,
        wait_reason=str(reason or "")[:400] or None,
        error=None, error_kind=None)


def mark_billing_blocked(manifest: dict, chunk_id_value: str, *, reason: str) -> dict:
    """Persist a non-timed provider block; only an operator may reset it."""
    return update_chunk(
        manifest, chunk_id_value, status=BLOCKED_BILLING,
        resume_at=None, wait_reason=str(reason or "")[:400] or None,
        error=None, error_kind="billing_or_quota_configuration")


def reset_billing_blocks(manifest: dict) -> dict:
    """Requeue blocked chunks after the operator says billing was fixed."""
    result = dict(manifest or new_manifest())
    for chunk in list(result.get("chunks") or []):
        if chunk.get("status") == BLOCKED_BILLING:
            result = update_chunk(
                result, chunk["id"], status=PENDING, resume_at=None,
                wait_reason=None, error=None, error_kind=None)
    return result


def classify_error(status_code=None, exception=None) -> dict:
    """Is this worth retrying? Decided from the status code, not the message.

    401/403 mean the credential is wrong; asking again with the same credential
    is guaranteed to fail and looks, in a log, exactly like a service outage.
    """
    code = None
    try:
        code = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        code = None
    if code is None and exception is not None:
        found = re.search(r"\b(4\d\d|5\d\d)\b", str(exception))
        if found:
            code = int(found.group(1))
    if code in NON_RETRYABLE_STATUS:
        kind = {400: "invalid_request", 401: "unauthenticated",
                403: "permission_denied", 404: "not_found"}[code]
        return {"status_code": code, "retryable": False, "kind": kind}
    if code in RETRYABLE_STATUS:
        kind = "rate_limited" if code == 429 else "server_error"
        return {"status_code": code, "retryable": True, "kind": kind}
    if code is not None:
        return {"status_code": code, "retryable": False, "kind": "unknown_status"}
    return {"status_code": None, "retryable": True, "kind": "transient"}


# ------------------------------------------------------------------- writing
def atomic_write_bytes(path, payload: bytes) -> None:
    """Write into the final location or not at all.

    A half-written WAV that survives a crash is worse than none: it has a size,
    a hash and a path, so every later check treats it as a real recording.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(target.parent),
                                         prefix=".tts_", suffix=".part")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def chunk_output_path(base_dir, chunk: dict) -> Path:
    """Named by identity, so the same audio is never written twice and a changed
    chunk cannot overwrite the recording it replaced."""
    identity = str(chunk.get("identity_sha256") or "")[:16]
    return Path(base_dir) / f"{chunk['id']}_{identity}.wav"


# --------------------------------------------------------------- concatenation
def ordered_ready(manifest) -> list:
    """Completed chunks in sequence order, or [] when any are missing.

    All-or-nothing on purpose: concatenating "what we have" produces a narration
    with a hole in it that sounds finished.
    """
    chunks = sorted((manifest or {}).get("chunks") or [],
                    key=lambda chunk: int(chunk.get("sequence") or 0))
    if not chunks:
        return []
    if any(chunk.get("status") != COMPLETED or not chunk.get("output_path")
           for chunk in chunks):
        return []
    return chunks


def concat_readiness(manifest) -> dict:
    chunks = (manifest or {}).get("chunks") or []
    missing = [chunk["id"] for chunk in chunks if chunk.get("status") != COMPLETED]
    total = sum(float(chunk.get("duration_sec") or 0.0)
                for chunk in chunks if chunk.get("status") == COMPLETED)
    return {
        "ready": bool(chunks) and not missing,
        "missing": missing,
        "chunk_count": len(chunks),
        "completed_count": len(chunks) - len(missing),
        "total_duration_sec": round(total, 2),
    }


def summarise(manifest) -> dict:
    """View model for the chunk list panel."""
    chunks = sorted((manifest or {}).get("chunks") or [],
                    key=lambda chunk: int(chunk.get("sequence") or 0))
    readiness = concat_readiness(manifest)
    return {
        "chunks": [
            {"id": chunk.get("id"), "sequence": chunk.get("sequence"),
             "chapter_id": chunk.get("chapter_id"),
             "chapter_index": chunk.get("chapter_index"),
             "status": chunk.get("status"), "attempts": chunk.get("attempts", 0),
             "word_count": chunk.get("word_count"),
             "duration_sec": chunk.get("duration_sec"),
             "voice": chunk.get("voice"), "model": chunk.get("model"),
             "provider": chunk.get("provider"),
             "error": chunk.get("error"), "error_kind": chunk.get("error_kind"),
             "resume_at": chunk.get("resume_at"),
             "wait_reason": chunk.get("wait_reason"),
             # Basename only: enough for the media endpoint, without exposing
             # a host path in the view model.
             "media_name": (Path(chunk.get("output_path")).name
                            if chunk.get("output_path") else None),
             "output_sha256_16": (chunk.get("output_sha256") or "")[:16],
             "text_preview": (chunk.get("text") or "")[:120],
             "updated_at": chunk.get("updated_at")}
            for chunk in chunks
        ],
        "readiness": readiness,
        "combined": (manifest or {}).get("combined"),
        "reused_count": (manifest or {}).get("reused_count", 0),
        "superseded_count": len((manifest or {}).get("superseded") or []),
        # Gemini TTS is a preview model, so its output is never auto-approved.
        "approval_policy": ("Gemini TTS PREVIEW modeldir; cikti her zaman "
                            "review_required kalir. Yedek saglayiciyla uretilen "
                            "bir parca onceki onayi DEVRALMAZ."),
        "not_uploaded": True,
    }
