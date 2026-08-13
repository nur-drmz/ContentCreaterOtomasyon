# ADR-003 — Local-First AI Routing

**Status:** Accepted  
**Date:** 2026-07-26

## Decision

Run deterministic media processing, transcription, embeddings and
top-candidate VLM enrichment locally. Load Whisper and the VLM sequentially on
the RTX 3080. Ship no cloud provider implementation in Pre-Alpha.

## Rationale

Local processing removes variable API cost and keeps private entertainment
media on the machine. Cascaded analysis limits expensive VLM work to candidates
that pass deterministic filtering.

## Consequences

- GPU compatibility and processing time must be proven before UI development.
- Model outputs require strict schemas and versioned evaluation.
- A disabled provider interface may support future explicit, per-request cloud
  assistance, but automatic fallback is prohibited.

