# 09 — Media Lifecycle Policy

**Formalizes:** `the plan.txt` §17 (Media Lifecycle), §3.3 (Cloud-first media workflow).
**Status:** Draft
**Approval needed from:** Product, Security

---

## 1. Asset types (from §17)

Original source video, proxy video, audio track, extracted subtitles, transcript, keyframes, scene previews, selected high-quality clips, rough-cut previews, voice-over audio, final render, thumbnail candidates. Each maps to a `MediaAsset.asset_type` in `06-database-schema.md`.

## 2. Retention policy (proposal, per §17 table — not yet approved as defaults, §30)

| Asset type | Default retention | Deletion trigger |
|---|---:|---|
| Original source | Until project completion, max 24h by default | `project.state == PUBLIC/CANCELLED/ARCHIVED` OR 24h elapsed, whichever first — **unless** required high-quality clips not yet extracted or final upload not yet confirmed (explicit exception below) |
| Analysis proxy | 3 days | scheduled sweep past `retention_expires_at` |
| Transcript and timestamps | Until project deletion | project hard-delete only |
| Keyframes | 7 days or regenerated | scheduled sweep, or superseded by regeneration |
| Selected clips | 7 days | scheduled sweep |
| Rough preview | 3 days | scheduled sweep |
| Final render | 24h after confirmed YouTube upload | `Publication.uploaded_at` + 24h |
| Analytics metadata | Persistent | never (unless workspace deletion) |
| Audit records | Per policy (retained; exact duration open, §30 commercial/compliance TBD) | — |

## 3. Explicit exception (verbatim rule from §17)

> Do not delete the original source before required high-quality clips are extracted and final upload is confirmed, unless the user explicitly requests deletion.

Implementation: the Media Deletion job (`08-job-queue-spec.md`) must check, before deleting an `asset_type=original_source` row, that either (a) `Publication.uploaded_at IS NOT NULL` for this project, or (b) the user issued an explicit `media.delete` action against that asset. Absent either condition, the sweep skips it even if `retention_expires_at` has passed, and logs a `retention_hold` event rather than silently extending retention forever without visibility.

## 4. Deletion mechanics

- Deletion is a job (`08-job-queue-spec.md` "Media deletion"), not a synchronous API call — consistent with §32.5 (no long/side-effecting operations in request handlers) and so deletion is retryable/auditable.
- Deletion writes an `AuditLog` entry (§19 "Deletions").
- Deletion targets the object-storage object and the `MediaAsset` row's `deleted_at`; the row itself is retained (not hard-deleted) so audit/history queries still resolve, unless workspace-level GDPR-style erasure is later required (open, not in MVP scope).

## 5. Upload-side lifecycle (§3.3, §Stage 3)

- Uploads go directly browser/mobile → object storage via short-lived signed URLs; the application server does not proxy the full file body unless technically unavoidable (documented exceptions must be listed in `07-api-boundary-spec.md` if any arise).
- `UploadSession` rows expire; an expired session cannot be resumed and must be re-initiated, preventing indefinitely-lived signed URLs (§19 "Media security").

## 6. Duplicate and corruption handling

- Content-hash based duplicate detection at `SOURCE_UPLOADED → SOURCE_VALIDATED` (`04-state-transition-spec.md`): if a matching `content_hash` already has a completed `VideoAnalysis`, the user is offered to reuse it instead of storing/analyzing a second copy — reduces both storage and compute cost (§18).
- Corruption detection blocks the transition to `SOURCE_VALIDATED` and routes to `FAILED` with a specific reason.

## 7. Signed URL discipline (§19)

- Every signed URL is generated only after an authorization check for the requesting user against the specific media asset's project/channel/workspace chain.
- Signed URLs are short-lived; long-validity URLs are explicitly disallowed from logs (§27).

## 8. Open decisions

- Exact numeric retention values in the table above are a **default proposal from the source document**, not approved values — Product must confirm before Phase 0.1 implementation, especially "Maximum source duration" and "Maximum source file size" (§30).
- Audit record retention duration/policy — depends on eventual compliance requirements not yet defined.
- Whether users can configure per-workspace retention overrides (e.g., extend original-source retention for a paid plan) — not specified in source doc; flag as a possible commercial lever for `18-implementation-milestones.md` Phase 2.0, not MVP.

## Approval needed from
Product (retention values), Security (deletion/signed-URL discipline).
