"""Turning a list of clips and a narration into one video. Shared engine.

WHY THIS EXISTS
---------------
The console could plan a timeline but not execute one. `scene_editor.py` tried:
it wraps `build_vertical_filtergraph` -- which already emits a complete
`[0:v]...[framed]` statement -- inside another `[pre_0]...[framed_0]` wrapper,
producing an invalid graph. The test only checked that the string contained
"concat", so nothing ever handed it to ffmpeg. It also renders silent (`-an`),
burns no captions, and ignores the `timeline_start_sec` it stores.

Rather than fix five defects in a schema that cannot express what is needed
(no transitions, no reuse, no narration, no attribution, no fps normalisation),
this is a fresh compiler over a manifest that can. It calls the same
`render_utils` helpers, so there is one reframing implementation, not two.

THE TWO INVARIANTS
------------------
1. **No gaps, no overlaps.** `clips[i].timeline_end_sec == clips[i+1].timeline_start_sec`
   and the last clip ends exactly at the narration's measured duration. The
   packer computes this and `build_filter_complex` re-checks it before emitting
   anything: a manifest that disagrees with itself is rejected, not rendered.
2. **Same manifest, same command.** Given the same manifest and the same input
   files, `build_filter_complex` returns a byte-identical string and
   `build_ffmpeg_args` an identical argv. This deliberately does NOT promise a
   byte-identical MP4 -- libx264 output varies with library version and thread
   partitioning, so asserting file equality would fail on an ffmpeg upgrade for
   no real reason.

WHY FADE IS A DIP TO BLACK AND NOT AN XFADE
-------------------------------------------
`xfade` has signature VV->V: chaining N clips consumes N-1 transitions and each
one SHORTENS the total by its duration. An 18-minute timeline with 216 clips and
0.4s crossfades would finish 86 seconds before its narration. Compensating for
that is possible but every later retime has to recompute it. A fade-out into a
fade-in, concatenated normally, preserves total duration exactly -- so narration
sync is structural rather than arithmetic.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from datetime import datetime, timezone
from math import ceil
from pathlib import Path

from . import render_utils

SCHEMA_VERSION = 1
ARTIFACT_TYPE = "timeline_manifest"

#: Above this, the argv approaches Windows' 32,768-character command-line limit
#: even with the graph in a file, because each clip still contributes its own
#: `-ss/-t/-i <path>` arguments.
MAX_CLIPS = 240
#: A tolerance of one frame at the lowest supported rate. Floating-point
#: accumulation over 200 clips cannot be expected to land exactly.
GAP_TOLERANCE_SEC = 1.0 / 24


class TimelineError(ValueError):
    """A timeline that cannot be rendered, and the reason why."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _round(value) -> float:
    return round(float(value), 3)


# ------------------------------------------------------------------ ordering
def resolve_seed(*, project_id: str, script_sha256: str, seed_input: str) -> int:
    """A reproducible integer seed derived from what the video actually is.

    Derived rather than taken raw so the same typed seed in two different
    projects does not produce the same shuffle, and so the same brief
    reproduces its order on another machine and after a project restore.
    """
    material = f"{project_id}|{script_sha256}|{seed_input}"
    return int(hashlib.sha256(material.encode("utf-8")).hexdigest()[:16], 16)


def order_clip_ids(clip_ids, *, mode: str, seed: int | None) -> list:
    """The frozen play order.

    `sorted()` first, always: iterating a set or relying on `hash()` would make
    the "same seed, same order" promise depend on PYTHONHASHSEED.
    """
    ordered = sorted(str(value) for value in clip_ids)
    if mode == "sequential":
        return ordered
    if mode != "seeded_random":
        raise TimelineError(f"bilinmeyen siralama: {mode!r}")
    if seed is None:
        raise TimelineError("seeded_random siralamasi bir seed gerektirir")
    shuffled = list(ordered)
    random.Random(int(seed)).shuffle(shuffled)
    return shuffled


# -------------------------------------------------------------------- packing
#: Suffixes treated as a still image rather than footage. A still has no
#: duration of its own, so it is looped for the length of its slot and given
#: motion by `zoompan` -- a static frame held for five seconds reads as a
#: broken video, not as a deliberate choice.
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})

KEN_BURNS_DIRECTIONS = ("in", "out", "left", "right")
DEFAULT_KEN_BURNS = {"direction": "in", "amount": 0.12}
MAX_KEN_BURNS_AMOUNT = 0.5


def clip_kind(source: dict) -> str:
    """`image` or `video`, decided by the source record, then by the suffix.

    An explicit `kind` from the provider wins; the suffix is the fallback for
    records written before images existed.
    """
    declared = str((source or {}).get("kind") or "").strip().lower()
    if declared in ("image", "video"):
        return declared
    suffix = Path(str((source or {}).get("relative_path") or "")).suffix.lower()
    return "image" if suffix in IMAGE_SUFFIXES else "video"


def normalize_ken_burns(values) -> dict:
    """Validated Ken Burns settings. Refuses rather than silently clamping to a
    default, because a typo in the direction would otherwise ship as `in`."""
    data = dict(values or {})
    direction = str(data.get("direction")
                    or DEFAULT_KEN_BURNS["direction"]).strip().lower()
    if direction not in KEN_BURNS_DIRECTIONS:
        raise TimelineError(
            "ken burns yonu su seceneklerden biri olmali: "
            + ", ".join(KEN_BURNS_DIRECTIONS))
    try:
        amount = round(float(data.get("amount", DEFAULT_KEN_BURNS["amount"])), 4)
    except (TypeError, ValueError) as exc:
        raise TimelineError("ken burns miktari bir sayi olmali") from exc
    if not (0.0 <= amount <= MAX_KEN_BURNS_AMOUNT):
        raise TimelineError(
            f"ken burns miktari 0 ile {MAX_KEN_BURNS_AMOUNT} arasinda olmali "
            f"(girilen: {amount})")
    return {"direction": direction, "amount": amount}


def _zoompan_statement(position: int, clip: dict, *, fps: int) -> str:
    """The Ken Burns move for one still image.

    `on` is the OUTPUT frame index, so the ramp is driven by frames actually
    emitted rather than by zoompan's `d` interacting with `-loop 1`. zoompan
    also carries its own fps (default 25); leaving it unset would resample the
    clip and break `concat`, which compares frame rates exactly.
    """
    span = _round(float(clip["timeline_end_sec"])
                  - float(clip["timeline_start_sec"]))
    total = max(1, int(round(span * fps)))
    settings = normalize_ken_burns(clip.get("ken_burns"))
    amount, direction = settings["amount"], settings["direction"]
    probed = clip.get("probed") or {}
    out_w = max(2, int(probed.get("width") or 1920))
    out_h = max(2, int(probed.get("height") or 1080))

    centre_y = "ih/2-(ih/zoom/2)"
    if direction == "in":
        zoom = f"1+{amount}*on/{total}"
        x, y = "iw/2-(iw/zoom/2)", centre_y
    elif direction == "out":
        zoom = f"1+{amount}-{amount}*on/{total}"
        x, y = "iw/2-(iw/zoom/2)", centre_y
    elif direction == "left":
        zoom = f"1+{amount}"
        x, y = f"(iw-iw/zoom)*(1-on/{total})", centre_y
    else:  # right
        zoom = f"1+{amount}"
        x, y = f"(iw-iw/zoom)*on/{total}", centre_y

    return (f"[{position}:v]fps={fps},setsar=1,"
            f"zoompan=z='{zoom}':x='{x}':y='{y}':d=1:fps={fps}:"
            f"s={out_w}x{out_h},"
            f"trim=duration={span},setpts=PTS-STARTPTS[pre_{position}]")


def pack_clips(sources, *, total_duration_sec: float, clip_duration_sec: float,
               fps: int, order: list, frame_defaults: dict,
               transition: str = "none", transition_sec: float = 0.0,
               ken_burns_defaults: dict | None = None) -> list:
    """Lay the ordered sources end to end so they exactly fill the narration.

    `sources` maps clip_id -> {relative_path, sha256, provider, ..., probed}.
    Returns clip entries with both timeline and source time ranges.

    When there is not enough footage the clips REPEAT, and every repeat is
    recorded in `reuse_index` so the UI can show it. Silently looping would let
    a 30-second video made from two clips look intentional.
    """
    if not order:
        raise TimelineError("zaman cizelgesi icin en az bir klip gerekir")
    total = float(total_duration_sec)
    if total <= 0:
        raise TimelineError("anlatim suresi olculmeden zaman cizelgesi paketlenemez")

    slot_count = max(1, int(ceil(total / float(clip_duration_sec))))
    if slot_count > MAX_CLIPS:
        raise TimelineError(
            f"{slot_count} klip gerekiyor; ust sinir {MAX_CLIPS}")

    clips = []
    used = {}
    cursor = 0.0
    for position in range(slot_count):
        clip_id = order[position % len(order)]
        source = sources.get(clip_id)
        if source is None:
            raise TimelineError(f"klip kaynagi bulunamadi: {clip_id!r}")
        # The last slot absorbs the remainder, so the timeline ends exactly on
        # the narration rather than one rounding error short of it.
        is_last = position == slot_count - 1
        span = _round(total - cursor) if is_last else float(clip_duration_sec)
        if span <= 0:
            break

        probed = source.get("probed") or {}
        kind = clip_kind(source)
        # A still has no timeline of its own to seek into, so the reuse/offset
        # arithmetic below would be meaningless for it.
        source_len = (0.0 if kind == "image"
                      else float(probed.get("duration_sec") or 0.0))
        reuse_index = used.get(clip_id, 0)
        used[clip_id] = reuse_index + 1

        if source_len > 0:
            # Each reuse starts further into the source when there is room, so a
            # repeated clip does not show the identical seconds twice.
            offset = min(reuse_index * span, max(0.0, source_len - span))
            source_in = _round(max(0.0, offset))
            available = source_len - source_in
            loops = 1 if available >= span else int(ceil(span / max(source_len, 0.04)))
        else:
            source_in, loops = 0.0, 1

        clips.append({
            "clip_id": f"c{position + 1:03d}",
            "index": position,
            "source_id": clip_id,
            "source": {key: source.get(key) for key in (
                "relative_path", "sha256", "provider", "asset_id",
                "source_page", "creator", "attribution", "license_note")},
            "probed": probed,
            "timeline_start_sec": _round(cursor),
            "timeline_end_sec": _round(cursor + span),
            "source_in_sec": source_in,
            "source_out_sec": _round(source_in + span),
            "stream_loop": 0 if kind == "image" else max(0, loops - 1),
            "kind": kind,
            # Only meaningful for images; kept off video clips so the render
            # fingerprint does not change for footage that never pans.
            "ken_burns": (normalize_ken_burns(ken_burns_defaults)
                          if kind == "image" else None),
            "reuse_index": reuse_index,
            "frame": dict(frame_defaults),
            "fade_in": bool(transition == "fade" and position > 0),
            "fade_out": bool(transition == "fade" and not is_last),
            "fade_sec": float(transition_sec) if transition == "fade" else 0.0,
            "source_audio": "dropped",
            "revision": 0,
        })
        cursor += span

    assert_gapless(clips, total)
    return clips


def assert_gapless(clips, total_duration_sec: float) -> None:
    """Reject a timeline that disagrees with itself.

    Checked before rendering rather than trusted, because the failure it catches
    -- narration drifting out of sync with the picture -- is invisible until
    someone watches the whole video.
    """
    if not clips:
        raise TimelineError("zaman cizelgesinde hic klip yok")
    if abs(float(clips[0]["timeline_start_sec"])) > GAP_TOLERANCE_SEC:
        raise TimelineError("ilk klip 0.0 saniyede baslamiyor")
    for earlier, later in zip(clips, clips[1:]):
        gap = float(later["timeline_start_sec"]) - float(earlier["timeline_end_sec"])
        if abs(gap) > GAP_TOLERANCE_SEC:
            raise TimelineError(
                f"{earlier['clip_id']} ile {later['clip_id']} arasinda "
                f"{gap:+.3f} sn bosluk/cakisma var")
    drift = float(clips[-1]["timeline_end_sec"]) - float(total_duration_sec)
    if abs(drift) > GAP_TOLERANCE_SEC:
        raise TimelineError(
            f"zaman cizelgesi anlatimdan {drift:+.3f} sn sapiyor")


# ------------------------------------------------------------------ manifest
def new_manifest(*, clips, narration: dict, video: dict, audio: dict,
                 ordering: dict, subtitles: dict | None = None,
                 settings_snapshot: dict | None = None, ownership=None) -> dict:
    record = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": ARTIFACT_TYPE,
        "status": "review_required",
        "not_uploaded": True,
        "video": dict(video),
        "ordering": dict(ordering),
        "duration_sec": _round(narration.get("duration_sec") or 0.0),
        "gap_policy": "no_gaps",
        "clips": list(clips),
        "narration": dict(narration),
        "subtitles": dict(subtitles or {"enabled": False}),
        "audio": dict(audio),
        "settings_snapshot": dict(settings_snapshot or {}),
        "attribution_required": True,
        "attribution_lines": sorted({
            (clip.get("source") or {}).get("attribution")
            for clip in clips
            if (clip.get("source") or {}).get("attribution")}),
        "reuse_count": sum(1 for clip in clips if clip.get("reuse_index")),
        "created_at": _now(),
        "updated_at": _now(),
    }
    record["render_fingerprint"] = render_fingerprint(record)
    return record


def render_fingerprint(manifest: dict) -> str:
    """Identity of everything that affects the pixels.

    Lets a re-render of an unchanged manifest be reported as "already rendered"
    instead of burning ten minutes, and lets a revision prove it changed
    something.
    """
    material = {
        "video": manifest.get("video"),
        "duration": manifest.get("duration_sec"),
        "ordering": (manifest.get("ordering") or {}).get("clip_id_order"),
        "clips": [
            {key: clip.get(key) for key in (
                "clip_id", "source_in_sec", "source_out_sec",
                "timeline_start_sec", "timeline_end_sec", "stream_loop",
                "frame", "fade_in", "fade_out", "fade_sec",
                "kind", "ken_burns")}
            | {"sha256": (clip.get("source") or {}).get("sha256")}
            for clip in manifest.get("clips") or []],
        "subtitles": {key: (manifest.get("subtitles") or {}).get(key)
                      for key in ("enabled", "sha256", "style")},
        "audio": manifest.get("audio"),
        "encode": (manifest.get("settings_snapshot") or {}).get("encode"),
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ------------------------------------------------------------- filtergraph
def build_filter_complex(manifest: dict, *, subtitle_path=None) -> str:
    """Compile the manifest into one ffmpeg filtergraph.

    Every clip gets its own label namespace, which is the defect that made
    `scene_editor`'s attempt invalid: `build_vertical_filtergraph` emits five
    labels for `blurred_background`, so renaming only the input and output would
    still collide on four of them.
    """
    clips = manifest.get("clips") or []
    assert_gapless(clips, manifest.get("duration_sec") or 0.0)
    video = manifest.get("video") or {}
    width = int(video.get("width") or 1080)
    height = int(video.get("height") or 1920)
    fps = int(video.get("fps") or 30)

    statements = []
    for position, clip in enumerate(clips):
        span = _round(float(clip["timeline_end_sec"])
                      - float(clip["timeline_start_sec"]))
        frame = clip.get("frame") or {}
        mode = ("blurred_background" if frame.get("mode") == "fit"
                else "center_crop")
        # `setsar=1` BEFORE the reframe: a source with a non-square pixel aspect
        # ratio (common in stock footage derived from DV) makes `concat` abort
        # with "Input link parameters do not match". One `fps=` per clip for the
        # same reason -- concat requires identical frame rates.
        if clip.get("kind") == "image":
            statements.append(_zoompan_statement(position, clip, fps=fps))
        else:
            statements.append(
                f"[{position}:v]trim=duration={span},setpts=PTS-STARTPTS,"
                f"fps={fps},setsar=1[pre_{position}]")
        statements.append(render_utils.build_vertical_filtergraph(
            mode, width, height,
            blur_sigma=int(frame.get("blur_sigma") or 20),
            zoom_scale=float(frame.get("zoom") or 1.0),
            focus_x=float(frame.get("focus_x") or 0.5),
            focus_y=float(frame.get("focus_y") or 0.5),
            in_label=f"pre_{position}", out_label=f"framed_{position}",
            label_suffix=f"_{position}"))

        fades = []
        fade_sec = float(clip.get("fade_sec") or 0.0)
        if clip.get("fade_in") and fade_sec > 0:
            fades.append(f"fade=t=in:st=0:d={fade_sec}")
        if clip.get("fade_out") and fade_sec > 0 and span > fade_sec:
            fades.append(f"fade=t=out:st={_round(span - fade_sec)}:d={fade_sec}")
        # `setsar=1` a SECOND time, after the reframe. The first one normalises
        # the source; the scale+crop that follows then RE-DERIVES a sample aspect
        # ratio to preserve display aspect, and 1920x1080 -> 1080x1920 lands on
        # SAR 10240:10239 rather than 1:1. concat compares SAR exactly, so one
        # clip off by a ten-thousandth aborts the whole render. Measured, not
        # guessed: this is the error the first real three-clip render produced.
        chain = ",".join(["format=yuv420p", "setsar=1"] + fades)
        statements.append(f"[framed_{position}]{chain}[v{position}]")

    joined = "".join(f"[v{i}]" for i in range(len(clips)))
    statements.append(f"{joined}concat=n={len(clips)}:v=1:a=0[vcat]")

    # Subtitles are burned ONCE, after the concat: a cue that spans a clip
    # boundary would otherwise be cut in half by the clip it started in.
    if subtitle_path:
        style = _force_style((manifest.get("subtitles") or {}).get("style") or {})
        escaped = render_utils.escape_path_for_filter(subtitle_path)
        statements.append(
            f"[vcat]subtitles='{escaped}'"
            + (f":force_style='{style}'" if style else "") + "[vout]")
    else:
        statements.append("[vcat]null[vout]")

    statements.append(_audio_statements(manifest, len(clips)))
    return ";".join(part for part in statements if part)


def _force_style(style: dict) -> str:
    pairs = []
    for key, value in sorted((style or {}).items()):
        if value in (None, ""):
            continue
        name = "".join(part.capitalize() for part in str(key).split("_"))
        pairs.append(f"{name}={value}")
    return ",".join(pairs)


def _audio_statements(manifest: dict, clip_count: int) -> str:
    """Narration is input N; music, when present, is N+1.

    Stock-clip audio is never referenced -- no `[i:a]` appears in the graph --
    so it is dropped by construction rather than by remembering to mute it.
    """
    audio = manifest.get("audio") or {}
    music = audio.get("music") or {}
    narration_label = f"{clip_count}:a"
    narration_volume = float(audio.get("narration_volume") or 1.0)
    if not music.get("present"):
        return render_utils.build_audio_filtergraph(
            False, False, 8.0, 0.0, narration_volume,
            narration_label=narration_label, out_label="aout")
    ducking = audio.get("ducking") or {}
    bed = (f"[{clip_count + 1}:a]atrim=0:{manifest.get('duration_sec')},"
           f"asetpts=N/SR/TB[bed_src]")
    mix = render_utils.build_audio_filtergraph(
        True, bool(ducking.get("enabled", True)),
        float(ducking.get("duck_ratio") or 8.0),
        float(music.get("volume") or 0.18), narration_volume,
        bed_label="bed_src", narration_label=narration_label, out_label="aout")
    return f"{bed};{mix}"


# ---------------------------------------------------------------- invocation
def build_ffmpeg_args(manifest: dict, *, resolve, narration_path,
                      output_path, graph_path, music_path=None,
                      preset: str = "final") -> list:
    """The complete argv, with the graph read from a file.

    `-/filter_complex <file>` rather than inline: 216 clips produce roughly 19KB
    of filter text, and Windows' command line stops at 32,768 characters. The
    per-input arguments alone are another 17KB, which is why `MAX_CLIPS` exists
    on top of this.
    """
    clips = manifest.get("clips") or []
    video = manifest.get("video") or {}
    fps = int(video.get("fps") or 30)
    scale, crf, encode_preset = _encode_profile(manifest, preset)
    width = max(2, int(int(video.get("width") or 1080) * scale) // 2 * 2)
    height = max(2, int(int(video.get("height") or 1920) * scale) // 2 * 2)

    args = []
    for clip in clips:
        span = _round(float(clip["timeline_end_sec"])
                      - float(clip["timeline_start_sec"]))
        if clip.get("kind") == "image":
            # A still is decoded once and looped for the slot. `-ss` would seek
            # inside a file that has nothing to seek into.
            args += ["-loop", "1", "-t", str(span), "-i", str(resolve(clip))]
            continue
        if int(clip.get("stream_loop") or 0) > 0:
            args += ["-stream_loop", str(int(clip["stream_loop"]))]
        args += ["-ss", str(clip["source_in_sec"]), "-t", str(span),
                 "-i", str(resolve(clip))]
    args += ["-i", str(narration_path)]
    if music_path is not None:
        if (manifest.get("audio") or {}).get("music", {}).get("loop"):
            args += ["-stream_loop", "-1"]
        args += ["-i", str(music_path)]

    args += [
        "-/filter_complex", str(graph_path),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", encode_preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-r", str(fps),
        # Two channels always: a 5.1 narration bed silently loses the centre
        # channel in some players otherwise.
        "-ac", "2", "-c:a", "aac",
        "-shortest", "-movflags", "+faststart",
        "-s", f"{width}x{height}", str(output_path),
    ]
    return args


def _encode_profile(manifest: dict, preset: str):
    """Preview trades quality for turnaround; final does the opposite."""
    if preset == "preview":
        return 0.5, 30, "ultrafast"
    if preset == "final":
        return 1.0, 18, "slow"
    raise TimelineError(f"bilinmeyen render profili: {preset!r}")


def write_graph(graph: str, path) -> Path:
    """Write the filtergraph beside the render, atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".part")
    temp.write_text(graph, encoding="utf-8")
    temp.replace(path)
    return path


# ----------------------------------------------------------------- revisions
def set_frame(manifest: dict, clip_id: str, **frame) -> dict:
    """Change one clip's framing in place. Nothing upstream is touched."""
    manifest = dict(manifest)
    clips = [dict(clip) for clip in manifest.get("clips") or []]
    target = next((clip for clip in clips if clip["clip_id"] == clip_id), None)
    if target is None:
        raise TimelineError(f"bilinmeyen klip: {clip_id!r}")
    merged = dict(target.get("frame") or {})
    for key, value in frame.items():
        if value is not None:
            merged[key] = value
    if merged.get("mode") not in ("crop", "fit"):
        raise TimelineError("kadraj modu 'crop' veya 'fit' olmali")
    zoom = float(merged.get("zoom") or 1.0)
    if not (1.0 <= zoom <= 2.0):
        raise TimelineError("zoom 1.0 ile 2.0 arasinda olmali")
    target["frame"] = merged
    target["revision"] = int(target.get("revision") or 0) + 1
    manifest["clips"] = clips
    manifest["updated_at"] = _now()
    manifest["render_fingerprint"] = render_fingerprint(manifest)
    return manifest


def retime_clip(manifest: dict, clip_id: str, seconds: float, *,
                policy: str = "spread_forward") -> dict:
    """Give one clip a different length and absorb the difference after it.

    The total must not move: it equals the narration, and a timeline that no
    longer does would desync everything downstream of the change. So the delta
    is taken from the FOLLOWING clips, never from the total.
    """
    manifest = dict(manifest)
    clips = [dict(clip) for clip in manifest.get("clips") or []]
    index = next((i for i, clip in enumerate(clips)
                  if clip["clip_id"] == clip_id), None)
    if index is None:
        raise TimelineError(f"bilinmeyen klip: {clip_id!r}")
    if index == len(clips) - 1:
        raise TimelineError(
            "son klip yeniden zamanlanamaz; farki emecek klip kalmiyor")

    current = float(clips[index]["timeline_end_sec"]) - float(
        clips[index]["timeline_start_sec"])
    new_span = float(seconds)
    if new_span < 0.5:
        raise TimelineError("klip suresi en az 0.5 saniye olmali")
    delta = new_span - current
    followers = clips[index + 1:]
    room = sum(float(c["timeline_end_sec"]) - float(c["timeline_start_sec"])
               for c in followers) - 0.5 * len(followers)
    if delta > room:
        raise TimelineError(
            f"sonraki klipler bu uzatmayi ememez (en fazla +{room:.2f} sn)")

    if policy == "spread_forward":
        share = delta / len(followers)
        spans = [max(0.5, (float(c["timeline_end_sec"])
                           - float(c["timeline_start_sec"])) - share)
                 for c in followers]
    elif policy == "absorb_last":
        spans = [float(c["timeline_end_sec"]) - float(c["timeline_start_sec"])
                 for c in followers]
        spans[-1] = max(0.5, spans[-1] - delta)
    else:
        raise TimelineError(f"bilinmeyen yeniden zamanlama politikasi: {policy!r}")

    cursor = float(clips[index]["timeline_start_sec"])
    for clip, span in zip([clips[index]] + followers, [new_span] + spans):
        clip["timeline_start_sec"] = _round(cursor)
        clip["timeline_end_sec"] = _round(cursor + span)
        clip["source_out_sec"] = _round(float(clip["source_in_sec"]) + span)
        clip["revision"] = int(clip.get("revision") or 0) + 1
        cursor += span

    # The last clip absorbs any rounding residue so the total still lands.
    total = float(manifest.get("duration_sec") or 0.0)
    clips[-1]["timeline_end_sec"] = _round(total)
    clips[-1]["source_out_sec"] = _round(
        float(clips[-1]["source_in_sec"])
        + total - float(clips[-1]["timeline_start_sec"]))

    assert_gapless(clips, total)
    manifest["clips"] = clips
    manifest["retime_policy"] = policy
    manifest["updated_at"] = _now()
    manifest["render_fingerprint"] = render_fingerprint(manifest)
    return manifest


def summarise(manifest: dict) -> dict:
    """What the timeline strip in the UI needs, without the whole manifest."""
    clips = (manifest or {}).get("clips") or []
    return {
        "clip_count": len(clips),
        "duration_sec": (manifest or {}).get("duration_sec"),
        "reuse_count": sum(1 for clip in clips if clip.get("reuse_index")),
        "distinct_sources": len({clip.get("source_id") for clip in clips}),
        "ordering": ((manifest or {}).get("ordering") or {}).get("mode"),
        "seed": ((manifest or {}).get("ordering") or {}).get("seed"),
        "render_fingerprint": (manifest or {}).get("render_fingerprint"),
        "attribution_lines": (manifest or {}).get("attribution_lines") or [],
        "clips": [
            {"clip_id": clip["clip_id"],
             "start": clip["timeline_start_sec"],
             "end": clip["timeline_end_sec"],
             "provider": (clip.get("source") or {}).get("provider"),
             "attribution": (clip.get("source") or {}).get("attribution"),
             "reuse_index": clip.get("reuse_index"),
             "frame": clip.get("frame")}
            for clip in clips],
    }
