# ADR-001 — Local Python Monolith

**Status:** Accepted  
**Date:** 2026-07-26

## Decision

Build the Pre-Alpha as a native Windows Django/HTMX monolith with SQLite WAL
and one sequential database-backed worker process.

## Rationale

There is one user and one Windows machine. PostgreSQL, Redis, Celery, WSL2,
Docker and separate frontend/backend deployments add operational work without
validating moment quality. Native Python provides a direct path to FFmpeg and
local GPU tooling while matching the required Windows user experience.

## Consequences

- One native Windows installation and PowerShell launcher.
- CUDA/model compatibility must be verified specifically on Windows.
- No multi-user or distributed-worker guarantees.
- Module/provider boundaries must permit later database and queue replacement.
