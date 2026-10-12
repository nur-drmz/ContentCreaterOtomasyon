"""Scene Editor — EDL (Edit Decision List) model and preview compiler.

Faz 2 başlangıç noktası. Bu modül; trim, zoom, crop, caption stili,
B-roll ve müzik katmanlarını taşıyan bir EDL artefaktı tanımlar ve bu EDL'yi
FFmpeg filtergraph parçalarına derler.

Önemli: `render_utils.py` zaten 9:16 vertical reframe, altyazı ve audio ducking
üretiyor. Scene editor o mevcut grafiği "klip listesi" kavramına genişletir;
mevcut render kodunu tekrar yazmaz.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .render_utils import build_vertical_filtergraph, escape_path_for_filter


@dataclass
class Box:
    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0

    def is_valid(self) -> bool:
        return (
            0.0 <= self.x <= 1.0
            and 0.0 <= self.y <= 1.0
            and self.w > 0.0
            and self.h > 0.0
            and self.x + self.w <= 1.0 + 1e-9
            and self.y + self.h <= 1.0 + 1e-9
        )


@dataclass
class Zoom:
    scale: float = 1.0
    anchor_x: float = 0.5
    anchor_y: float = 0.5

    def is_valid(self) -> bool:
        return (
            self.scale >= 1.0
            and 0.0 <= self.anchor_x <= 1.0
            and 0.0 <= self.anchor_y <= 1.0
        )


@dataclass
class CaptionStyle:
    preset: str = "active_word_yellow"
    font_size: int = 24
    margin_v: int = 50
    word_color: str = "&H00FFFFFF"
    active_color: str = "&H0000FFFF"

    def is_valid(self) -> bool:
        presets = ("active_word_yellow", "classic_white", "lower_third")
        return self.preset in presets


@dataclass
class BRoll:
    media_path: str = ""
    start_sec: float = 0.0
    duration_sec: float = 0.0
    position: str = "overlay"  # overlay, split_screen
    opacity: float = 0.7

    def is_valid(self) -> bool:
        return (
            self.media_path
            and self.duration_sec > 0.0
            and 0.0 <= self.opacity <= 1.0
        )


@dataclass
class EDLClip:
    clip_id: str = ""
    source_path: str = ""
    source_start_sec: float = 0.0
    source_end_sec: float = 0.0
    timeline_start_sec: float = 0.0
    crop: Box = field(default_factory=Box)
    zoom: Zoom = field(default_factory=Zoom)
    caption_style: CaptionStyle = field(default_factory=CaptionStyle)
    b_roll: list[BRoll] = field(default_factory=list)
    notes: str = ""

    def duration(self) -> float:
        return max(0.0, self.source_end_sec - self.source_start_sec)

    def is_valid(self, source_duration: float | None = None) -> bool:
        if not self.clip_id or not self.source_path:
            return False
        if self.source_end_sec <= self.source_start_sec:
            return False
        if self.source_start_sec < 0:
            return False
        if source_duration is not None and self.source_end_sec > source_duration:
            return False
        return self.crop.is_valid() and self.zoom.is_valid() and self.caption_style.is_valid()


@dataclass
class EDL:
    title: str = ""
    width: int = 1080
    height: int = 1920
    render_mode: str = "center_crop"  # center_crop, blurred_background
    clips: list[EDLClip] = field(default_factory=list)
    music_track: str | None = None
    revision_notes: str = ""
    revision_requested_by: str = ""
    created_at: str = ""
    version: int = 1

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    def total_duration(self) -> float:
        return sum(c.duration() for c in self.clips)

    def is_valid(self, source_durations: dict | None = None) -> bool:
        if self.width <= 0 or self.height <= 0:
            return False
        if self.render_mode not in ("center_crop", "blurred_background"):
            return False
        for clip in self.clips:
            source_duration = None
            if source_durations is not None:
                source_duration = source_durations.get(clip.source_path)
            if not clip.is_valid(source_duration):
                return False
        return True


def build_edl(source_path: str, source_duration: float, title: str = "",
              *, width: int = 1080, height: int = 1920,
              render_mode: str = "center_crop") -> EDL:
    """Construct a default EDL that covers the whole source, unedited."""
    clip = EDLClip(
        clip_id="clip_001",
        source_path=source_path,
        source_start_sec=0.0,
        source_end_sec=source_duration,
    )
    return EDL(
        title=title,
        width=width,
        height=height,
        render_mode=render_mode,
        clips=[clip],
    )


def trim_clip(clip: EDLClip, start: float, end: float,
              source_duration: float | None = None) -> EDLClip:
    """Return a new clip trimmed to [start, end) within the current clip."""
    if start < 0:
        raise ValueError("trim baslangici negatif olamaz")
    if end <= start:
        raise ValueError("trim bitisi baslangictan buyuk olmali")
    if source_duration is not None:
        if end > source_duration:
            raise ValueError("trim bitisi kaynak suresini asamaz")
    if end > clip.source_end_sec:
        raise ValueError("trim bitisi klip sonunu asamaz")
    if start < clip.source_start_sec:
        raise ValueError("trim baslangici klip basindan once olamaz")
    new_clip = EDLClip(
        clip_id=clip.clip_id,
        source_path=clip.source_path,
        source_start_sec=start,
        source_end_sec=end,
        timeline_start_sec=clip.timeline_start_sec,
        crop=clip.crop,
        zoom=clip.zoom,
        caption_style=clip.caption_style,
        b_roll=clip.b_roll,
        notes=clip.notes,
    )
    if not new_clip.is_valid(source_duration):
        raise ValueError("trim sonucu gecersiz klip")
    return new_clip


MIN_CLIP_DURATION_SEC = 0.2


def set_clip_source_bounds(clip: EDLClip, start: float, end: float,
                           source_duration: float | None = None) -> EDLClip:
    """Move and/or resize a clip ANYWHERE inside the source.

    `trim_clip` only ever narrows: it refuses a start before the current one and
    an end after it. That is correct for "trim", and it is kept -- but it also
    made the chosen range permanent. An operator who found the moment starts a
    second too late had no way to reach back; the only route was re-running the
    analysis and picking a different moment, which throws away the transcript
    and the script with it.

    This function is the editable version. The ONLY limits are the source
    itself and a floor on duration, because a zero-length clip is not an edit,
    it is a broken render.
    """
    try:
        start = float(start)
        end = float(end)
    except (TypeError, ValueError) as exc:
        raise ValueError("klip sinirlari sayisal olmali") from exc
    if start != start or end != end:            # NaN
        raise ValueError("klip sinirlari sayisal olmali")
    if start < 0:
        raise ValueError("klip baslangici negatif olamaz")
    if end <= start:
        raise ValueError("klip bitisi baslangictan buyuk olmali")
    if end - start < MIN_CLIP_DURATION_SEC:
        raise ValueError(
            f"klip suresi en az {MIN_CLIP_DURATION_SEC} saniye olmali "
            f"(istenen: {end - start:.3f} sn)")
    if source_duration is not None and end > float(source_duration):
        raise ValueError("klip bitisi kaynak suresini asamaz")
    new_clip = EDLClip(
        clip_id=clip.clip_id,
        source_path=clip.source_path,
        source_start_sec=start,
        source_end_sec=end,
        timeline_start_sec=clip.timeline_start_sec,
        crop=clip.crop,
        zoom=clip.zoom,
        caption_style=clip.caption_style,
        b_roll=clip.b_roll,
        notes=clip.notes,
    )
    if not new_clip.is_valid(source_duration):
        raise ValueError("yeni klip sinirlari gecersiz")
    return new_clip


def shift_clip(clip: EDLClip, delta: float,
               source_duration: float | None = None) -> EDLClip:
    """Slide the clip along the source, PRESERVING its duration.

    Separate from `set_clip_source_bounds` because "the moment starts a second
    late" and "the moment should be a second longer" are different edits, and a
    drag on the middle of a clip must not silently change how long it runs.

    Hitting either end of the source clamps instead of raising: a drag that runs
    into the boundary should stop there, the way every timeline behaves.
    """
    duration = clip.source_end_sec - clip.source_start_sec
    start = clip.source_start_sec + float(delta)
    start = max(0.0, start)
    if source_duration is not None:
        start = min(start, max(0.0, float(source_duration) - duration))
    return set_clip_source_bounds(clip, start, start + duration, source_duration)


def crop_clip(clip: EDLClip, x: float, y: float, w: float, h: float) -> EDLClip:
    new_crop = Box(x, y, w, h)
    if not new_crop.is_valid():
        raise ValueError("crop alani [0,1] icerisinde, pozitif ve sinirlari asmamali")
    return EDLClip(
        clip_id=clip.clip_id,
        source_path=clip.source_path,
        source_start_sec=clip.source_start_sec,
        source_end_sec=clip.source_end_sec,
        timeline_start_sec=clip.timeline_start_sec,
        crop=new_crop,
        zoom=clip.zoom,
        caption_style=clip.caption_style,
        b_roll=clip.b_roll,
        notes=clip.notes,
    )


def zoom_clip(clip: EDLClip, scale: float, anchor_x: float = 0.5,
              anchor_y: float = 0.5) -> EDLClip:
    new_zoom = Zoom(scale, anchor_x, anchor_y)
    if not new_zoom.is_valid():
        raise ValueError("zoom scale >= 1 ve anchor [0,1] icinde olmali")
    return EDLClip(
        clip_id=clip.clip_id,
        source_path=clip.source_path,
        source_start_sec=clip.source_start_sec,
        source_end_sec=clip.source_end_sec,
        timeline_start_sec=clip.timeline_start_sec,
        crop=clip.crop,
        zoom=new_zoom,
        caption_style=clip.caption_style,
        b_roll=clip.b_roll,
        notes=clip.notes,
    )


def set_caption_style(clip: EDLClip, style: CaptionStyle) -> EDLClip:
    if not style.is_valid():
        raise ValueError("bilinmeyen caption stili")
    return EDLClip(
        clip_id=clip.clip_id,
        source_path=clip.source_path,
        source_start_sec=clip.source_start_sec,
        source_end_sec=clip.source_end_sec,
        timeline_start_sec=clip.timeline_start_sec,
        crop=clip.crop,
        zoom=clip.zoom,
        caption_style=style,
        b_roll=clip.b_roll,
        notes=clip.notes,
    )


def add_b_roll(clip: EDLClip, b_roll: BRoll) -> EDLClip:
    if not b_roll.is_valid():
        raise ValueError("gecersiz B-roll")
    new_list = list(clip.b_roll) + [b_roll]
    return EDLClip(
        clip_id=clip.clip_id,
        source_path=clip.source_path,
        source_start_sec=clip.source_start_sec,
        source_end_sec=clip.source_end_sec,
        timeline_start_sec=clip.timeline_start_sec,
        crop=clip.crop,
        zoom=clip.zoom,
        caption_style=clip.caption_style,
        b_roll=new_list,
        notes=clip.notes,
    )


def set_music_track(edl: EDL, music_path: str) -> EDL:
    return EDL(
        title=edl.title,
        width=edl.width,
        height=edl.height,
        render_mode=edl.render_mode,
        clips=edl.clips,
        music_track=music_path,
        created_at=edl.created_at,
        version=edl.version,
    )


def _zoom_crop_to_filtergraph(clip: EDLClip, input_idx: int, width: int,
                              height: int) -> tuple[str, str]:
    """Generate a filtergraph that applies crop + zoom to a clip input.

    Returns the filtergraph statement and the output pad label.
    """
    box = clip.crop
    zoom = clip.zoom
    in_pad = f"[{input_idx}:v]"
    crop_w = f"iw*{box.w:.4f}"
    crop_h = f"ih*{box.h:.4f}"
    crop_x = f"iw*{box.x:.4f}"
    crop_y = f"ih*{box.y:.4f}"
    # Crop then scale by zoom factor; the vertical filtergraph then reframes.
    scale_w = f"{width * zoom.scale:.0f}"
    scale_h = f"{height * zoom.scale:.0f}"
    out_pad = f"[preclip_{input_idx}]"
    statement = (
        f"{in_pad}crop={crop_w}:{crop_h}:{crop_x}:{crop_y},"
        f"scale={scale_w}:{scale_h}:flags=lanczos{out_pad}"
    )
    return statement, out_pad[1:-1]


def build_preview_filtergraph(edl: EDL) -> tuple[str, int]:
    """Return an ffmpeg -filter_complex string for the EDL preview.

    Supports multi-clip concatenation and B-roll picture-in-picture overlays.
    Returns (filter_complex, total_input_count) so the caller can append all
    media inputs in the right order.
    """
    if not edl.clips:
        raise ValueError("EDL'de klip yok")

    clip_statements: list[str] = []
    framed_labels: list[str] = []
    input_idx = 0
    broll_input_offset = len(edl.clips)
    broll_input_idx = broll_input_offset

    for clip in edl.clips:
        zoom_stmt, pre_label = _zoom_crop_to_filtergraph(
            clip, input_idx, edl.width, edl.height,
        )
        clip_statements.append(zoom_stmt)
        vertical = build_vertical_filtergraph(edl.render_mode, edl.width, edl.height)
        framed_label = f"framed_{input_idx}"
        clip_statements.append(
            f"[{pre_label}]{vertical}[{framed_label}]"
        )

        base_label = framed_label
        for b in clip.b_roll:
            if not b.is_valid():
                raise ValueError(f"gecersiz B-roll: {b}")
            # Overlay the B-roll at 70% opacity, centered, scaled to 30% width.
            ol_label = f"ol_{broll_input_idx}"
            stmt = (
                f"[{broll_input_idx}:v]scale=iw*0.3:-2:flags=lanczos,"
                f"format=rgba,colorchannelmixer=aa={b.opacity:.2f}[{ol_label}]"
            )
            clip_statements.append(stmt)
            tmp_label = f"tmp_{input_idx}_{broll_input_idx}"
            stmt = (
                f"[{base_label}][{ol_label}]overlay="
                f"(main_w-overlay_w)/2:(main_h-overlay_h)/2[{tmp_label}]"
            )
            clip_statements.append(stmt)
            base_label = tmp_label
            broll_input_idx += 1

        framed_labels.append(base_label)
        input_idx += 1

    # Concatenate all finished clip streams.
    concat_inputs = "".join(f"[{lbl}]" for lbl in framed_labels)
    concat_n = len(framed_labels)
    concat_stmt = f"{concat_inputs}concat=n={concat_n}:v=1:a=0[concatv]"
    clip_statements.append(concat_stmt)
    clip_statements.append("[concatv]format=yuv420p[vout]")

    return ";".join(clip_statements), broll_input_idx


def build_preview_command(edl: EDL, output_path: Path | str) -> list[str]:
    """Build a complete ffmpeg command list for the EDL preview."""
    out = Path(output_path)
    filter_complex, total_inputs = build_preview_filtergraph(edl)
    cmd = ["ffmpeg", "-y"]
    for clip in edl.clips:
        cmd.extend(["-ss", str(clip.source_start_sec)])
        cmd.extend(["-t", str(clip.duration())])
        cmd.extend(["-i", clip.source_path])
    for clip in edl.clips:
        for b in clip.b_roll:
            # B-roll inputs are added after all clip inputs.
            cmd.extend(["-ss", str(b.start_sec)])
            cmd.extend(["-t", str(b.duration_sec)])
            cmd.extend(["-i", b.media_path])
    cmd.extend([
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-an",  # preview is silent for now
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out),
    ])
    return cmd


def render_preview(edl: EDL, output_path: Path | str, *,
                   ffmpeg_bin: str = "ffmpeg") -> dict:
    """Render the EDL preview by executing ffmpeg.

    Returns a dict with command, returncode, stdout and stderr. The caller
    decides whether a non-zero returncode is an error (e.g. missing source
    media in a test environment).
    """
    import subprocess
    cmd = build_preview_command(edl, output_path)
    cmd[0] = ffmpeg_bin
    result = subprocess.run(cmd, capture_output=True, text=True)
    return {
        "command": cmd,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def add_clip_to_edl(edl: EDL, clip: EDLClip) -> EDL:
    return EDL(
        title=edl.title,
        width=edl.width,
        height=edl.height,
        render_mode=edl.render_mode,
        clips=list(edl.clips) + [clip],
        music_track=edl.music_track,
        created_at=edl.created_at,
        version=edl.version,
    )


def edl_to_artifact(edl: EDL) -> dict[str, Any]:
    """Wrap the EDL with the canonical artifact envelope."""
    payload = asdict(edl)
    payload["artifact_type"] = "edl"
    payload["status"] = "draft"
    return payload


def _rebuild_box(data: dict) -> Box:
    return Box(**{k: v for k, v in data.items() if k in Box.__dataclass_fields__})


def _rebuild_zoom(data: dict) -> Zoom:
    return Zoom(**{k: v for k, v in data.items() if k in Zoom.__dataclass_fields__})


def _rebuild_caption(data: dict) -> CaptionStyle:
    return CaptionStyle(**{k: v for k, v in data.items() if k in CaptionStyle.__dataclass_fields__})


def _rebuild_broll(data: dict) -> BRoll:
    return BRoll(**{k: v for k, v in data.items() if k in BRoll.__dataclass_fields__})


def _rebuild_clip(data: dict) -> EDLClip:
    clip = {k: v for k, v in data.items() if k in EDLClip.__dataclass_fields__}
    if "crop" in clip:
        clip["crop"] = _rebuild_box(clip["crop"])
    if "zoom" in clip:
        clip["zoom"] = _rebuild_zoom(clip["zoom"])
    if "caption_style" in clip:
        clip["caption_style"] = _rebuild_caption(clip["caption_style"])
    if "b_roll" in clip and clip["b_roll"]:
        clip["b_roll"] = [_rebuild_broll(b) for b in clip["b_roll"]]
    return EDLClip(**clip)


def edl_from_artifact(data: dict[str, Any]) -> EDL:
    if data.get("artifact_type") != "edl":
        raise ValueError("artefakt turu 'edl' degil")
    edl = {k: v for k, v in data.items() if k in EDL.__dataclass_fields__}
    if "clips" in edl:
        edl["clips"] = [_rebuild_clip(c) for c in edl["clips"]]
    return EDL(**edl)
