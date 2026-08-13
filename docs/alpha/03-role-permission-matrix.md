# 03 — Role and Permission Matrix

**Formalizes:** `the plan.txt` §5 (User Roles and Authorization).
**Status:** Draft
**Approval needed from:** Product, Security

---

## 1. Role scopes

Three independent scopes, matching §5:

- **Platform**: Platform Admin, Support Admin
- **Workspace**: Owner, Admin, Creator, Editor, Analyst, Viewer
- **Channel**: Channel Owner, Channel Manager, Publisher, Creator, Editor, Analyst, Viewer

A user's effective permission on a given resource is the union of platform role grants, workspace role grants, and channel role grants for the channel that resource belongs to. Channel roles are additive on top of workspace roles, not a replacement — e.g., a Workspace Viewer who is also Channel Publisher for one specific channel can publish for that channel only.

## 2. Permission list

Taken verbatim from §5's granular permission list (28 permissions across workspace/channel/project/media/analysis/moment/prompt/rough_cut/script/voice/video/publication/analytics/model namespaces).

## 3. Matrix (workspace-scope roles × permission)

`X` = granted, `-` = not granted, `own` = only on resources the user created/was assigned.

| Permission | Owner | Admin | Creator | Editor | Analyst | Viewer |
|---|---|---|---|---|---|---|
| workspace.manage_members | X | X | - | - | - | - |
| workspace.manage_billing | X | - | - | - | - | - |
| workspace.view_usage | X | X | - | - | X | - |
| channel.connect_youtube | X | X | - | - | - | - |
| channel.disconnect_youtube | X | X | - | - | - | - |
| channel.manage_members | X | X | - | - | - | - |
| channel.manage_concepts | X | X | own | - | - | - |
| channel.view_analytics | X | X | X | X | X | X |
| project.create | X | X | X | own | - | - |
| project.archive | X | X | own | - | - | - |
| project.assign | X | X | own | - | - | - |
| media.upload | X | X | X | own | - | - |
| media.delete | X | X | own | - | - | - |
| analysis.start / .cancel | X | X | X | own | - | - |
| analysis.view | X | X | X | X | X | - |
| moment.select / .reject / .manage | X | X | X | own | - | - |
| prompt.create / .manage_templates | X | X | X | own | - | - |
| rough_cut.generate / .edit | X | X | X | own | - | - |
| rough_cut.approve | X | X | own | - | - | - |
| script.generate / .edit | X | X | X | own | - | - |
| script.approve | X | X | own | - | - | - |
| voice.generate | X | X | X | own | - | - |
| voice.approve | X | X | own | - | - | - |
| video.edit / .render | X | X | X | own | - | - |
| video.approve | X | X | own | - | - | - |
| publication.upload_private | X | X | own | - | - | - |
| publication.approve_public | X | X | - | - | - | - |
| publication.publish | X | X | - | - | - | - |
| analytics.view | X | X | X | X | X | X |
| model.create_version | X | X | own | - | - | - |
| model.publish_version / .rollback_version | X | X | - | - | - | - |

Notes:
- `publication.approve_public` and `publication.publish` deliberately exclude Creator/Editor at workspace scope — public approval is an Owner/Admin-tier action by default (§3.1, §5 "Only authorized users may approve public publication"). Whether Channel Owner/Publisher roles can also grant this at channel scope is an explicit open point for Security sign-off (below).
- "own" means scoped to projects/media the user created or is assigned to via `ProjectAssignment` — enforced server-side, not just hidden in UI (§19 "service-layer authorization").

## 4. Matrix (channel-scope roles × permission)

Channel-scope roles apply only within the specific channel; they can **grant** additional permission for that channel beyond the user's workspace role, but never bypass workspace-level gates like `workspace.manage_billing`.

| Permission | Channel Owner | Channel Manager | Publisher | Creator | Editor | Analyst | Viewer |
|---|---|---|---|---|---|---|---|
| channel.manage_concepts | X | X | - | - | - | - | - |
| channel.view_analytics | X | X | X | X | X | X | X |
| moment.select/.reject/.manage | X | X | - | X | own | - | - |
| prompt.create | X | X | - | X | own | - | - |
| rough_cut.generate/.edit/.approve | X | X | - | X | own | - | - |
| script.generate/.edit/.approve | X | X | - | X | own | - | - |
| voice.generate/.approve | X | X | - | X | own | - | - |
| video.edit/.render/.approve | X | X | - | X | own | - | - |
| publication.upload_private | X | X | X | - | - | - | - |
| publication.approve_public | X | X | X | - | - | - | - |
| publication.publish | X | X | X | - | - | - | - |
| analytics.view | X | X | X | X | X | X | - |
| model.create_version | X | X | - | - | - | - | - |
| model.publish_version/.rollback_version | X | X | - | - | - | - | - |

## 5. Platform-scope roles

- **Platform Admin**: full cross-workspace administrative access (support tooling, billing overrides, tenant suspension). Must be logged distinctly and MFA-gated (ties to `13-security-threat-model.md`).
- **Support Admin**: read-only cross-workspace access for support investigations; no publish, no billing changes, no data export by default.

## 6. Open decisions

- Whether Channel Owner/Publisher may unilaterally approve public publication without a second workspace-level Admin/Owner sign-off (a "four-eyes" requirement) — currently modeled as **single approver sufficient**; flag for Security review before Phase 0.4 implementation.
- Exact behavior when a user's workspace role and channel role conflict (e.g., workspace Viewer + channel Publisher) — this doc assumes **most-permissive-of-scope wins within that channel only**; needs explicit sign-off.

## Approval needed from
Product (role names/scopes), Security (approval-gate and platform-role review).
