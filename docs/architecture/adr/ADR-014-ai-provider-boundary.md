# ADR-014 — AI / Provider Boundary

## Status
Accepted

## Context
`local_pipeline/google_ai.py` and `local_pipeline/provider_adapters.py` route AI calls. `google_ai.py` lazily imports `genai.Client` and constructs it from a secret. Provider adapters receive an `OpenAI`-compatible client.

## Decision
All provider construction is centralized; every call produces operational metadata.

Centralized modules:
- `local_pipeline/provider_adapters.py` for OpenAI-compatible providers (OpenAI, Groq, NVIDIA NIM).
- `local_pipeline/google_ai.py` for Gemini.
- `creator_console/secrets_ref.py` (or future `application/secrets_service.py`) provides credentials.

Operational metadata for every call:
- `task_type` (script, tts, asr, analysis, etc.)
- `provider`, `model`
- `request_id` / `attempt`
- `latency_sec`
- `input_tokens` / `output_tokens` / `characters` / `seconds` where applicable
- `structured_output_valid` (yes/no)
- `error_class` (none, parse, rate_limit, safety, etc.)
- `fallback_path` (which provider was tried next)
- `estimated_cost_usd` / `actual_cost_usd` when available

Safety refusals are terminal:
- A content safety refusal stops the task.
- It is not retried on a different provider to circumvent the refusal.
- The refusal is logged and returned to the caller.

Agents do not own:
- auth, billing, rights authorization
- job leasing, storage ownership
- final technical render validation

## Consequences
- Provider calls are observable and auditable.
- Quota ledger can be exact.
- No SDK construction in HTTP handlers.

## Rejected alternatives
- A. Each module constructs its own client. Rejected: breaks testability and observability.
- B. Retry on safety refusal with another provider. Rejected: undermines safety.

## Migration path
1. Add `ProviderCall` operational metadata model.
2. Wrap every provider call with telemetry.
3. Add `ProviderRegistry` for task-to-provider routing.
4. Update `provider_adapters.py` to return normalized metadata.
