# ADR-010 — Connected Accounts and Secrets

## Status
Accepted

## Context
`creator_console/secrets_ref.py` stores secrets locally. `local_pipeline/publish_youtube.py` uses injected HTTP sessions and OAuth tokens.

## Decision
All secrets are references; raw values are held by a secrets manager and accessed only by backend workers.

- `SecretRef`: `kind`, `name`, `fingerprint` (last 4), `expires_at`.
- Provider API keys (OpenAI, Groq, Gemini, ElevenLabs) are workspace-level secrets.
- YouTube OAuth tokens are connected-account secrets, scoped to a user, not workspace.
- Encryption at rest by KMS / HashiCorp Vault / AWS Secrets Manager.
- Token refresh is a backend worker concern; browser never sees refresh token.

Authorization:
- A user can connect their YouTube channel to a workspace.
- Workspace members with `editor`+ can select it for publish.
- Publish still requires explicit approval per video.

Revocation:
- Token invalidation marks connected account `status=revoked`.
- Audit event `connected_account.revoked`.
- No fallback to stored raw token after revocation.

## Consequences
- `secrets_ref.py` becomes a client-side interface to the secrets manager.
- `publish_youtube.py` receives a `Credential` object with a short-lived access token.
- Raw refresh tokens never appear in API, logs, artifacts, or audit payloads.

## Rejected alternatives
- A. Store secrets in PostgreSQL with application encryption. Rejected: key management is weaker.
- B. Pass tokens to browser. Rejected: direct security violation.

## Migration path
1. Add `SecretManager` interface with local env fallback.
2. Add `ConnectedAccount` repository.
3. Update `publish_youtube.py` to use token from manager.
4. Add OAuth flow in React, backend-only token exchange.

## Status note (Milestone 6)
`PublishService` now owns the publication credential surface, but the
token layout is unchanged: `publish_youtube.OAuthTokenStore` still reads the
project-namespaced `.runtime/youtube_oauth_token.json`, `credential_state()`
returns only capabilities, and `complete_authorization()` audits channel
identity, never token material. Workspace-owned connected accounts remain
future work — this milestone deliberately did not migrate ownership.
