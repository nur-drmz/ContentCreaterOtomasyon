# ADR-009 — EDL v3 / Editor Architecture

## Status
Accepted

## Context
Current `local_pipeline/scene_editor.py` EDL supports 9:16 vertical, center crop / blurred background, captions, B-roll, and a single music track. `apps/web/src/screens/RenderAtelierScreen.jsx` is a drag-drop 9:16 editor.

## Decision
EDL v3 is a server-authoritative timeline model. The browser preview is a proxy, never a second renderer.

### v3 concepts

- **Canvas**: target `width` × `height` with `aspect_ratio` enum `vertical_9_16`, `horizontal_16_9`, `square_1_1`, `feed_4_5`.
- **Track**: video, image, audio, text, effect.
- **Clip**: `source_in`, `source_out`, `timeline_in`, `timeline_out`, `crop`, `transform`, `keyframes`.
- **Caption overlay**: text, style, position, animation, track.
- **B-roll**: on a video/image track with `position`, `position_x`, `position_y`, `opacity`.
- **Audio**: source, music, ducking, gain.
- **Version history**: immutable EDL versions; optimistic concurrency uses `version` + `parent_version`.

### Migration from current EDL

1. Keep current EDL schema for existing projects.
2. Add `edl_v3` artifact type.
3. `scene_editor.py` v2 compiler remains for legacy; v3 compiler introduced.
4. Render Atelier upgrades to v3 once compiler parity is proven.
5. Timeline editor works from v3 only.

### Renderer rule

- `local_pipeline/render_utils.py` and `local_pipeline/scene_editor.py` produce filtergraph.
- Browser preview can render a low-res server-generated proxy or an open-frame exact player, but not a separate JS renderer.

## Consequences
- Current `render_utils.py` and `scene_editor.py` kept.
- v3 is additive, not replacement.
- `RenderAtelierScreen.jsx` will eventually become v3-native.

## Rejected alternatives
- A. Browser-side rendering. Rejected: semantic divergence and secret exposure.
- B. Immediate EDL replacement. Rejected: breaks existing projects and tests.

## Migration path
1. Design `EDLv3` dataclasses in `local_pipeline/scene_editor_v3.py`.
2. Add `build_v3_filtergraph()` and `build_v3_command()`.
3. Add `POST /edl/v3` and `PATCH /edl/v3/clips/`.
4. Migrate Render Atelier to v3 behind feature flag.
