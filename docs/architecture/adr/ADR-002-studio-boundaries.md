# ADR-002 — Turbo / Long-form Studio Boundary

## Status
Accepted

## Context
`creator_console/turbo_studio.py` and `creator_console/long_form_studio.py` directly import:

```python
from local_pipeline import (google_ai, music_library, provider_adapters,
                            render_utils, stock_media, timeline, turbo,
                            turbo_script, tts_chunks, stage_machine)

from local_pipeline import google_ai, long_form, long_form_script, stock_media, tts_chunks
```

The documented rule is "only `PipelineService` calls `local_pipeline`".

## Decision
Classify `TurboStudio` and `LongFormStudio` as **domain workflow orchestrators** with permitted `local_pipeline` access, but they must not be called directly by HTTP transport.

- They are application-layer workflow engines, not HTTP handlers.
- They are the only allowed exception to the "only `PipelineService`" rule because they implement multi-stage state machines that `PipelineService` would otherwise absorb.
- Future target: each becomes a thin state-machine driver whose per-stage execution is a `JobStore` job.

## Consequences
- `TurboStudio` and `LongFormStudio` are explicitly recognized as boundary exceptions.
- They still use `Storage`, `Audit`, `QuotaSignals`, and `TTSRunner` from `creator_console`.
- HTTP routes must call them through `ConsoleApp`, not directly.

## Rejected alternatives
- A. Force them through `PipelineService`. Rejected: `PipelineService` is already 4300+ lines and these modules are intentionally separated to avoid one god class.
- B. Move them into `local_pipeline`. Rejected: they manage storage, audit, quota, and TTS runner — all console concerns.

## Migration path
1. Add `application/studios.py` (or `application/turbo_studio.py`, `application/long_form_studio.py`) as the canonical driver.
2. `creator_console/turbo_studio.py` becomes a re-export/delegate.
3. Convert each stage into a `JobStore` job type; studio becomes a state-machine reader, not an executor.
