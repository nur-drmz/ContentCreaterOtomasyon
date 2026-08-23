"""One resumable TTS pass, shared by every workflow that narrates.

WHAT IS GENERIC AND WHAT IS NOT
-------------------------------
Speaking a list of chunks is the same job everywhere: check the ledger BEFORE
spending anything, mark the chunk running, make exactly one routed call, tell a
quota wait apart from a real failure, persist after every chunk, and stop
between chunks when cancelled. None of that depends on which workflow asked.

What does depend on the workflow is where the manifest lives and how the
finished pass is recorded against a stage. Those stay with the caller.

WHY PERSIST AFTER EVERY CHUNK
-----------------------------
Because the pass is meant to be interrupted. A daily quota can run out on chunk
9 of 40, and the only thing that makes the next attempt cheap is that chunks 1-8
are already on disk with their identity hashes. Buffering the manifest in memory
and writing once at the end would throw that away in exactly the case it matters.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from local_pipeline import google_ai, stage_machine, tts_chunks

from . import quota_signals


def run_chunk_pass(studio, *, manifest, manifest_artifact: str, audio_dir,
                   pending, actor: str = "console", tracker=None,
                   cancel_check=None) -> dict:
    """Speak the pending chunks, persisting after each one.

    `studio` supplies `_write`, `log_event`, `ledger()` and `service`. Returns
    what happened; recording it against a stage is the caller's decision,
    because only the caller knows which stage it was.
    """
    client = studio.service._build_google_client()
    # One ledger for the whole pass: each chunk is a separate request and has
    # to be counted as one, not amortised across the job.
    ledger = studio.ledger()
    produced, failed = 0, 0
    waiting = None

    for chunk in pending:
        if cancel_check is not None and cancel_check():
            # Between chunks, never inside one: a half-spoken chunk would have
            # to be discarded anyway, so stopping mid-call wastes the request.
            studio.log_event("tts_chunk.failed", chunk_id=chunk.get("id"),
                             error_kind="cancelled", retryable=True)
            break

        tokens = google_ai.estimate_input_tokens(chunk.get("text") or "")
        quota = quota_signals.tts_quota_wait(ledger, chunk["model"], tokens)
        if not quota.get("ready") and quota.get("state"):
            manifest = tts_chunks.mark_waiting(
                manifest, chunk["id"], state=quota["chunk_state"],
                resume_at=quota["resume_at"], reason=quota["reason"])
            studio._write(manifest_artifact, manifest, actor=actor)
            waiting = quota
            studio.log_event("tts_chunk.waiting", chunk_id=chunk["id"],
                             sequence=chunk.get("sequence"),
                             model=chunk.get("model"),
                             wait_state=quota["state"],
                             resume_at=quota["resume_at"])
            break
        if not quota.get("ready"):
            # A disabled or missing model: no honest resume time exists, so this
            # is a failure a person has to act on, not something to wait out.
            manifest = tts_chunks.mark_failed(
                manifest, chunk["id"],
                error=quota.get("reason") or "TTS modeli kullanilamiyor",
                error_kind="model_unavailable")
            studio._write(manifest_artifact, manifest, actor=actor)
            failed += 1
            break

        manifest = tts_chunks.mark_running(manifest, chunk["id"])
        studio._write(manifest_artifact, manifest, actor=actor)
        attempt = int(tts_chunks.find_chunk(manifest, chunk["id"])[1]
                      .get("attempts") or 1)
        studio.log_event(
            "tts_chunk.started" if attempt == 1 else "tts_chunk.retried",
            chunk_id=chunk["id"], sequence=chunk.get("sequence"),
            chapter_id=chunk.get("chapter_id"), attempt=attempt,
            provider=chunk.get("provider"), model=chunk.get("model"))
        if tracker is not None:
            tracker.count(produced, len(pending), unit="parca",
                          message=f"TTS parcasi {chunk.get('sequence')}")

        started = time.time()
        try:
            result = google_ai.run_google_tts_chunk(
                chunk["text"],
                tts_chunks.chunk_output_path(audio_dir, chunk),
                voice=chunk["voice"], language_code=chunk["language_code"],
                narration_direction=chunk.get("narration_direction") or "",
                client=client, ledger=ledger, mode="explicit",
                overrides={"tts": [chunk["model"]]},
                enable_model_fallback=False)
        except Exception as exc:  # noqa: BLE001 - recorded, never fabricated
            kind = tts_chunks.classify_error(exception=exc)
            manifest = tts_chunks.mark_failed(
                manifest, chunk["id"], error=f"{type(exc).__name__}: {exc}",
                error_kind=kind["kind"])
            studio._write(manifest_artifact, manifest, actor=actor)
            studio.log_event("tts_chunk.failed", chunk_id=chunk["id"],
                             attempt=attempt, error_kind=kind["kind"],
                             retryable=kind["retryable"],
                             latency_sec=round(time.time() - started, 3))
            failed += 1
            if not kind["retryable"]:
                break
            continue

        latency = round(time.time() - started, 3)
        if result.get("status") != "ok" or not result.get("audio_path"):
            # The route reports "all models failed" without the original status,
            # so the per-attempt detail is where 401 vs 503 actually lives.
            attempts_blob = json.dumps(result.get("provider_attempts") or [],
                                       ensure_ascii=False, default=str)
            kind = tts_chunks.classify_error(
                status_code=result.get("status_code"),
                exception=f"{result.get('status_reason')} {attempts_blob}")
            quota_kind = quota_signals.quota_kind_from_result(result)
            if quota_kind:
                if quota_kind == stage_machine.BLOCKED_BILLING:
                    quota = {
                        "ready": False, "state": stage_machine.BLOCKED_BILLING,
                        "chunk_state": tts_chunks.BLOCKED_BILLING,
                        "resume_at": None,
                        "reason": (result.get("status_reason")
                                   or "plan/billing veya kota yapilandirmasi engeli"),
                        "model": chunk["model"],
                    }
                else:
                    quota = quota_signals.tts_quota_wait(
                        ledger, chunk["model"], tokens)
                # A live 429 is authoritative even when the local ledger still
                # believes there is room and the payload omitted a retry time.
                if (quota_kind != stage_machine.BLOCKED_BILLING
                        and quota.get("ready")):
                    resume_at = datetime.now(timezone.utc) + timedelta(seconds=60)
                    daily = quota_kind == stage_machine.WAITING_DAILY_QUOTA
                    quota = {
                        "ready": False, "state": quota_kind,
                        "chunk_state": (tts_chunks.WAITING_DAILY_QUOTA if daily
                                        else tts_chunks.WAITING_RATE_LIMIT),
                        "resume_at": (
                            google_ai.next_pacific_reset(resume_at).isoformat()
                            if daily else resume_at.isoformat()),
                        "reason": (result.get("status_reason")
                                   or "saglayici kota siniri"),
                        "model": chunk["model"],
                    }
                if quota_kind == stage_machine.BLOCKED_BILLING:
                    manifest = tts_chunks.mark_billing_blocked(
                        manifest, chunk["id"], reason=quota["reason"])
                else:
                    manifest = tts_chunks.mark_waiting(
                        manifest, chunk["id"], state=quota["chunk_state"],
                        resume_at=quota["resume_at"], reason=quota["reason"])
                studio._write(manifest_artifact, manifest, actor=actor)
                waiting = quota
                studio.log_event(
                    ("tts_chunk.blocked_billing"
                     if quota_kind == stage_machine.BLOCKED_BILLING
                     else "tts_chunk.waiting"),
                    chunk_id=chunk["id"], attempt=attempt,
                    model=chunk.get("model"), wait_state=quota["state"],
                    resume_at=quota["resume_at"], latency_sec=latency)
                break
            manifest = tts_chunks.mark_failed(
                manifest, chunk["id"],
                error=result.get("status_reason") or "TTS basarisiz",
                error_kind=kind["kind"])
            studio._write(manifest_artifact, manifest, actor=actor)
            studio.log_event("tts_chunk.failed", chunk_id=chunk["id"],
                             attempt=attempt, error_kind=kind["kind"],
                             latency_sec=latency,
                             provider=chunk.get("provider"))
            failed += 1
            if not kind["retryable"]:
                break
            continue

        manifest = tts_chunks.mark_completed(
            manifest, chunk["id"], output_path=result["audio_path"],
            output_sha256=result["output_audio_sha256"],
            duration_sec=result.get("duration_sec") or 0.0,
            latency_sec=latency)
        studio._write(manifest_artifact, manifest, actor=actor)
        studio.log_event("tts_chunk.completed", chunk_id=chunk["id"],
                         sequence=chunk.get("sequence"), attempt=attempt,
                         duration_sec=result.get("duration_sec"),
                         latency_sec=latency, provider=chunk.get("provider"),
                         model=result.get("model_used") or chunk.get("model"))
        produced += 1

    return {
        "manifest": manifest,
        "produced": produced,
        "failed": failed,
        "waiting": waiting,
        "readiness": tts_chunks.concat_readiness(manifest),
    }


def concat_wav_chunks(ordered, target) -> dict:
    """Join validated chunks in sequence order and validate the whole.

    Refuses on a format mismatch rather than joining anyway: a narration whose
    sample rate changes mid-file still plays, badly, and the damage would only
    be noticed after it had been rendered into a finished video.

    Returns the combined file's facts. It deliberately does NOT judge whether
    the duration is acceptable -- "long enough" means 15-20 minutes to one
    workflow and whatever the brief said to another, so that judgement belongs
    to the caller that knows which target applies.
    """
    import os
    import wave

    params = None
    frames = []
    for chunk in ordered:
        with wave.open(str(chunk["output_path"]), "rb") as handle:
            if params is None:
                params = handle.getparams()
            elif (handle.getnchannels(), handle.getsampwidth(),
                  handle.getframerate()) != (params.nchannels, params.sampwidth,
                                             params.framerate):
                raise ValueError(
                    f"{chunk['id']} farkli ses formatinda; birlestirilemez")
            frames.append(handle.readframes(handle.getnframes()))

    # Written to a sibling and renamed, so an interrupted join never leaves a
    # truncated file that looks like a finished narration.
    temp = target.with_suffix(".part")
    with wave.open(str(temp), "wb") as handle:
        handle.setnchannels(params.nchannels)
        handle.setsampwidth(params.sampwidth)
        handle.setframerate(params.framerate)
        handle.writeframes(b"".join(frames))
    os.replace(temp, target)

    validation = google_ai.validate_audio_file(target)
    duration = (validation.get("details") or {}).get("duration_sec") or 0.0
    return {
        "path": str(target),
        "sha256": tts_chunks.sha256_file(target),
        "duration_sec": round(float(duration), 2),
        "chunk_count": len(ordered),
        "chunk_order": [chunk["id"] for chunk in ordered],
        "validation": validation,
    }
