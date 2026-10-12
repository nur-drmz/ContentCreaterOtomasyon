# ADR-005 — Authentication, Tenancy and RBAC

## Status
Accepted

## Context
Current `Ownership` carries `workspace_id` and `project_id`, but authorization trusts request-provided `X-Actor` header or body `actor` / `workspace_id`.

## Decision
Authenticated server context is the only source of identity for SaaS. Local console keeps a single synthetic identity for development.

| Identity element | Source | Notes |
|---|---|---|
| `user_id` | JWT / session from `services/api/main.py` | not from body |
| `workspace_id` | resolved from route + membership | not from body |
| `project_id` | route | unchanged |
| `actor` | `user_id` or `job_worker` | `X-Actor` used only for local console override |

Roles:
- `owner`: full control, delete workspace, billing.
- `admin`: manage members, secrets, publish accounts.
- `editor`: create, edit, submit jobs, request approval.
- `reviewer`: approve/reject, cannot edit content (maker-checker).
- `viewer`: read-only.

Trust boundary:
- `services/api/main.py` extracts `user_id` from token.
- `creator_console/api.py` sets `Ownership` from token and route.
- `application/` services enforce workspace/project membership.
- `X-Actor` header is ignored in SaaS unless a local-dev `unsafe_local_actor` flag is on.

Local console compatibility:
- `console` app uses a single `usr_local` identity.
- No JWT; API token or cookie can bind to a local workspace.
- Same code paths, different `AuthAdapter`.

## Consequences
- All `Ownership` objects created after auth middleware.
- Direct `api.py` calls from `verify_*.py` continue using `Ownership` directly, bypassing auth.
- Cross-workspace access is denied at repository boundary.

## Rejected alternatives
- A. Trust `X-Actor` in SaaS. Rejected: easily spoofed.
- B. One workspace per user. Rejected: real product needs teams.

## Migration path
1. Add `AuthContext` to `services/api/main.py`.
2. Add `WorkspaceMembership` and `Role` to repository model.
3. Update `Ownership` to require `tenant_id`.
4. Add middleware that populates `Ownership` from token.
