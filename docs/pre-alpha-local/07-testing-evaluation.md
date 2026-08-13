# Local Pre-Alpha Testing and Evaluation

**Status:** Approved planning baseline

## 1. GPU PoC gate

Use the current English MP4:

```text
G:\Youtube kaynak videolar\
THE ABSOLUTE BEST OF DAEMON TARGARYEN - ABC CINEMA (1080p).mp4
```

Verified with native Windows FFprobe 8.1.2:

- file size: 214,073,347 bytes (approximately 204.16 MiB);
- duration: 1328.599 seconds (approximately 22:08.6);
- video: H.264, 1920×1080, 24000/1001 FPS;
- audio: English AAC, stereo, 44.1 kHz;
- embedded subtitle stream: none.

This fixture therefore exercises the local Whisper fallback.

Record:

- dependency/model revisions;
- stage wall time;
- CPU and RAM peak;
- GPU utilization and VRAM peak;
- generated artifact sizes;
- transcript usability;
- scene count;
- valid/invalid VLM JSON count.

Native environment baseline established before the model run:

- Windows 11 Pro 64-bit;
- Python 3.12.10;
- uv 0.11.32;
- FFmpeg/FFprobe 8.1.2;
- NVIDIA driver 610.74;
- PyTorch 2.7.1+cu128 with CUDA available;
- CTranslate2 4.8.1 with one CUDA device;
- faster-whisper 1.2.1.

Pass when:

- no GPU OOM occurs;
- VLM JSON validity is at least 95% after one allowed repair retry;
- timestamps support scene preview/caption generation;
- total processing time is shorter than source duration;
- rerunning reuses completed checkpoints.

PoC failure blocks Django feature implementation until documented and resolved.

## 2. Unit tests

- Content-hash and idempotency-key generation.
- Hot Moment component and total scores.
- Adaptive 20/30/40 candidate count.
- Overlap suppression.
- Prompt formula (`0.75 base + 0.25 relevance`).
- Character allowlist enforcement and spoiler enum.
- Timeline trim/order/lock invariants.
- Job transitions and resume selection.
- Retention, trash expiry and disk thresholds.
- Path containment and traversal rejection.

## 3. Integration tests

- Source discovery/registration without copying, moving or renaming the file.
- Invalid/oversized/corrupt source rejection.
- Embedded subtitle → sidecar SRT → Whisper priority.
- FFmpeg proxy and 720×1280 blurred-background render.
- Worker shutdown/restart after each checkpoint.
- Duplicate-source analysis reuse.
- Caption sidecar and optional burn-in.
- SQLite backup and restore.

## 4. End-to-end scenario

Launcher → project creation → rights acknowledgement → source import → preflight
→ analysis → moment review → optional focus prompt → select/reorder/trim/lock →
timeline proposal → caption toggle → rough-cut render → close/reopen project.

## 5. AI quality set

Use at least three legally usable English sources:

- dialogue-heavy;
- action-heavy;
- emotional or narrative-turn-heavy.

The user labels candidate moments as `strong`, `usable`, or `reject`. The release
gate is at least five `strong` moments in the Top 20 for every source.

Also record:

- selection rate;
- duplicate/near-duplicate rate;
- spoiler-label errors;
- incorrect character attributions;
- number and magnitude of manual trims;
- prompt re-ranking improvement;
- VLM failure/repair rate.

Evaluation inputs, pipeline configuration and user labels are versioned so
model changes can be compared.

## 6. Performance gate

On the approved Ryzen 9 5950X / RTX 3080 / 32 GB machine:

- a 180-minute source must reach Moment Explorer in under 180 minutes;
- only one GPU model is resident at a time;
- preflight requires at least 8 GB free VRAM;
- temporary files must respect the G: disk guards.
