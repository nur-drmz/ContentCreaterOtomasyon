# PublishService

> Extracted in Milestone 6. `application/services/publish_service.py` owns
> publication orchestration; `local_pipeline/publish.py` remains the canonical
> deterministic policy engine (private-first rules, intent/visibility digests,
> preconditions) and `local_pipeline/publish_youtube.py` remains the canonical
> YouTube implementation (OAuth, resumable upload, token redaction). The façade
> methods are thin delegates — every existing caller, API route and notebook
> keeps working unchanged.

## What it owns

| Responsibility | Method | Notes |
|---|---|---|
| Metadata generation | `generate_metadata(*, job_id, actor)` | Job `metadata_generation`; Gemini client via injected factory or `secrets.credential("google_api")`; `metadata_studio` builds the artifact |
| Manual metadata | `write_manual_metadata(*, title, description, tags, actor)` | `metadata_studio.build_manual_metadata` + `apply_attribution` |
| Selection/editing | `select_metadata(*, title_index, description, tags, category_id, actor)` | Recomputes `selected_sha256`; invalidates a prior metadata approval by changing the digest |
| Metadata approval | `approve_metadata(*, reviewed_sha256, approved_by, notes)` | Hash-bound; `approval.granted`/`approval.rejected` with `kind: publication_metadata` |
| Publish-state query | `publish_state()` | View model: credential state, preconditions, intent approval, manifest — never mutates |
| Intent creation | `create_intent(*, platform, visibility, publish_at, made_for_kids, account_ref, actor)` | Requires render + metadata artifacts present AND `ApprovalQuery.chain()["render"].effective_valid` |
| Intent approval | `approve_intent(*, reviewed_sha256, approved_by, notes)` | Binds `publish_approval.approved_decision_sha256`; authorizes exactly ONE private upload |
| Private upload | `run_upload(*, job_id, actor)` | Job `publish_upload`; progress stages; manifest persisted before the intent is marked spent |
| Visibility approval | `approve_visibility(*, target_visibility, reviewed_remote_id, approved_by, publish_at, notes)` | Bound to the reviewed remote video id, not to the project |
| Visibility application | `apply_visibility(*, job_id, actor)` | Job `visibility_change`; progress tracker + the canonical `execute_visibility_change` |
| Credential state | `credential_state()` | Capability report only — no token material |
| OAuth | `authorization_url(*, redirect_uri, state)` / `complete_authorization(*, code, redirect_uri, actor)` | Backend-only; the completed state carries channel identity, never a token |
| Provider access | `provider()` | Lazy `publish_youtube.YouTubeProvider` construction; injection preferred in tests |

## Dependencies (all injected)

`artifacts` (`ports.ArtifactRepository`) · `audit` (`ports.AuditSink`) ·
`namespace` (`ports.ProjectNamespaceStore`) · `ownership` ·
`progress` (`ports.ProgressFactory`) · `approvals` (`ports.ApprovalQuery`) ·
`secrets` (resolver — only `.credential("google_api").configured` is read) ·
`provider` (`local_pipeline.publish.PublishProvider` — `None` builds the real
YouTube provider lazily) · `google_client_factory` (optional).

**Zero** façade/transport imports and no sibling-service imports — enforced by
`tests/test_architecture_guards.py` and `test_publish_facade.py` architecture
guards.

## Publication state machine

```
(none) → publication_metadata written          [generate | write_manual]
       → metadata selected                     [select_metadata]
       → metadata APPROVED                     [approve_metadata, sha-bound]
       → publish_intent written                [create_intent; requires
                                                render chain effective_valid]
       → intent APPROVED                       [approve_intent, sha-bound]
       → upload_attempt marker persisted       [run_upload, BEFORE the
                                                provider call -- fail-closed]
       → PRIVATE upload performed              [run_upload → provider]
       → publish_manifest persisted            [upload.remote_id recorded]
       → intent spent                          [upload_performed + attempt
                                                status completed|failed]
       → visibility APPROVED                   [approve_visibility, bound to
                                                reviewed_remote_id]
       → visibility applied                    [apply_visibility → provider]
```

The three approval records live **inside** their artifacts
(`metadata_approval`, `publish_approval`, `visibility_approval`) and are a
completely separate state machine from content approval
(`processing_plan`/`script`/`voiceover`/`transcript`/`render` grants in
`ApprovalService`). Content approval is *necessary* (create_intent consults
the chain) but *never sufficient*.

## Digest binding (unchanged, canonical)

| Artifact | Binding | Invalidated by |
|---|---|---|
| `publication_metadata` | `metadata_approval.approved_selection_sha256` vs current `selected_sha256` | any `select_metadata`/edit changing the selection |
| `publish_intent` | `publish_approval.approved_decision_sha256` vs `sha256_canonical_json(decision)` where `decision` covers `video_sha256` (file hash), `rights_decision_sha256` (ingest), platform, visibility, `made_for_kids`, render+metadata digests | re-render, rights re-decision, metadata re-selection, target edits → **create a new intent** |
| `publish_manifest` | carries `publish_intent_sha256`, `video_sha256`, `rights_decision_sha256`, `upload.remote_id` | append-only result record |
| visibility approval | `reviewed_remote_id` must equal `manifest.upload.remote_id` | a different upload → new approval |

All SHA-256 math stays in `local_pipeline.approval` / `local_pipeline.publish`;
the service never re-implements a hash.

**Schema note (M7):** `upload_attempt` is a new optional field on
`publish_intent`, written outside `decision` — it does not participate in
`decision_sha256`/`approved_decision_sha256`, so intent approval validity is
unchanged. Older artifacts without the field load and behave exactly as
before; the field is purely operational state (`in_flight` → `refused` /
`uncertain` / `failed` / `completed`, plus `attempted_at`, `attempted_by`,
`job_id`, optional `observed_remote_id`, `completed_at`).

## Private-first invariant

- `publish.INITIAL_PRIVACY` is always `private`; `execute_private_upload`
  ignores the intent's *requested* visibility for the actual upload.
- `run_upload` refuses a second upload when `manifest.upload.remote_id`
  exists — before the provider is reached.
- `upload_performed` marks the intent spent; a new upload needs a new intent
  and a new approval.
- `apply_visibility` is the only path to `unlisted`/`public`, and only after
  `visibility_change_allowed` (reviewed-remote-id binding, expiry, target
  validity). A successful private upload changes nothing remotely.

## Content-approval integration

`PublishService` reads the chain through `ports.ApprovalQuery.chain()` —
implemented by `ApprovalService`, injected as a port. The only consumption is
`create_intent`: `chain()["render"].effective_valid` must be true (covers
upstream staleness, transcript drift, moment-selection drift). No
`ApprovalService.approve` call is ever made by this service; publication
approvals are granted by the publication gates only.

## YouTube gateway and credentials

- The injected `provider` implements `publish.PublishProvider`:
  `describe_credential_state`, `upload_private`, `set_visibility`. No new
  gateway port was added — the canonical protocol is already the right seam.
- Lazy construction resolves the token path through the namespace store:
  `.runtime/youtube_oauth_token.json` (`RUNTIME_DIRNAME` in
  `artifact_catalog`). Building the provider reads no secret and opens no
  socket.
- `credential_state` wraps provider errors into `{configured: false}` so a
  settings panel cannot crash; nothing returned ever contains token material
  (provider-side redaction is the only path data leaves the boundary).
- `complete_authorization` audits `artifact.written` with
  `{youtube_authorized, channel_id}` — never a token.
- Test doubles: `tests/test_publish_facade.py::FakePublishProvider` records
  calls so tests prove WHEN the provider is reached.

## Upload idempotency, retry and crash recovery (Milestone 7)

One intent now authorizes exactly one **attempt**, durably recorded before
the external side effect:

- **Pre-upload marker (fail-closed).** After the `remote_id` dedup check and
  before `execute_private_upload`, `run_upload` persists
  `intent.upload_attempt = {attempted_at, attempted_by, job_id,
  status: "in_flight"}`. **If this write fails, the provider is never
  called** — a persistence failure can never start an upload.
- **Blocked retry.** Any later `run_upload` on an intent that already has
  `upload_attempt` (or legacy `upload_performed`) raises
  `PipelineServiceError` before reaching the provider. The error carries the
  attempt status, timestamp and — when known — the `observed_remote_id` so a
  human can reconcile in YouTube Studio before creating a new intent.
- **Duplicate-upload guard.** A persisted `manifest.upload.remote_id` still
  blocks every later `run_upload` first — ordinary retries of a completed
  job cannot double-publish.
- **Crash-window taxonomy.**

  | Window | Marker state | Retry outcome |
  |---|---|---|
  | Gate refusal before provider (`PermissionError`) | `status: refused` (best-effort) | blocked — new intent needed |
  | Provider raises / ambiguous mid-upload failure | `status: uncertain` | blocked — no duplicate |
  | Remote success, manifest write fails | `status: uncertain` + `observed_remote_id` | blocked — evidence preserved |
  | Manifest written, spent-marker write fails | `status: in_flight` | `remote_id` dedup blocks |
  | Provider reports `status: failed` | `status: failed` | blocked — a failed result is ambiguous (the chunk may have landed) |
  | Success | `status: completed` | `remote_id` dedup blocks |

- **Crash-path annotation** (`_best_effort_attempt_update`) preserves what
  the provider actually returned — including the remote id — when the
  manifest write itself fails. It records evidence; it never fabricates a
  success and never masks the original exception.
- **Reconciliation.** The canonical provider exposes no "find my upload"
  operation (`describe_credential_state` / `upload_private` /
  `set_visibility` only), and YouTube cannot be queried by upload session or
  content hash, so true remote reconciliation is **not available**. The
  blocked state is therefore explicit: check `upload_attempt` (and
  `observed_remote_id` when present), verify or delete the orphaned private
  upload in YouTube Studio, then create and approve a **new** intent.
- **Concurrency.** The marker makes a *sequential or retried* second attempt
  impossible; a genuine same-instant race (both callers read before either
  marker write lands) is narrowed but not eliminated — artifact writes are
  last-writer-wins, not a lock. Job-level idempotency (`submit_job`
  `idempotency_key` + `claim`) is the primary guard; a residual P2 race
  remains documented.
- **Job side.** `creator_console.jobs` owns leasing, attempts, idempotency
  keys, and cancellation — unchanged. The service only reports progress.
- **Semantics.** This is **at-least-once** upload with fail-closed retry, not
  exactly-once: the trade is a possible orphaned private upload (requiring
  manual cleanup) for a guaranteed absence of silent duplicates.

## Audit events (unchanged)

`artifact.written` (metadata/intent/manifest writes + OAuth completion
detail) · `approval.granted`/`approval.rejected` (`kind` ∈
`publication_metadata`, `publish_intent`, `visibility`) ·
`provider.call` (`phase` ∈ `metadata_generation`, `publish_upload`,
`visibility_change`). Detail shapes are pinned by characterization tests.

## Fault injection

All publication writes go through `ports.ArtifactRepository` →
`infrastructure.persistence.local.artifact_repository.atomic_write_json`,
already in `creator_console.faults._ATOMIC_WRITE_HOLDERS` — no new holder.
`patch_atomic_write(match=...)` reaches every publication write (ENOSPC,
permission, partial-write all tested).

## Known limitations

- **Orphaned private uploads.** An `uncertain`/`in_flight` attempt whose
  remote object was never recorded requires manual cleanup in YouTube
  Studio — no provider-side lookup exists. See the reconciliation section.
- **Same-instant race.** The marker check→write is not atomic across
  processes; job-level idempotency is the primary dedup (P2 residual).
- **Single channel identity.** Workspace-owned connected accounts
  (ADR-010) are a later milestone; the token file stays project-namespaced.

### Fixed in Milestone 7

- **`visibility_change` job dispatch.** `run_visibility_change` /
  `apply_visibility` now accept `job_id=None`, matching the generic executor
  contract (`method(job_id=..., **params)`) and the other job entry points.
  Direct callers without `job_id` are unaffected; the job now carries a
  `visibility_change` progress tracker.
- **Duplicate upload after uncertain outcome.** Closed by the durable
  `upload_attempt` marker described above.

## Dependency diagram

```
ConsoleApi → PipelineService ─► PublishService
                                    ├─ ports.ArtifactRepository → LocalArtifactRepository
                                    ├─ ports.AuditSink          → AuditLog
                                    ├─ ports.ProjectNamespaceStore → LocalFilesystemStorage
                                    ├─ ports.ProgressFactory    → LocalProgressFactory
                                    ├─ ports.ApprovalQuery      → ApprovalService (read-only)
                                    ├─ PublishProvider          → publish_youtube.YouTubeProvider
                                    ├─ metadata_studio / publish (canonical policy)
                                    └─ secrets.credential("google_api") → google_ai client
```
