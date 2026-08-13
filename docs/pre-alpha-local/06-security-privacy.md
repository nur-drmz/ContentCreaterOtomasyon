# Local Pre-Alpha Security and Privacy

**Status:** Approved planning baseline

## 1. Boundary

- The application is single-user and binds only to `127.0.0.1`.
- No login, registration, invitation, workspace or remote API is exposed.
- Windows firewall/router configuration must not publish the port.
- A request with an unexpected `Host` header is rejected.

## 2. Local-first privacy

During normal analysis operation (source ingestion, transcription, scene/audio/
motion analysis, VLM enrichment, scoring, timeline and render):

- video, audio, subtitles, transcript, frames and prompts stay on the PC;
- no analytics/telemetry SDK is enabled;
- no cloud AI/storage provider is configured;
- no automatic model download occurs during an analysis job (the VLM stage
  loads with `local_files_only=True` and degrades to deterministic-only
  scoring rather than downloading — see `studio/pipeline/vlm.py`).

Internet access is allowed only for an explicit environment/model update
command, or for the opt-in voice-over module described in §3 below. Model
revisions and hashes are recorded.

## 3. Voice-over provider boundary (implemented)

The voice-over module (`studio/voice/`) is the one feature that talks to an
external service, and it is the concrete implementation of the gate this
document originally described as a future `OptionalCloudAnalyzer`. It ships
disabled by default (`local_disabled` provider — never makes a network call)
and requires all of the following before any request reaches a real provider:

- **Provider and model are explicit.** Providers are named, injected
  implementations of `TextToSpeechProvider`
  (`studio/voice/providers/base.py`); no call site references a vendor by
  name. The only real provider today is `ElevenLabsProvider`
  (`studio/voice/providers/elevenlabs.py`).
- **Exact text transmitted is visible before sending.** The Voice Studio page
  shows the narration script being sent; nothing beyond that sentence text
  and the selected voice settings is transmitted.
- **Per-request preview and explicit approval.** `queue_voiceover`
  (`studio/views.py`) requires an `approved=true` field, enforced
  server-side by `VoiceGenerationForm` — checking a box is the only way to
  reach the provider, and approval is scoped to that one submission, not a
  standing setting.
- **Cost limit.** `settings.VOICE_COST_LIMIT_USD` bounds the estimated cost
  of a generation batch; `worker.run_voiceover` aborts before calling the
  provider if the estimate exceeds it, and no partial spend happens first.
- **Retention/training policy.** Not independently verified here — this
  document defers to ElevenLabs' own published data-retention/training
  terms at the time a user supplies their own API key; no key is bundled or
  defaulted.
- **Audit record.** Every request attempt (`voice.generate.requested`,
  `voice.synthesize`, `voice.synthesize.failed`, `voice.cost_limit_exceeded`)
  is written to the local `AuditEvent` table via `services.record_audit_event`,
  recording provider, sentence length and cost — never the full narration
  text — per §6.
- **Local fallback, no automatic fallback.** `LocalDisabledProvider` is the
  default and the only fallback; the system never silently substitutes one
  provider for another. If ElevenLabs is unusable (no key, request failure),
  the job fails with a clear reason instead of falling back automatically.

The API key itself (`ELEVENLABS_API_KEY`) is read from an environment
variable at call time only — never stored in `settings.py`, the database, or
logs.

## 4. Filesystem safety

- Store root identifiers plus relative paths, not arbitrary absolute paths.
- Resolve/canonicalize before every read, write, move or delete.
- Reject traversal, symlink/reparse-point escape and alternate data-stream
  targets.
- Never recursively delete an unresolved/computed root.
- Native Windows subprocesses receive validated argument arrays; no user value
  is interpolated into PowerShell, `cmd.exe` or shell command strings.

## 5. Untrusted media

- Treat containers, subtitle files and model output as untrusted.
- Invoke FFmpeg/FFprobe with argument arrays, resource/time limits and no shell.
- Bound subtitle size, line length and encoding handling.
- Bound decoded resolution, frame count and VLM pixel budget.
- Validate every VLM JSON field and ignore unexpected fields.

## 6. Secrets and logs

- No secrets are required initially.
- Logs omit source contents, transcript bodies, extracted frames and full
  filesystem paths when a project-relative reference is sufficient.
- Crash reports remain local.
- Rights acknowledgement is a responsibility record, not legal clearance.
