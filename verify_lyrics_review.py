#!/usr/bin/env python
"""Offline tests for conservative song/lyrics subtitle suggestions."""
from __future__ import annotations

import json
import math
import shutil
import struct
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from local_pipeline import lyrics_review, transcript_edit  # noqa: E402

CHECKS = 0
FAILURES = []


def check(condition, label, message=""):
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"[{label}] OK")
    else:
        FAILURES.append(f"{label}: {message}")
        print(f"[{label}] FAILED - {message}")


def write_wav(path, samples, rate=16000):
    samples = np.clip(np.asarray(samples), -1.0, 1.0)
    pcm = (samples * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())


def artifact(cues):
    words = []
    for cue in cues:
        tokens = cue["text"].split()
        span = (cue["end"] - cue["start"]) / max(1, len(tokens))
        for index, token in enumerate(tokens):
            words.append({"text": token, "start": cue["start"] + index * span,
                          "end": cue["start"] + (index + 1) * span,
                          "probability": 0.97})
    return {"cues": cues, "words": words}


def main():
    temp = Path(tempfile.mkdtemp(prefix="lyrics_review_"))
    rate, seconds = 16000, 8
    t = np.arange(rate * seconds) / rate
    # Tonal carrier with a regular 2 Hz amplitude pulse: intentionally strong
    # musical evidence for deterministic classifier tests.
    pulse = 0.35 + 0.65 * (np.sin(2 * math.pi * 2 * t) > 0)
    music = 0.45 * np.sin(2 * math.pi * 220 * t) * pulse
    music_path = temp / "music.wav"
    write_wav(music_path, music)
    rng = np.random.default_rng(7)
    sparse = rng.normal(0, 0.15, rate * seconds)
    mask = np.zeros(rate * seconds)
    for start in (0.4, 2.4, 5.2):
        mask[int(start * rate):int((start + 0.65) * rate)] = 1
    speech_like_path = temp / "speech_like.wav"
    write_wav(speech_like_path, sparse * mask)

    music_profile = lyrics_review.audio_music_profile(music_path)
    speech_profile = lyrics_review.audio_music_profile(speech_like_path)
    check(music_profile["available"], "music-profile-available")
    check(music_profile["music_evidence"] > speech_profile["music_evidence"],
          "tonal-pulsed-higher-than-sparse-noise",
          f"{music_profile['music_evidence']} vs {speech_profile['music_evidence']}")

    source = artifact([
        {"start": 0.0, "end": 1.5, "text": "[Music]"},
        {"start": 1.6, "end": 3.0, "text": "la la la oh"},
        {"start": 3.1, "end": 4.4, "text": "we go home tonight"},
        {"start": 4.5, "end": 5.8, "text": "we go home tonight"},
        {"start": 6.0, "end": 7.5, "text": "Where is the northern gate?"},
    ])
    review = lyrics_review.analyse(source, audio_path=music_path)
    by_id = {row["id"]: row for row in review["segments"]}
    check(by_id["seg_0001"]["recommendation"] == "exclude_suggested",
          "explicit-music-marker-suggested")
    check(by_id["seg_0002"]["recommendation"] == "exclude_suggested",
          "vocalisation-plus-audio-suggested")
    check(by_id["seg_0003"]["recommendation"] in {"review", "exclude_suggested"}
          and "ayni satir tekrarlaniyor" in by_id["seg_0003"]["reasons"],
          "repeated-lyric-review-evidence")
    check(review["automatic_exclusion_applied"] is False
          and review["human_confirmation_required"] is True,
          "suggestions-never-auto-apply")
    check(all("text" not in row for row in review["segments"]),
          "review-artifact-does-not-copy-caption-text")

    dialogue = artifact([
        {"start": 0.0, "end": 1.2, "text": "Close the door."},
        {"start": 2.5, "end": 3.8, "text": "Did anyone follow you?"},
        {"start": 5.5, "end": 7.0, "text": "No, the road was empty."},
    ])
    dialogue_review = lyrics_review.analyse(dialogue, audio_path=speech_like_path)
    check(not dialogue_review["suggested_segment_ids"],
          "ordinary-dialogue-not-excluded", str(dialogue_review["segments"]))

    manually_hidden = artifact([
        {"start": 0.0, "end": 1.0, "text": "ordinary words",
         "excluded_from_subtitles": True},
    ])
    hidden_review = lyrics_review.analyse(manually_hidden)
    check(hidden_review["segments"][0]["recommendation"] == "already_excluded",
          "manual-decision-authoritative")

    changed = json.loads(json.dumps(dialogue))
    old_review = lyrics_review.analyse(dialogue)
    changed["cues"][0]["text"] = "Changed text."
    check(not lyrics_review.is_stale(old_review, dialogue), "fresh-review-not-stale")
    check(lyrics_review.is_stale(old_review, changed), "text-edit-invalidates-review")

    prompt = lyrics_review.provider_prompt(dialogue)
    check("Close the door" not in prompt and "seg_0001" in prompt,
          "provider-prompt-has-timestamps-not-caption-text")
    schema = lyrics_review.provider_response_schema()
    labels = schema["properties"]["segments"]["items"]["properties"]["label"]["enum"]
    check(set(labels) == set(lyrics_review.PROVIDER_LABELS),
          "provider-schema-labels-closed")
    provider_result = {"segments": [
        {"id": "seg_0001", "label": "dialogue", "confidence": 0.98,
         "reason": "spoken voice"},
        {"id": "seg_0002", "label": "sung_lyrics", "confidence": 0.94,
         "reason": "melodically sung words"},
        {"id": "seg_0003", "label": "music_without_words", "confidence": 0.9,
         "reason": "instrumental bed"},
    ]}
    merged = lyrics_review.merge_provider_result(
        dialogue_review, dialogue, provider_result,
        provider="google_api", model="fake-audio-model")
    merged_rows = {row["id"]: row for row in merged["segments"]}
    check(merged_rows["seg_0001"]["recommendation"] == "dialogue_likely",
          "provider-keeps-confident-dialogue")
    check(merged_rows["seg_0002"]["recommendation"] == "exclude_suggested",
          "provider-suggests-confident-singing")
    check(merged_rows["seg_0003"]["recommendation"] == "review",
          "music-without-words-still-needs-review")
    check(merged["automatic_exclusion_applied"] is False,
          "provider-result-never-auto-applies")
    try:
        bad = json.loads(json.dumps(provider_result))
        bad["segments"][0]["id"] = "seg_9999"
        lyrics_review.merge_provider_result(
            dialogue_review, dialogue, bad, provider="fake", model="fake")
        rejected_unknown = False
    except lyrics_review.LyricsReviewError:
        rejected_unknown = True
    check(rejected_unknown, "provider-unknown-segment-rejected")

    # The existing exclusion engine must still preserve text/timing and only
    # change visibility; the classifier is advisory, not a parallel editor.
    applied = transcript_edit.apply_segment_exclusions(
        source, review["suggested_segment_ids"], excluded=True)
    check(applied["changed_segment_ids"] == review["suggested_segment_ids"],
          "suggestions-compatible-with-existing-exclusion-engine")
    check(len(applied["cues"]) == len(source["cues"])
          and [c["text"] for c in applied["cues"]]
          == [c["text"] for c in source["cues"]],
          "applying-visibility-preserves-caption-content")

    shutil.rmtree(temp, ignore_errors=True)
    print(f"LYRICS_REVIEW_OK ({CHECKS - len(FAILURES)}/{CHECKS})")
    if FAILURES:
        for failure in FAILURES:
            print(" -", failure)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
