# Local Pre-Alpha Product Requirements

**Status:** Approved planning baseline  
**Primary user:** Product owner, using one Windows PC  
**Initial market/content:** English entertainment content

## 1. Problem and goal

Finding theory-worthy scenes inside a long movie or episode is slow. The
Pre-Alpha must prove that a fully local pipeline can analyze a user-provided
video, surface useful evidence-backed moments, and turn selected moments into
an editable rough cut without sending media to an external service.

The product succeeds at this stage when the user can:

1. Place a video in the known local source library.
2. Create a project with title and optional character names.
3. Analyze the source once.
4. Review 20–40 ranked moment cards.
5. Optionally enter a theory/focus prompt and re-rank without re-analysis.
6. Select, reject, reorder, trim and lock moments.
7. Generate and play a 45-second vertical rough cut.
8. Reopen the project later with all analysis and timeline versions intact.

## 2. Scope

### Included

- English UI and English source analysis.
- One local user; no login screen.
- MP4, MKV and MOV sources up to 180 minutes or 20 GB.
- Source-rights responsibility checkbox per project.
- Embedded subtitle or sidecar SRT extraction, with local Whisper fallback.
- Scene, transcript, audio, motion and top-candidate VLM analysis.
- Adaptive candidates: 20/30/40 based on source duration.
- Preview cards with timestamp, core range, transcript evidence, reasons,
  characters, score and Low/Medium/High spoiler level.
- Character and score-reason filters.
- Optional focus prompt for Theory/Scene Explanation.
- AI-proposed 3–6 clip timeline that remains user-editable.
- Optional simple English captions.
- 720×1280 H.264/AAC rough-cut preview with normalized original audio.
- Persistent project history and local metadata backups.

### Excluded

- Cloud upload/storage and remote access.
- Multiple users/workspaces/channels.
- Opportunity Radar and external metadata providers.
- Script generation, TTS and music selection.
- Advanced timeline effects, face tracking and 1080p final export.
- YouTube OAuth, upload, publication and analytics.
- Billing, subscriptions, mobile apps and admin console.

## 3. Workflow

### Project creation

Required:

- title;
- source selected from `G:\Youtube kaynak videolar`;
- source-rights responsibility acknowledgement.

Optional:

- season and episode;
- known character names;
- initial theory/focus prompt;
- target duration from 30–60 seconds, default 45.

The source is registered in place by relative path and content hash. The
application does not copy, move, rename or delete the source file.

### Analysis

The user explicitly starts analysis. A preflight checks file validity, free
disk space, model availability and free VRAM. Jobs run sequentially and expose
stage, progress, elapsed time and actionable errors.

### Moment review

Moment cards support playback and display why a moment was selected. The user
can mark each candidate selected, rejected or undecided. Prompt changes only
re-rank stored analysis outputs.

### Rough cut

The system proposes order and trim points from selected moments. User edits
create new timeline versions. Locked clips may not be changed by regeneration.

## 4. Acceptance criteria

- No media, transcript, frame or prompt leaves the PC during normal analysis.
- A 180-minute source becomes reviewable in less than its source duration on
  the approved hardware, after PoC tuning.
- Each of three evaluation videos has at least five user-rated strong moments
  in its Top 20.
- Changing a focus prompt does not repeat transcription, scene detection,
  motion/audio extraction or VLM analysis.
- A rough cut is playable, 720×1280, near the selected duration, uses
  normalized original audio and can optionally burn captions.
- Restarting the PC does not lose completed stages; the job resumes from the
  next safe checkpoint.
- Duplicate sources are detected by content hash and offer analysis reuse.
- No application operation writes outside the approved G:/D: roots and the
  application-data directory under the current Windows user profile.
