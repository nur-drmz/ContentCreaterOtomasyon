#!/usr/bin/env python
"""Offline verification for the human-labelled lyrics dataset ledger."""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import wave
from pathlib import Path

import numpy as np

from local_pipeline import lyrics_training

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


def fake_extract(_source, destination, _start, duration):
    rate = 16000
    samples = (np.zeros(round(rate * duration)) * 32767).astype(np.int16)
    with wave.open(str(destination), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.tobytes())


def main():
    temp = Path(tempfile.mkdtemp(prefix="lyrics_training_"))
    source = temp / "source.bin"
    source.write_bytes(b"offline fake audio")
    dataset = temp / "dataset"
    digest = hashlib.sha256(b"transcript-v1").hexdigest()
    first = lyrics_training.record_example(
        dataset, source_audio=source, source_group="project-a/source-1",
        segment_id="seg_0001", start_sec=2.0, end_sec=4.0,
        label="dialogue", transcript_sha256=digest, extractor=fake_extract)
    check((dataset / first["clip"]).is_file(), "clip-created")
    check(first["audio"]["sample_rate_hz"] == 16000
          and first["audio"]["channels"] == 1, "clip-format")
    check(first["clip_start_sec"] == 1.5 and first["clip_end_sec"] == 4.5,
          "context-window")
    check(first["human_confirmed"] is True, "human-label-required")

    manifest_text = (dataset / "manifest.json").read_text(encoding="utf-8")
    check("project-a/source-1" not in manifest_text, "source-group-not-disclosed")
    check(str(source.resolve()) not in manifest_text, "absolute-path-not-stored")
    check("transcript-v1" not in manifest_text, "transcript-text-not-stored")

    # Re-labelling is a correction, not a contradictory duplicate.
    changed = lyrics_training.record_example(
        dataset, source_audio=source, source_group="project-a/source-1",
        segment_id="seg_0001", start_sec=2.0, end_sec=4.0,
        label="sung_lyrics", transcript_sha256=digest,
        label_origin="human_reject_suggestion", suggested_label="dialogue",
        extractor=fake_extract)
    manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
    check(first["id"] == changed["id"] and len(manifest["examples"]) == 1,
          "relabel-updates-same-example")
    check(manifest["examples"][0]["label"] == "sung_lyrics",
          "corrected-label-persisted")

    second = lyrics_training.record_example(
        dataset, source_audio=source, source_group="project-a/source-1",
        segment_id="seg_0002", start_sec=5.0, end_sec=6.0,
        label="music_without_words", transcript_sha256=digest,
        extractor=fake_extract)
    check(first["split"] == second["split"], "same-source-same-split")
    summary = lyrics_training.dataset_summary(dataset)
    check(summary["example_count"] == 2
          and summary["labels"]["sung_lyrics"] == 1
          and summary["labels"]["music_without_words"] == 1,
          "summary-counts", str(summary))
    check(summary["source_group_count"] == 1, "source-group-count")
    check(summary["ready_for_training"] is False, "minimum-size-gate")

    rejected = 0
    for kwargs in (
        {"label": "provider_guess"},
        {"start_sec": 4.0, "end_sec": 4.0},
        {"transcript_sha256": "bad"},
    ):
        params = dict(source_audio=source, source_group="project-b/source",
                      segment_id="seg_0001", start_sec=1.0, end_sec=2.0,
                      label="dialogue", transcript_sha256=digest,
                      extractor=fake_extract)
        params.update(kwargs)
        try:
            lyrics_training.record_example(dataset, **params)
        except lyrics_training.LyricsTrainingError:
            rejected += 1
    check(rejected == 3, "invalid-examples-rejected")

    shutil.rmtree(temp, ignore_errors=True)
    print(f"LYRICS_TRAINING_OK ({CHECKS - len(FAILURES)}/{CHECKS})")
    if FAILURES:
        for failure in FAILURES:
            print(" -", failure)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
