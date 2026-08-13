# 00 — Alpha / Future SaaS Planning Documents Index

> **Classification notice:** This set describes the future cloud,
> multi-user Alpha/SaaS product. It is preserved as long-term product vision
> and is not the implementation source of truth for the current local,
> single-user Pre-Alpha. Use `../pre-alpha-local/00-index.md` for current work.
>
> **Platform decision:** The Alpha reference implementation is native Windows,
> not Linux/WSL. See
> [`adr/ADR-001-native-windows-runtime.md`](adr/ADR-001-native-windows-runtime.md).

**Source of truth this set formalizes:** `../../the plan.txt` §33 "Required Documents Before Coding."
**Overall status:** Archived as Alpha vision. All 18 documents are drafted;
none are approved for current implementation.

---

## 1. Document status

| # | Document | Status | Approval needed from |
|---|---|---|---|
| 1 | [MVP PRD](01-mvp-prd.md) | Draft | Product |
| 2 | [User Stories & Acceptance Criteria](02-user-stories-acceptance.md) | Draft | Product, Eng |
| 3 | [Role & Permission Matrix](03-role-permission-matrix.md) | Draft | Product, Security |
| 4 | [State Transition Spec](04-state-transition-spec.md) | Draft | Eng, Security |
| 5 | [Domain Model](05-domain-model.md) | Draft | Eng |
| 6 | [Database Schema](06-database-schema.md) | Draft | Eng |
| 7 | [API Boundary Spec](07-api-boundary-spec.md) | Draft | Eng |
| 8 | [Job & Queue Spec](08-job-queue-spec.md) | Draft | Eng |
| 9 | [Media Lifecycle Policy](09-media-lifecycle-policy.md) | Draft | Product, Security |
| 10 | [AI Model Routing Spec](10-ai-model-routing-spec.md) | Draft | Eng, Product |
| 11 | [Cost Model — per Source Hour](11-cost-model-source-hour.md) | Draft (structure only) | Product, Eng |
| 12 | [Cost Model — per Finished Video](12-cost-model-finished-video.md) | Draft (structure only) | Product, Eng |
| 13 | [Security Threat Model](13-security-threat-model.md) | Draft | Security |
| 14 | [Repository Structure](14-repository-structure.md) | Draft (not to be scaffolded yet) | Eng |
| 15 | [Development Workflow](15-development-workflow.md) | Draft | Eng |
| 16 | [Testing Plan](16-testing-plan.md) | Draft | Eng |
| 17 | [AI Evaluation Plan](17-ai-evaluation-plan.md) | Draft | Eng, Product |
| 18 | [Implementation Milestones](18-implementation-milestones.md) | Draft | Product, Eng |

## 2. How to use this set

- These documents formalize `the plan.txt` into reviewable, testable artifacts. They do not replace it — `the plan.txt` remains the vision source; these are the "before coding" deliverables it requires (§33).
- No document here should be treated as final until its "Approval needed from" parties sign off. Update the Status column (Draft → Needs review → Approved) as that happens, and update the doc's own header line to match.
- Cross-references use `[[doc-name]]`-style prose links throughout; if you resolve an open decision, update every document that flagged it, not just one.

## 3. Consolidated open decisions (carried from `the plan.txt` §30 and each document's own "Open decisions" section)

### Product
- Final product name
- Initial language and target market
- Private alpha vs. public beta
- Opportunity Radar inclusion in the first prototype (`01-mvp-prd.md` §3, `02-user-stories-acceptance.md` US-1 AC3)
- Maximum source duration / max source file size (`09-media-lifecycle-policy.md` §8)
- Default media retention values — table in `09-media-lifecycle-policy.md` §2 is a proposal, not approved

### Web and mobile
- React Native vs. Flutter
- Mobile upload in first release
- Offline mobile prompt drafts

### Backend
- Backend framework (Python is selected for application and worker services)
- Auth provider vs. custom auth
- Database hosting, object storage provider and Windows-compatible queue provider
- Vector storage approach (`06-database-schema.md` §10)

### Runtime platform — resolved
- Native Windows is the Alpha reference runtime for development, AI/media
  processing, rendering and self-hosted validation.
- WSL/Linux is not required. Docker remains optional and cannot become a local
  development prerequisite.
- PowerShell is the operational scripting standard; owned CI must include a
  Windows runner.
- Managed cloud services remain allowed behind provider interfaces.

### AI
- Speech-to-text, embedding, vision, narrative, and final-script model/provider selection (`10-ai-model-routing-spec.md` §7)
- Local vs. API-hosted models
- Character clustering approach (`05-domain-model.md` §9 — free-text for MVP, deferred)

### Rendering
- FFmpeg-only vs. FFmpeg + Remotion
- Browser timeline implementation
- Preview strategy, segment render cache (`08-job-queue-spec.md` §5, `12-cost-model-finished-video.md` §6)

### Integrations
- Entertainment metadata provider (TMDB or similar)
- YouTube quota strategy
- TTS commercial plan
- CapCut feasibility

### Commercial
- Free plan limits, credit model, subscription/overage/team/storage pricing

### Security / governance (raised while formalizing, not originally itemized in §30)
- Four-eyes approval for public publish: single approver vs. two distinct people (`03-role-permission-matrix.md` §6, `13-security-threat-model.md` §6)
- MFA rollout timing for elevated roles (`13-security-threat-model.md` §6)
- Audit record retention duration/policy (`09-media-lifecycle-policy.md` §8)

None of the above should be silently assumed by whoever picks up implementation — per `the plan.txt` line 24, "Do not silently resolve open decisions."

## 4. Next step

Per `the plan.txt` §34 ("The next required planning deliverable is an approved MVP PRD with explicit acceptance criteria and selected architecture decisions"): route documents 1–2 to Product for review first, since most other documents depend on MVP scope being locked; route the Backend/AI open-decision list above to Eng/Product jointly, since documents 5–15 are written to be stack-agnostic but cannot become concrete until those decisions land.
