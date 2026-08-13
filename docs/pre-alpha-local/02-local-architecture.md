# Local Pre-Alpha Architecture

**Status:** Approved planning baseline

## 1. Runtime topology

```text
Windows 11
├── Browser -> http://127.0.0.1:<port>
├── PowerShell one-click launcher
├── G:\Youtube kaynak videolar\...  (read-only sources)
├── G:\AIContent\...  (cache and generated outputs)
├── D:\AIContent\...  (models and backups)
└── Native Windows processes
    ├── Django + HTMX web process
    ├── Sequential Python worker process
    ├── SQLite WAL database
    ├── FFmpeg / FFprobe executables
    └── NVIDIA CUDA-enabled Python runtimes
```

The server binds to `127.0.0.1` only. All filesystem access uses native
`pathlib.Path` Windows paths. The application never translates paths through a
Linux mount layer.

## 2. Technology baseline

- Windows 11 Pro 64-bit.
- Python 3.12 x64 in a project-local virtual environment managed by `uv`.
- Django 5.2 LTS with server-rendered templates and HTMX.
- SQLite in WAL mode.
- A Django management command implements one sequential worker.
- FFmpeg/FFprobe for media inspection, proxying, audio extraction and render.
- Native PyTorch/CUDA and CTranslate2-compatible Windows runtimes for local AI.
- No WSL2, Docker, Redis, Celery, PostgreSQL, Node application or cloud runtime.

Exact dependency versions are locked after the GPU PoC demonstrates a
compatible Windows driver/CUDA runtime/model combination.

## 3. Modules

- **Projects:** metadata, rights acknowledgement and source references.
- **Media:** allowlisted paths, source validation, proxy/cache/output assets.
- **Jobs:** queue, progress, checkpoints, retries, cancellation and errors.
- **Transcript:** subtitle selection and Whisper fallback.
- **Analysis:** scenes, audio, motion, candidate generation and VLM enrichment.
- **Moments:** scoring, prompt re-ranking, filtering and user decisions.
- **Timeline:** selected clips, trims, order, locks and versions.
- **Render:** captions, blurred-background vertical output and audio normalize.
- **Maintenance:** disk checks, cache expiry, trash and metadata backups.

Modules exchange identifiers and typed result objects; they do not invoke
vendor or process-specific code directly from views.

## 4. Execution model

- Web requests enqueue or mutate lightweight database records only.
- The worker claims one queued job using an atomic transaction.
- Each pipeline stage writes durable output and a completion checkpoint.
- GPU-heavy models are loaded sequentially:
  1. Whisper loads and transcribes.
  2. Whisper is released and CUDA cache is cleared.
  3. VLM loads and evaluates shortlisted candidates.
- A job interrupted by shutdown returns to `QUEUED` or `PAUSED` and resumes at
  the first incomplete stage.

## 5. Launcher

The Windows launcher must:

1. Verify the local virtual environment, FFmpeg and model roots.
2. Start native web and worker processes without opening duplicate instances.
3. Wait for a health endpoint.
4. Open the browser.
5. Provide a separate safe-stop action.

The launcher is a signed-or-local PowerShell script plus an optional shortcut.
It stores process IDs in the current user's application-data directory and
stops only those recorded processes.

Auto-start at Windows login is explicitly excluded.

## 6. Evolution boundary

SQLite/job-worker abstractions must allow later replacement by PostgreSQL and
a durable distributed queue. This compatibility does not authorize adding
those systems to the Pre-Alpha.
