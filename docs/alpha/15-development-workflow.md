# 15 — Development Workflow

**Formalizes:** `the plan.txt` §32 (Claude Code Implementation Guardrails), §33 item 15, general engineering practice implied by §25/§28.
**Status:** Draft
**Approval needed from:** Eng

---

## 1. Guardrail enforcement (§32, restated as process, not just principle)

Every one of the 18 guardrails in §32 must be checkable in PR review, not left as an honor system. Proposed enforcement mapping:

| Guardrail (§32) | Enforcement mechanism |
|---|---|
| No unresolved architecture decisions without approval | PR template requires linking the relevant `docs/alpha/*.md` doc and its "Approved" status (see `00-index.md`) |
| No automatic public publishing | Code review checklist item; covered by state-machine tests (`04-state-transition-spec.md`, `16-testing-plan.md` E2E "Unauthorized public approval attempt") |
| No DRM bypass / protected-stream downloading | Explicit reviewer checklist item on any upload/ingest-related PR |
| No hard-coded provider in domain logic | Lint rule / architecture test forbidding vendor SDK imports outside `/packages/domain` provider-implementation adapters (`07-api-boundary-spec.md` §4) |
| No long media jobs in sync API handlers | Architecture test or lint rule flagging job-shaped work (transcoding, ML inference, rendering) called directly from request handlers instead of enqueued |
| No OAuth token exposure | Reviewer checklist + response-schema tests asserting token fields never serialize to client-facing DTOs |
| No unauthorized media URL exposure | Integration test asserting signed-URL endpoints reject unauthorized callers (`13-security-threat-model.md` T4) |
| No skipped workspace/channel authorization | Every new endpoint must have an authorization test (`16-testing-plan.md` Integration "Tenant isolation") |
| No retaining media beyond lifecycle rules | Scheduled job + alert if `retention_expires_at` sweep backlog grows (`09-media-lifecycle-policy.md`) |
| No advanced mobile editing before web validation | Roadmap sequencing enforced via `18-implementation-milestones.md` phase gates |
| No premature microservice extraction | Architecture decisions require the justification list in §25 documented in the PR/ADR before any module leaves the monolith |
| No claiming deterministic view prediction | UI/copy review checklist; forecast output schema always includes P10/P50/P90 + confidence (`02-user-stories-acceptance.md` US-13 AC2) |
| No presenting theories as confirmed fact | Theory Guard schema requirement (`10-ai-model-routing-spec.md` §5) + copy review |
| No regenerating valid cached artifacts | Idempotency-key tests per job type (`08-job-queue-spec.md` §1) |
| Don't start coding before phase/AC approved | Phase gate defined in `18-implementation-milestones.md`; PRs reference the milestone they belong to |
| Small, reviewable, tested, documented changes | Standard PR size/review norms below |

## 2. Branching and review

- Trunk-based development with short-lived feature branches, PR review required before merge to main (no direct pushes to main).
- Every PR: links to the user story/AC it implements (`02-user-stories-acceptance.md`), and to the milestone phase (`18-implementation-milestones.md`).
- PRs touching provider integrations, auth, publication, or media deletion require an explicit reviewer sign-off from the area listed in that document's "Approval needed from" line (e.g., a Publication-module PR needs a Security-aware reviewer per `13-security-threat-model.md`).

## 3. Environments

- Local development uses native Windows 11, Python virtual environments,
  PowerShell and Windows builds of FFmpeg/CUDA dependencies. WSL is not a
  prerequisite (`adr/ADR-001-native-windows-runtime.md`).
- Local, shared staging/integration and production remain separate. Exact
  hosting is open, but any owned application/media/AI worker deployment must
  support the Windows reference runtime.
- Staging uses non-production YouTube channels/test accounts and non-production provider keys where the provider supports sandboxing, so no real channel can be accidentally published to during testing.

## 4. Secrets handling

- Provider API keys, OAuth client secrets, and signing keys are never committed to the repo; managed via the eventual hosting platform's secret store (mechanism open pending §30 hosting decisions).
- Consistent with `13-security-threat-model.md` T7 — secrets and tokens are also never written to logs.

## 5. Documentation discipline

- Every module implementing a provider interface (`07-api-boundary-spec.md` §4) documents which concrete provider it wires up and why, in that module's own README — keeps the "why this vendor" reasoning near the code, distinct from the planning docs here.
- Alpha planning documents in `docs/alpha/` are updated (not silently outdated) whenever an open decision they reference gets resolved — `00-index.md` is the single source of truth for which are still open.

## 6. Open decisions

- CI provider and pipeline tooling are open, but the required pipeline must
  include a Windows runner.
- Python code style/linting toolchain details remain open.

## Approval needed from
Eng.
