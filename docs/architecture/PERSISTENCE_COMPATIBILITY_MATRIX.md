# Persistence Compatibility Matrix

> Milestone 1, updated by Milestone 2. The **Behavior changed?** column is `No`
> for every row: ports wrap the existing implementation; `LocalObjectStore` and
> `LocalArtifactRepository` are the only new adapters, and artifact writes now
> route through the latter via façade delegation.
>
> Milestone 2 renamed `ProjectRepository` → **`ProjectNamespaceStore`** and made
> **`LocalArtifactRepository`** the wired `ArtifactRepository` adapter (the
> façade delegates; it no longer IS the adapter).
>
> Milestone 3 routed source media through **`SourceService`** and added the
> read-only **`ports.SourceQuery`** contract. No new persistence backend: the
> source descriptor is still the `project` artifact's `source` block.
>
> Milestone 4 routed Phase 0 through **`AnalysisService`** and added
> **`ports.ProgressFactory`** with `LocalProgressFactory`, replacing the
> façade-owned tracker callable. `source_assessment` and `processing_plan`
> remain ordinary artifacts written through `LocalArtifactRepository`.
>
> Milestone 5 routed the content-approval lifecycle through
> **`ApprovalService`** and added two application ports:
> **`ports.ReviewGate`** (read-only human-checklist preconditions; local impl
> `LocalReviewGate` wrapping the console checklist modules) and
> **`ports.ApprovalInvalidator`** (the invalidation *record* an edit implies).
> Approvals remain embedded in their artifacts; no approval table or new
> persistence mechanism was introduced.
>
> Milestone 6 routed publication orchestration through **`PublishService`**
> and added **`ports.ApprovalQuery`** (the read-only `chain()` the intent gate
> consults). `publication_metadata`, `publish_intent` and `publish_manifest`
> remain ordinary artifacts through `LocalArtifactRepository`; the OAuth
> token stays under `.runtime/`; publication approvals stay embedded and
> separate.
>
> Milestone 9 added **`ports.WorkflowGateQuery`** (read-only plan-derived
> eligibility + slot scope) — no persistence surface at all; the gate reads
> `processing_plan` through the existing `ArtifactRepository`.
>
> Milestone 10 routed Phase 1 through **`VideoAnalysisService`** —
> `hot_moments` remains an ordinary artifact at `results/hot_moments.json`
> written through `LocalArtifactRepository`; the service introduces no new
> persistence mechanism, port or schema.
>
> Milestone 11 routed Phase 2 through **`MomentService`** — `moment_selection`
> stays a per-slot catalog artifact and `clip_bounds` stays a non-catalog
> document at `results/clip_bounds/output_NN.json`. It added
> **`ports.RevisionArchiver`** (the `.revisions/` snapshot mechanics, now
> shared by the façade and the service via `LocalRevisionArchive`) and the
> `ArtifactRepository.write_artifact_path` extension — an un-audited raw-path
> write for non-catalog documents. No schema changed.
>
> Milestone 12 routed the transcript lifecycle through
> **`TranscriptService`** — `moment_transcript` stays a per-slot catalog
> artifact at `results/moment_transcript.json` /
> `results/render/output_NN/moment_transcript.json`, written exclusively
> through `LocalArtifactRepository.write_artifact` (schema v2, unchanged).
> It extended `ports.ApprovalQuery` with the read-only `state(kind, slot)`
> and `ports.ProgressTracker` with `note(message)` — both already satisfied
> by their existing implementations — and reuses `ports.RevisionArchiver`
> for the `action="retranscribe"` replacement snapshot. No new persistence
> mechanism, no schema change.
>
> Zero bug fixes were bundled into these milestones, by design — including the
> duration fallback documented in `SOURCE_SERVICE.md`, which was found during
> Milestone 3, deliberately left behaving exactly as before, and repaired as a
> separately scoped correctness change in Milestone 8.

| Concern | Current storage mechanism | New port / interface | Local adapter | Behavior changed? | Future PostgreSQL? | Future object storage? | Migration concern |
|---|---|---|---|---|---|---|---|
| Project metadata | `project.json` in project namespace, written via `write_artifact("project", …)` | `ArtifactRepository` (content) + `ProjectNamespaceStore` (namespace) | `LocalArtifactRepository` / `LocalFilesystemStorage` | **No** | Yes — `projects` table | No | Project id and `project.json` shape are compatibility surface; the cutover must preserve `created_at`/`operator_name` |
| Artifacts (22 kinds) | JSON under `results/…`, `artifact_catalog.ARTIFACTS` registry, atomic write | `ArtifactRepository` | `LocalArtifactRepository` (façade delegates) | **No** | Metadata yes (path, sha256, version, status); document body stays JSON initially | Large artifacts later | Logical keys, relative paths, `schema_version`, `status` semantics and per-slot `results/render/output_NN/` layout must not change |
| Artifact versions | `.revisions/<rev>/…` + `manifest.json` (archive on rewind / clip-bounds edit / render-profile edit) | `RevisionArchiver` (Milestone 11) | `LocalRevisionArchive` — façade `_archive_revision`/`_render_slot_archive_paths` delegate to it | **No** | Yes — immutable `artifact_versions` table | Blobs yes | Rewind/archive currently moves files; a DB model must keep "archived, not deleted" semantics |
| Jobs | One JSON file per job under `jobs/`, `O_CREAT\|O_EXCL` project lock | `JobRepository` | `JobStore` | **No** | Yes — source of truth (ADR-007) | No | Idempotency key, compare-and-set claim, lease expiry, heartbeat, attempts, `recoverable`, cancel semantics, and the `status` vs `artifact_status` split must be reproduced exactly |
| Progress | `progress/*.json` via `ProgressTracker` | `ports.ProgressFactory` / `ProgressTracker` (M4) | `LocalProgressFactory` → `creator_console/progress.py` | **No** | Yes — or a job-attempt/event stream | No | Persisted progress must survive restart; it is read by the UI and by `mark_interrupted`. The cancel signal is bound into the factory by the job runtime, so trackers built by application services cancel identically |
| Approvals | Embedded in the artifact they approve (`human_approval` block, SHA-256 bound) | `ports.ReviewGate` (precondition reads) + `ports.ApprovalInvalidator` (invalidation records) — the approval *state* itself intentionally remains artifact-embedded, not a port (M5) | `local_pipeline/approval.py` + `ApprovalService` + `LocalReviewGate` | **No** | Yes — `approvals` table with `policy_version` | No | Digest binding and invalidation-on-mutation are invariants (ADR-006). Extracting them into a table must not let an approval outlive its content |
| Review checklists | `results/render_review.json`, `results/voice_review.json`, `results/copyright_review.json` (+ per-slot render copies under `output_NN/`), read via `load_artifact_path`, written via `atomic_write_json` | `ports.ReviewGate` for READS (M5); mutation still façade `_save_*` methods | `LocalReviewGate` | **No** | Yes — checklist tables bound to content digests | No | The "bound to the current content digest" rule is the invariant; a DB model must bind checklist answers to the same digests the approval gate uses |
| Audit | `audit_events.jsonl`, append + fsync, closed 41-name vocabulary | `AuditSink` | `AuditLog` | **No** | Yes — append-only table | No | Closed vocabulary, append-only, newest-first ordering, redaction, and tolerance of a truncated final line |
| Automation | `results/automation.json` read via `load_artifact_path` | Not yet a port | `AutomationEngine` | **No** | Yes — `automation_definitions` / `automation_runs` | No | A corrupt automation file must keep starting from a fresh idle state rather than wedging the console |
| EDL | `results/edl/edl.json`, `artifact_type: edl` | `ArtifactRepository` (via `EditorService`) | `LocalArtifactRepository` | **No** | Metadata yes; body JSON | No | EDL v3 is additive (ADR-009); v2 projects must stay renderable |
| Turbo workflow | `results/turbo/{brief,workflow,timeline}.json` + `events.jsonl` + `quota_ledger.json` | Not yet a port | `TurboStudio` | **No** | Yes — `workflow_state` + stage rows | Media yes | Stage machine resume semantics and the closed Turbo event vocabulary must be preserved; not job-ified in this milestone |
| Long-form workflow | `results/long_form/*` + `events.jsonl` + chunk ledger | Not yet a port | `LongFormStudio` | **No** | Yes — `workflow_state` + chunk ledger rows | Media yes | The TTS chunk ledger keyed by content hash guarantees a succeeded chunk is never spoken twice |
| Source media | File in project namespace, streamed upload in 8 MB parts; descriptor in `project.source` | `ArtifactRepository` + `ProjectNamespaceStore` behind `SourceService`; `ports.SourceQuery` for readers; `ObjectStore` (available, not yet wired) | `SourceService` / `LocalArtifactRepository` / `LocalObjectStore` | **No** | Descriptor metadata yes | **Yes** | ffmpeg needs a local path → `materialize()`; keys must be opaque, never client-supplied host paths. `SourceService` is the single seam where the local-path hop becomes a materialize hop |
| Previews / proxies | `results/…` preview MP4s produced by ffmpeg | `ObjectStore` (available, not yet wired) | `LocalObjectStore` | **No** | Metadata only | **Yes** | Retention: previews are disposable and should get a TTL |
| Renders (final) | `results/render/…` + `render_manifest.json` | `ObjectStore` for bytes, `ArtifactRepository` for the manifest | `LocalObjectStore` / `LocalArtifactRepository` | **No** | Manifest metadata yes | **Yes** | Approval is bound to the video's SHA-256; the digest must be computed over the stored object, not a temp copy |

## Serialization and path compatibility

Explicitly verified unchanged by this milestone:

| Surface | Changed? | How it is protected |
|---|---|---|
| API payloads | **No** | `services/api/tests/test_contract.py` (5 passed), `verify_console.py` |
| Artifact JSON (keys, `schema_version`, `status`) | **No** | `verify_console.py` §15 "artifact contract preserved end to end", `verify_batch_outputs.py` |
| Job JSON (field set, `artifact_type: console_job`) | **No** | `test_persistence_characterization.py::TestJobPersistence::test_new_job_record_shape_is_stable` |
| Job filenames (`jobs/<job_id>.json`) | **No** | `test_job_record_is_stored_as_jobs_job_id_json` |
| Audit JSONL (line format, `evt_` id prefix, ordering) | **No** | `TestAuditAppendOnly`, `TestAuditRecordShape` |
| Audit event vocabulary (41 names) | **No** | `TestAuditVocabulary` |
| Workspace/project relative paths | **No** | `TestStorageNamespace`, `verify_console.py` §2 |
| Artifact relative paths (incl. per-slot) | **No** | `verify_batch_outputs.py` slot-path assertions |
| Error codes / debug ids | **No** | `verify_faults.py` (70/70) |
| Approval digests | **No** | `verify_console.py` §6 stale-approval checks, `tests/test_publish.py` |

## One packaging change was required

`creator_console/app.py` now imports `infrastructure.storage.local`, which broke
the Colab source bundle: `tools/build_colab_console_bundle.py` shipped only
`creator_console` and `local_pipeline`, so the extracted bundle failed to import.

Caught by `verify_colab_console.py` (`extracted-bundle-serves-console-health`).
Fixed by adding `application` and `infrastructure` to `INCLUDE_ROOTS`. This is a
packaging fix, not a behaviour change: the bundle now contains the packages the
console actually imports.

## Notes on rows that are "not yet a port"

Automation and the two studio workflows were deliberately left without ports in
this milestone:

- **Studio workflows** are consumed by application logic that is itself
  scheduled for extraction in the next milestone. Porting them now would mean
  writing the contract twice.
- **Approvals** gained two application ports in Milestone 5 (`ReviewGate`,
  `ApprovalInvalidator`), but approval *state* stays embedded in the artifacts
  it approves by design — the digest binding is the invariant, and the future
  `approvals` table (ADR-006) is a PostgreSQL-milestone concern, not an
  `ArtifactRepository` concern.
- **Progress** got its port in Milestone 4 (`ProgressFactory`).

Each is listed here so it is tracked rather than forgotten.
