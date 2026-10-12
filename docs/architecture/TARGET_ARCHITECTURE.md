# Target Architecture

> Target state for the multi-tenant SaaS evolution. ADRs in `docs/architecture/adr/` provide the detailed decisions.

## Layers

```
┌─────────────────────────────────────────┐
│  apps/web/  (React/Vite)                │  ← public SaaS UI
│  creator_console/static/ (compatibility)│
├─────────────────────────────────────────┤
│  services/api/  (FastAPI transport)     │  ← HTTP, authn extraction, CORS
├─────────────────────────────────────────┤
│  creator_console/api.py                 │  ← request dispatch, backward compat
├─────────────────────────────────────────┤
│  application/                           │  ← domain application services
│  - ProjectService                       │
│  - SourceService                        │
│  - AnalysisService                      │
│  - TranscriptService                    │
│  - ScriptService                        │
│  - VoiceService                         │
│  - EditorService                        │
│  - RenderService                        │
│  - PublishService                       │
│  - AutomationService                    │
│  - ApprovalService                      │
├─────────────────────────────────────────┤
│  domain/                                │  ← policies, workflow, studio drivers
│  - turbo_studio.py (workflow driver)    │
│  - long_form_studio.py (workflow driver)│
├─────────────────────────────────────────┤
│  local_pipeline/                        │  ← deterministic media engine
│  - scene_editor.py, render_utils.py     │
│  - publish_youtube.py, provider_adapters│
│  - tts.py, video_analysis.py            │
├─────────────────────────────────────────┤
│  infrastructure/                        │  ← repositories, storage, queue, secrets
│  - PostgreSQL repositories              │
│  - ObjectStorage (S3/MinIO/local)       │
│  - JobQueue (Redis/SQS)                 │
│  - SecretsManager                       │
│  - AuditSink                            │
└─────────────────────────────────────────┘
```

## Dependency rules

1. `apps/web/` may only call HTTP endpoints.
2. `services/api/` may only call `creator_console`.
3. `creator_console/api.py` may only call `creator_console/app.py` and `application/` services.
4. `application/` may call `domain/`, `infrastructure/`, and `local_pipeline`.
5. `domain/` may call `local_pipeline` and `infrastructure/`.
6. `local_pipeline/` does not import from `creator_console`, `application`, `domain`, or `services/api`.
7. `infrastructure/` does not import from upper layers.

## Migration principle

- `creator_console/pipeline_service.py` remains a backward-compatible façade until all application services are extracted.
- Existing tests and notebooks continue to work.
- Local filesystem remains the default dev adapter.
- PostgreSQL and object storage are opt-in via configuration.
