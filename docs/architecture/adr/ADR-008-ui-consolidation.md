# ADR-008 — React and Creator Console Consolidation

## Status
Accepted

## Context
There are two frontends:
- `creator_console/static/app.js` (local console)
- `apps/web/src/*.jsx` (React/Vite, newer)

Feature overlap is partial. React currently has `TrendRadarScreen`, `SceneEditorScreen`, `RenderAtelierScreen`, `PublishScreen`.

## Decision
- **React** becomes the public SaaS UI.
- **Creator Console static JS** becomes a local/diagnostic compatibility surface.
- No new features in Creator Console.
- Feature parity means all user-facing SaaS flows exist in React.

Migration first:
1. Render Atelier / EDL (already in React).
2. Turbo Studio view and stage control.
3. Long-form Studio view and stage control.
4. Trend research → source-candidates/ingest.
5. Publish flow (metadata, intent, upload, visibility).
6. Workspace/admin (new, no Creator Console equivalent).

Creator Console retains:
- local project bootstrap,
- worker management,
- diagnostics,
- secret entry,
- quick-fix operator tools.

## Consequences
- API remains common.
- Creator Console screens can be retired one-by-one as React reaches parity.
- `services/api/main.py` and `creator_console/api.py` stay the same; only UI changes.

## Rejected alternatives
- A. Keep both full UIs. Rejected: duplicated effort, divergence risk.
- B. Build new Vue/Angular. Rejected: React already exists.

## Migration path
1. Add missing React screens behind feature flags.
2. Run browser tests against React.
3. Remove Creator Console screen when parity proven.
