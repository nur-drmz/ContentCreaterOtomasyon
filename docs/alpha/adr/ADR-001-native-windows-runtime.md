# ADR-001 — Native Windows Reference Runtime for Alpha

**Status:** Accepted for Alpha planning  
**Date:** 2026-07-26  
**Scope:** Alpha backend, media workers, AI workers, rendering workers, local development and self-hosted validation

## Context

The current Pre-Alpha is being built and validated on Windows 11 with an
NVIDIA RTX 3080. Leaving the Alpha operating system undecided would allow the
implementation documents to drift toward Linux-only commands, containers and
deployment assumptions that are not required for this project.

The Alpha may still use cloud services for object storage, authentication,
queues or managed databases. “Cloud” does not imply that our application and
GPU worker runtime must be Linux.

## Decision

The Alpha reference implementation will be native Windows:

- Development and single-machine validation target Windows 11.
- Python services, AI inference, FFmpeg and background workers run directly on
  Windows using a project virtual environment.
- Operational scripts use PowerShell (`.ps1`).
- File paths are handled with `pathlib`/platform-neutral application APIs; no
  `/home`, `/mnt`, Bash, `systemd`, `chmod` or `apt` assumptions are allowed.
- WSL2 is not required and is not part of the supported Alpha architecture.
- Docker is optional for a future provider or CI need, never a prerequisite for
  local development or the reference worker runtime.
- A hosted Alpha deployment must support Windows-native application/worker
  processes, or keep Windows GPU/media workers connected to managed cloud
  services through provider interfaces.

Browser and mobile clients remain platform-independent. External managed
services may run on infrastructure controlled by their providers; this ADR
governs the code and runtime we own.

## Consequences

- CUDA, FFmpeg, file locking, process control and path behavior must be tested
  on Windows.
- Queue and worker technologies must support Windows without relying on Unix
  process semantics.
- CI must include a Windows runner before Alpha is considered releasable.
- Linux support may be added later through a separate ADR, but Alpha cannot be
  blocked on it.

