# ADR-002 — Local Media Storage

**Status:** Accepted  
**Date:** 2026-07-26

## Decision

Treat `G:\Youtube kaynak videolar` as the user-managed, read-only source
library. Store cache/output media under `G:\AIContent` and model cache plus
metadata backups on D:. Projects reference source files in place.

## Rationale

G: has the required capacity; D: is SSD-backed and suited to model loading.
Referencing sources in place prevents unnecessary copies and protects the
user's chosen library organization. Cloud object storage and same-PC uploads
provide no Pre-Alpha value.

## Consequences

- No cloud storage bill or media transfer.
- G: is a single physical-disk failure domain.
- H: cannot be considered a backup because it shares the same HDD.
- The application may not rename, move or delete source-library files.
- Path containment, disk guards, generated-output trash and metadata backups
  are mandatory.
