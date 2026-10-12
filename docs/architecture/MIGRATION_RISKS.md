# MIGRATION_RISKS — SaaS Multi-Tenant Path

> Risks identified during PROMPT 0 source-code audit. No code changes were made; these are pre-refactoring observations.

## Risk matrix

| # | Risk | Severity | Evidence | Mitigation |
|---|---|---|---|---|
| 1 | `PipelineService` is a 4300-line god class | **High** | `creator_console/pipeline_service.py` | Split into per-domain services (`SourceService`, `RenderService`, `PublishService`) behind a facade. Do not duplicate logic. |
| 2 | `turbo_studio.py` / `long_form_studio.py` bypass `PipelineService` | **High** | Direct `local_pipeline` imports | Refactor stage machine drivers to submit jobs or call `PipelineService`; keep `local_pipeline` canonical. |
| 3 | No self-approval enforcement in code | **High** | `local_pipeline/approval.py` records actor but never rejects `actor == approver` | Add explicit policy check; log and refuse. |
| 4 | Single-tenant filesystem storage | **High** | `creator_console/storage.py` uses `CONSOLE_ROOT/data` | Introduce object storage abstraction; keep `Storage` interface. PostgreSQL for metadata, S3/MinIO for media. |
| 5 | Secrets in local files | **High** | `.env` / `console/secrets` stored under `CONSOLE_ROOT` | Move to a secrets manager; use `SecretRef` + per-tenant encryption. |
| 6 | Job store is filesystem JSON | **Medium** | `creator_console/jobs.py` writes JSON files | Replace with PostgreSQL + row-level tenant isolation; keep `JobStore` API. |
| 7 | Audit log is append-only file | **Medium** | `creator_console/audit.py` appends to `audit.log` | Move to append-only DB table / log sink with tenant column. |
| 8 | Worker process is single-OS-process | **Medium** | `creator_console/worker.py` | Containerize worker, use a queue (Celery / RQ / SQS) with lease semantics. |
| 9 | `scene_editor.py` / `render_utils.py` synchronous on HTTP path | **Medium** | `/edl/render` runs ffmpeg in API process | Move Scene Editor render to a job or a separate worker process; keep `/edl/preview` fast. |
| 10 | No explicit tenant / workspace authz | **High** | `assert_owned()` checks `project_id` but no user/tenant boundary | Add `tenant_id` to `Ownership`; deny cross-workspace access. |
| 11 | `__file__` upload endpoints write directly | **Medium** | `creator_console/server.py` | Validate path, size, MIME; store in object storage. |
| 12 | Provider credentials are project-scoped, not tenant-scoped | **Medium** | `secrets_ref.py` uses `kind` + `CONSOLE_USER` | Add tenant isolation and per-tenant quota ledger. |
| 13 | Frontend uses relative API root with Vite proxy | **Low** | `API_ROOT = import.meta.env.VITE_API_ROOT ?? ''` | Fine for SaaS if proxy is tenant-aware; otherwise configure per-tenant backend. |
| 14 | `resolve_edl_revision` is unexposed | **Low** | `pipeline_service.py` has it; `api.py` does not | Wire `POST /edl/revision/resolve` if needed. |
| 15 | `verify_*.py` scripts use demo providers inconsistently | **Low** | `console.diagnostics` flags vary | Standardize a `demo` vs `live` mode matrix and document which scripts need real credentials. |

## Backward-compatibility notes

- `local_pipeline/` must remain the canonical deterministic media layer.
- `PipelineService` method names and artifact paths are the implicit API for `verify_*.py` and tests.
- Any storage rewrite must keep `Storage.resolve()` semantics and artifact relative paths.
- Approval hash semantics and private-first publish are non-negotiable.
- Jobs must remain idempotent; `idempotency_key` must survive store backend change.

## Recommended next milestone

1. **PROMPT X — Storage and tenant boundary refactor**
   - Introduce `TenantContext` / `Ownership.tenant_id`.
   - Abstract `Storage` behind a pluggable backend (local FS for dev, S3/MinIO for prod).
   - Move `JobStore` and `Audit` to PostgreSQL while keeping file-backed fallback.
   - Add explicit `self_approval` rejection.
   - Add `POST /edl/revision/resolve` or remove dead `resolve_edl_revision` code.

Do not proceed unless asked.
