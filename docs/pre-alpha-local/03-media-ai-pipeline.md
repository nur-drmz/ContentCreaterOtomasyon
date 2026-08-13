# Local Media and AI Pipeline

**Status:** Approved planning baseline  
**Mode optimized first:** Theory / Scene Explanation

## 1. Pipeline stages

1. `validate_source`
2. `extract_metadata`
3. `create_proxy`
4. `extract_or_transcribe`
5. `detect_scenes`
6. `analyze_audio`
7. `analyze_motion`
8. `generate_pre_candidates`
9. `enrich_candidates_with_vlm`
10. `score_and_select_candidates`
11. `generate_previews`

Every stage is idempotent against source hash, pipeline version and relevant
configuration hash.

## 2. Source and transcript

- Validate actual container/streams with FFprobe; never trust the extension.
- Reject sources over 180 minutes or 20 GB before analysis.
- Use embedded English subtitles first.
- Otherwise use a sidecar SRT with matching basename.
- Otherwise use GPU `faster-whisper` with an English Distil-Whisper model.
- Preserve segment and word timestamps when available.
- Store transcript source (`embedded`, `sidecar`, `whisper`) and model/version.

The PoC starts with `distil-large-v3.5`. If its evaluation transcript is not
usable, compare `large-v3-turbo` under the same speed/VRAM measurements before
changing the approved default.

## 3. Deterministic analysis

- PySceneDetect produces shot/scene boundaries.
- FFmpeg/librosa produce RMS/loudness, spectral/onset changes, silence and
  music-rise features.
- OpenCV samples the proxy for motion intensity and composition stability.
- Transcript features include dialogue density, semantic boundaries and
  optional similarity to known characters/focus prompt.

This stage creates a larger pre-candidate set without a VLM.

## 4. VLM enrichment

Default model: `Qwen/Qwen2.5-VL-7B-Instruct-AWQ`, loaded locally and
sequentially after Whisper is unloaded.

For each pre-candidate, provide:

- four bounded-resolution keyframes or one contact sheet;
- transcript excerpt with timestamps;
- project title, episode metadata and user-supplied character allowlist.

Require JSON output:

```json
{
  "summary": "string",
  "characters": ["allowlisted name or unknown"],
  "narrativeImportance": 0.0,
  "emotionalIntensity": 0.0,
  "theoryPotential": 0.0,
  "hookSuitability": 0.0,
  "independentComprehensibility": 0.0,
  "spoilerLevel": "low|medium|high",
  "reasons": ["string"]
}
```

Invalid output is retried once with a repair prompt. Persistent failure keeps
the deterministic features and marks VLM enrichment unavailable; it does not
invent values.

## 5. Hot Moment scoring

Base score:

```text
22% Narrative importance
18% Emotional intensity
15% Audio and music rise
15% Motion or action
10% Character importance
10% Theory potential
 5% Hook suitability
 5% Independent comprehensibility
```

All component values and the formula version are stored. Overlapping
candidates are merged or suppressed to avoid near-duplicate cards.

Candidate count:

- up to 60 minutes: 20;
- over 60 through 120 minutes: 30;
- over 120 through 180 minutes: 40.

## 6. Focus prompt re-ranking

The base analysis remains immutable. A prompt run computes local semantic
relevance from stored transcript/VLM data:

```text
final_rank = 0.75 * base_score + 0.25 * prompt_relevance
```

The formula is versioned. Changing a prompt creates a new ranking run and must
not trigger stages 1–9.

## 7. Rough-cut proposal and render

- Propose 3–6 clips totaling the selected target duration (default 45 seconds).
- Respect all selected moments and locked clips.
- Prefer coherent transcript boundaries and avoid cutting mid-word.
- Render 720×1280 H.264/AAC.
- Preserve the source aspect ratio in the center; fill the background with a
  blurred and darkened copy of the same frame.
- Normalize original audio and use hard cuts.
- Optionally generate SRT/WebVTT and burn a single simple caption style.

The timeline JSON, captions and render are separate versioned artifacts.

