# Local Pre-Alpha Documentation Index

**Status:** Approved planning baseline  
**Product stage:** Pre-Alpha  
**Audience:** Product owner and implementation agents

This directory is the source of truth for the product currently being built:
a local, single-user entertainment-video intelligence prototype. The broader
cloud and multi-user vision is archived in `../alpha/`.

## Documents

| # | Document | Purpose |
|---|---|---|
| 01 | [Product Requirements](01-product-requirements.md) | Goal, scope, workflow, acceptance criteria |
| 02 | [Local Architecture](02-local-architecture.md) | Native Windows architecture and component boundaries |
| 03 | [Media and AI Pipeline](03-media-ai-pipeline.md) | Local transcription, analysis, scoring and rendering |
| 04 | [Data Model and Jobs](04-data-model-and-jobs.md) | SQLite entities, job states and versioning |
| 05 | [Media Lifecycle](05-media-lifecycle.md) | G:/D: layout, retention, trash and disk guards |
| 06 | [Security and Privacy](06-security-privacy.md) | Localhost boundary, path safety and optional API rules |
| 07 | [Testing and Evaluation](07-testing-evaluation.md) | PoC gates, tests and moment-quality evaluation |
| 08 | [Implementation Milestones](08-implementation-milestones.md) | Ordered checklist and exit gates |
| 09 | [Development Workflow](09-development-workflow.md) | Git, environment, changes and verification rules |

## Architecture decisions

- [ADR-001 — Local Python Monolith](adr/ADR-001-local-python-monolith.md)
- [ADR-002 — Local Media Storage](adr/ADR-002-local-media-storage.md)
- [ADR-003 — Local-First AI Routing](adr/ADR-003-local-first-ai-routing.md)

## Fixed decisions

- One user, one Windows PC, localhost access only.
- Native Windows Python/Django; no WSL2 or Docker for Pre-Alpha.
- SQLite WAL plus one sequential database-backed worker.
- Read-only source library at `G:\Youtube kaynak videolar`; generated media on
  `G:\AIContent`; model cache and metadata backups on D:.
- English source content and English UI.
- Theory/scene-explanation is the first optimized content mode.
- Maximum source: 180 minutes or 20 GB.
- Local AI is the only active route. Cloud-provider code is disabled and
  limited to an interface/feature flag.
- Output: editable 45-second timeline and 720×1280 preview render.

## Explicitly deferred

Cloud storage, remote access, multi-user tenancy, billing, mobile apps,
Opportunity Radar, scripts, TTS, YouTube upload/publishing, advanced editing
and performance forecasting remain in the Alpha vision.
