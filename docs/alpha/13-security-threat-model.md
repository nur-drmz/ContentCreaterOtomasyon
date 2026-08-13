# 13 — Security Threat Model

**Formalizes:** `the plan.txt` §19 (Security Requirements), §32 (Claude Code Implementation Guardrails), §3.1–3.3 (publishing/media principles).
**Status:** Draft
**Approval needed from:** Security

---

## 1. Assets

- User credentials / sessions
- YouTube OAuth tokens (per channel)
- Uploaded source media (potentially copyrighted, user-owned)
- Derived media (proxies, clips, renders)
- Scripts, evidence links, prompts (may contain unreleased/spoiler content)
- Analytics and channel performance data
- Provider API keys/secrets
- Audit logs
- Billing/subscription data

## 2. Trust boundaries

1. Public internet ↔ Client apps (web/mobile/admin)
2. Client apps ↔ API boundary (`07-api-boundary-spec.md`)
3. API boundary ↔ Background workers/job queue
4. Backend ↔ Object storage (signed URLs)
5. Backend ↔ External providers (LLM, vision, STT, TTS, YouTube, trend data)
6. Workspace ↔ Workspace (tenant isolation, §19)
7. Platform Admin/Support Admin ↔ Tenant data (elevated internal boundary)

## 3. Threats and mitigations (STRIDE-lite, mapped to source-doc requirements)

| # | Threat | Boundary | Mitigation (source) |
|---|---|---|---|
| T1 | Credential theft / session hijack | 1↔2 | Secure auth, session management, session revocation, optional MFA (§19 Authentication) |
| T2 | Cross-tenant data access (workspace A reads workspace B's project/media) | 6 | Every tenant-owned record scoped to workspace directly or via verifiable FK chain; DB-level + service-layer + channel-level checks (§19 Tenant isolation; enforced per `06-database-schema.md` §9) |
| T3 | OAuth token exfiltration or client-side exposure | 2↔3, 5 | Encrypt at rest, never returned to clients, minimum scopes, revocation + refresh support, access restricted to Channel Integration module only (§19 YouTube OAuth; `07-api-boundary-spec.md` §5) |
| T4 | Unauthorized media access via guessable/long-lived URLs | 4 | Private buckets, short-lived signed URLs, authorization check before URL generation, no public raw buckets (§19 Media security) |
| T5 | Unauthorized public publication (accidental or malicious) | 2↔3 | Two-step approve/publish with distinct permissions and distinct audited actions, server-side transition validation (§3.1, `04-state-transition-spec.md` critical design rule, §32.2) |
| T6 | Privilege escalation via role/permission confusion (workspace vs. channel scope) | 2↔3 | Explicit matrix (`03-role-permission-matrix.md`); flagged open decision on channel-role override of publish gate |
| T7 | Secret/token leakage via logs or observability | 3↔5, internal | Never log OAuth tokens, provider secrets, raw private media, long-validity signed URLs, sensitive user info (§27) |
| T8 | Insider/support overreach (Platform Admin, Support Admin) | 7 | Support Admin modeled read-only by default (`03-role-permission-matrix.md` §5); Platform Admin actions must be audited distinctly |
| T9 | Provider dependency compromise (a vendor SDK/service is breached or behaves maliciously) | 5 | Provider interfaces isolate domain logic from any single vendor (§3.7) so a compromised/misbehaving provider can be swapped without a domain rewrite; still needs vendor-side due diligence (not modeled here) |
| T10 | Copyright/legal exposure from source media | asset | Explicit non-guarantee of fair use/clearance; rights declaration required but not presented as legal clearance; no DRM bypass or protected-stream downloading anywhere in the product (§3.2, §20, §31) |
| T11 | Data loss from premature/incorrect media deletion | asset | Deletion job's "do not delete original before high-quality clips extracted and final upload confirmed" rule (`09-media-lifecycle-policy.md` §3); soft-delete with audit trail |
| T12 | Job/queue abuse (a user or bug triggers unbounded expensive jobs) | 3 | Idempotency, retries, timeouts, cost recording per job (`08-job-queue-spec.md` §1); ties to cost-model documents 11/12 for anomaly baselines |
| T13 | Theory Guard bypass leading to defamatory/false claims presented as fact | asset (script) | Theory Guard checks (§11); statement classification is a hard schema requirement (`10-ai-model-routing-spec.md` §5), not advisory-only at the data layer |

## 4. Audit logging requirements (verbatim list, §19)

Must log: login, membership changes, role changes, channel connections, OAuth revocation, media uploads, rights declarations, AI generations, script modifications, final approvals, private uploads, public approvals, publications, deletions, model version changes. Every entry attributable to `workspace_id` + `actor_user_id` (`06-database-schema.md` §8 `audit_logs`).

## 5. Publication safety checklist (§19, restated as a gate)

Before any transition to `PUBLIC` (`04-state-transition-spec.md`):
1. Final video approved (`FINAL_APPROVED` reached).
2. Metadata approved (title/description/thumbnail complete).
3. Actor holds `publication.approve_public` **and** a separate `publication.publish` check at execution time.
4. Explicit confirmation UI action recorded, not inferred from a prior step.
5. Audit record written for both the approval and the publish action.

## 6. Open decisions / follow-ups

- Four-eyes approval for public publish (single approver vs. two distinct people) — carried over from `03-role-permission-matrix.md` §6, needs explicit Security sign-off before Phase 0.4.
- Vendor security review process for each selected provider (LLM/vision/STT/TTS/storage/YouTube) — not yet started since providers are unselected (§30).
- MFA rollout timing — listed as "optional later" in §19; recommend requiring it at minimum for Platform Admin/Support Admin roles before Phase 1.0 (SaaS foundation) given elevated cross-tenant access.

## Approval needed from
Security.
