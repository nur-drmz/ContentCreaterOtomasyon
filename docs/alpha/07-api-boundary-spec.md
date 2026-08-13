# 07 — API Boundary Specification

**Formalizes:** `the plan.txt` §16 (Backend modules), §3.7 (Provider independence), §15 (Web/Mobile responsibilities).
**Status:** Draft
**Approval needed from:** Eng

---

## 1. Purpose

Define module boundaries and the logical request/response contracts between clients (web/mobile/admin) and the backend, and between backend modules — without committing to a specific web framework, RPC style, or serialization format (all open, §30 "Backend framework").

## 2. Module boundary list (from §16)

Each module owns its own tables (per `06-database-schema.md`) and exposes operations only through its boundary — no module reaches into another module's tables directly:

1. **Identity** — auth, sessions, users.
2. **Workspace & RBAC** — workspaces, memberships, roles/permissions (`03-role-permission-matrix.md`).
3. **Channel Integration** — channel connection, OAuth token storage/refresh (never exposed to clients, §19).
4. **Channel Concept** — concepts, model versions.
5. **Opportunity Radar** — content opportunities, scoring.
6. **Project Workflow** — projects, state machine (`04-state-transition-spec.md`), assignments.
7. **Upload and Media Lifecycle** — upload sessions, media assets, retention/deletion.
8. **Video Analysis** — analysis orchestration, transcript, scenes, Scene Genome, Narrative Graph.
9. **Moment Ranking** — Hot Moment scoring and retrieval.
10. **Prompt and Script Generation** — prompts, rough cut, script, evidence links, Theory Guard.
11. **Voice Generation** — TTS orchestration.
12. **Timeline and Rendering** — timeline editor state, render jobs.
13. **Prediction** — viral score, view forecast.
14. **Publication** — YouTube upload, public-approval gate.
15. **Analytics** — snapshots, retention, feedback signals.
16. **Billing and Usage** — subscriptions, usage records, provider usage/cost.
17. **Audit** — audit log writes/reads (write access available to all modules; read access gated).
18. **Notifications** — cross-module event fan-out to users.

## 3. Client-facing API resource groups

Logical REST-ish resource groups (naming is illustrative, not a framework commitment):

```
/auth/*                         -> Identity
/workspaces/{id}/*               -> Workspace & RBAC
/workspaces/{id}/channels/*       -> Channel Integration, Channel Concept
/workspaces/{id}/opportunities/*  -> Opportunity Radar
/projects/*                      -> Project Workflow (state transitions live here)
/projects/{id}/uploads/*          -> Upload and Media Lifecycle (returns signed URLs, never proxies bytes — §3.3)
/projects/{id}/analysis/*         -> Video Analysis (read-mostly; start/cancel are the only writes)
/projects/{id}/moments/*          -> Moment Ranking + Moment Selection
/projects/{id}/prompts/*          -> Prompt and Script Generation
/projects/{id}/rough-cut/*        -> Prompt and Script Generation (rough cut submodule)
/projects/{id}/scripts/*          -> Prompt and Script Generation (script submodule)
/projects/{id}/voice/*            -> Voice Generation
/projects/{id}/timeline/*         -> Timeline and Rendering
/projects/{id}/predictions/*      -> Prediction
/projects/{id}/publication/*      -> Publication
/channels/{id}/analytics/*        -> Analytics
/workspaces/{id}/billing/*        -> Billing and Usage
/workspaces/{id}/audit/*          -> Audit
/me/notifications/*               -> Notifications
```

Every write endpoint under `/projects/{id}/*` is validated against the current project state per `04-state-transition-spec.md` — an endpoint call that would skip or reverse a required transition returns a rejection, not a silent no-op.

## 4. Provider boundary contracts (§3.7)

Domain logic in modules 8 (Video Analysis), 10 (Script Generation), 11 (Voice Generation), 7 (Media Lifecycle → storage), 5 (Opportunity Radar → trend data), 12 (Rendering), 14 (Publication → social), 15 (Analytics) must depend only on an internal interface, never a concrete SDK call. Minimum interface shapes:

```
LlmProvider.generate(prompt, context, constraints) -> {text, tokensUsed, cost}
VisionProvider.analyzeFrame(frame, task) -> {labels, confidence, cost}
SpeechToTextProvider.transcribe(audioRef, language) -> {segments[], cost}
TextToSpeechProvider.synthesize(text, voiceSettings) -> {audioRef, durationSec, cost}
ObjectStorageProvider.createUploadSession(...) -> {uploadUrl, expiresAt}
ObjectStorageProvider.createSignedReadUrl(assetId, ttl) -> {url, expiresAt}
TrendDataProvider.queryEntity(entity, window) -> {metrics}
VideoRenderProvider.render(timelineSpec) -> {renderJobId}
SocialPublishingProvider.uploadPrivate(videoRef, metadata) -> {externalVideoId}
SocialPublishingProvider.publishPublic(externalVideoId) -> {status}  -- only ever called after PUBLISH_APPROVED (§04)
AnalyticsProvider.fetchSnapshot(externalVideoId) -> {metrics}
```

Concrete providers (ElevenLabs, specific LLM vendor, specific storage vendor) are injected implementations of these interfaces; no module signature may reference a vendor name. This is testable by the acceptance criterion in `01-mvp-prd.md` §6 ("a non-ElevenLabs TTS provider can be added by implementing the interface only").

## 5. Cross-module contracts that must not be violated

- Only the Channel Integration module may read/decrypt OAuth tokens; Publication module calls Channel Integration to get an authorized client, never reads the token itself (§19).
- Only Upload and Media Lifecycle issues signed URLs; other modules request one through it rather than generating their own (keeps authorization-before-signed-URL centralized, §19 "Media security").
- Video Analysis is called at most once per `SourceVideo` (enforced additionally at the schema level per `06-database-schema.md` §9); other modules must reference its outputs, never trigger a duplicate analysis run.
- Publication module exposes `requestPublicApproval()` and `publish()` as two distinct calls requiring two distinct permission checks — no combined endpoint (ties to `04-state-transition-spec.md` §2 critical design rule).

## 6. Open decisions

- Transport/style: REST vs. GraphQL vs. RPC (tRPC, gRPC) — open, depends on "Backend framework" (§30).
- Sync vs. async response pattern for long operations (poll vs. webhook vs. websocket/SSE for job progress) — recommend async with job-status polling/streaming for all Stage 4+ operations regardless of transport choice, since §16 mandates jobs not run in synchronous handlers (§32.5).
- API versioning scheme — not yet decided; no external consumers exist pre-Phase 2.0 Public API, so low urgency.

## Approval needed from
Eng.
