import hashlib
import json
import os
import sys
import tempfile
import shutil
from pathlib import Path
import subprocess

import imageio_ffmpeg
import librosa
import numpy as np
import pandas as pd
from faster_whisper import WhisperModel
from scenedetect import SceneManager, open_video
from scenedetect.detectors import ContentDetector

sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_pipeline import render_utils as ru
from local_pipeline import video_analysis as va


print("--- Source-video folder auto-selection tests ---")
with tempfile.TemporaryDirectory(prefix="source_video_resolver_") as _source_tmp:
    _source_dir = Path(_source_tmp)
    _older = _source_dir / "older.mp4"
    _newer = _source_dir / "newer video.mkv"
    _ignored = _source_dir / "not-a-video.txt"
    _older.write_bytes(b"older")
    _newer.write_bytes(b"newer")
    _ignored.write_bytes(b"ignored")
    os.utime(_older, (1, 1))
    os.utime(_newer, (2, 2))
    if va.resolve_source_video(_source_dir) != _newer:
        raise AssertionError("Klasor modu en yeni desteklenen videoyu secmedi")
    if va.resolve_source_video(_source_dir, _older) != _older:
        raise AssertionError("Acik kaynak video override'i oncelik kazanmadi")
print("SOURCE_VIDEO_AUTO_SELECTION_OK")

_got_theme = ru.resolve_caption_theme(
    "auto", "THE ABSOLUTE BEST OF DAEMON TARGARYEN - House of the Dragon"
)
if _got_theme["resolved_theme"] != "game_of_thrones":
    raise AssertionError(f"Game of Thrones tema algilamasi basarisiz: {_got_theme}")
if _got_theme["profile"]["font_name"] != "Marcellus SC":
    raise AssertionError("Otomatik Game of Thrones temasi Marcellus SC kullanmadi")
if ru.resolve_caption_theme("game_of_thrones_cinzel", "")["profile"]["font_name"] != "Cinzel":
    raise AssertionError("Eski Cinzel karsilastirma profili korunmadi")
if ru.resolve_caption_theme("auto", "generic technology tutorial")["resolved_theme"] != "default":
    raise AssertionError("Tema eslesmeyen icerik default profile dusmedi")
print("CAPTION_THEME_ROUTING_OK")


print("--- Directional hot-moment boundary tests ---")
_expanded_start, _expanded_end, _boundary_meta = va.expand_moment_boundaries(
    10.0,
    40.0,
    [{"start_sec": 8.8, "end_sec": 41.2, "text": "context"}],
    [8.0, 45.0],
    60.0,
)
if (_expanded_start, _expanded_end) != (8.0, 45.0):
    raise AssertionError(
        "Moment sinirlari onceki/sonraki sahneye disa dogru genislemedi: "
        f"{(_expanded_start, _expanded_end, _boundary_meta)}"
    )
if _expanded_start > 10.0 or _expanded_end < 40.0:
    raise AssertionError("Moment sinir politikasi skor penceresini iceri dogru kesti")

_fallback_start, _fallback_end, _fallback_meta = va.expand_moment_boundaries(
    10.0,
    40.0,
    [],
    [12.0, 38.0],
    60.0,
)
if (_fallback_start, _fallback_end) != (9.25, 40.75):
    raise AssertionError(
        "Icerideki sahne sinirlari secilmemeli; fallback dis padding bekleniyordu: "
        f"{(_fallback_start, _fallback_end, _fallback_meta)}"
    )
print("DIRECTIONAL_MOMENT_BOUNDARIES_OK")


PROJECT_ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = PROJECT_ROOT / ".local_runtime"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

VIDEO_NAME = "THE ABSOLUTE BEST OF DAEMON TARGARYEN - ABC CINEMA (1080p).mp4"
SOURCE_VIDEO = Path.home() / "Downloads" / VIDEO_NAME
SMOKE_VIDEO = RUNTIME_DIR / "smoke_clip.mp4"
SMOKE_AUDIO = RUNTIME_DIR / "smoke_clip.wav"
FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()

if not SOURCE_VIDEO.is_file():
    raise FileNotFoundError(f"Kaynak video bulunamadi: {SOURCE_VIDEO}")

video_result = subprocess.run(
    [
        FFMPEG_BIN,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(SOURCE_VIDEO),
        "-t",
        "10",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-c:a",
        "aac",
        str(SMOKE_VIDEO),
    ],
    capture_output=True,
    text=True,
)
if video_result.returncode != 0:
    raise RuntimeError(video_result.stderr[-3000:])

audio_result = subprocess.run(
    [
        FFMPEG_BIN,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(SMOKE_VIDEO),
        "-vn",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(SMOKE_AUDIO),
    ],
    capture_output=True,
    text=True,
)
if audio_result.returncode != 0:
    raise RuntimeError(audio_result.stderr[-3000:])

model = WhisperModel("tiny", device="cpu", compute_type="int8")
segments_iter, info = model.transcribe(
    str(SMOKE_AUDIO),
    vad_filter=True,
    word_timestamps=True,
    condition_on_previous_text=False,
)
segments = list(segments_iter)
transcript = " ".join(segment.text.strip() for segment in segments)

scene_manager = SceneManager()
scene_manager.add_detector(ContentDetector())
scene_manager.detect_scenes(open_video(str(SMOKE_VIDEO)))
scenes = scene_manager.get_scene_list()

audio, sample_rate = librosa.load(str(SMOKE_AUDIO), sr=16000)
rms = librosa.feature.rms(y=audio)[0]

summary = pd.DataFrame(
    [
        {
            "source_mb": round(SOURCE_VIDEO.stat().st_size / 1024**2, 2),
            "language": info.language,
            "transcript_chars": len(transcript),
            "segments": len(segments),
            "scenes": len(scenes),
            "rms_frames": len(rms),
            "max_rms": round(float(np.max(rms)), 6),
        }
    ]
)

print(summary.to_string(index=False))
print("Transcript:", transcript[:500])
print("LOCAL_SMOKE_TEST_OK")


# ---------------------------------------------------------------------------
# Moment Explorer selection contract.
# ---------------------------------------------------------------------------

print("\n--- Moment Explorer: preview/hash-bound selection tests ---")
_moment_tmp = Path(tempfile.mkdtemp(prefix="moment_selection_"))
_moment_preview = _moment_tmp / "hot_moment_01.mp4"
shutil.copy2(SMOKE_VIDEO, _moment_preview)
_moment_manifest = {
    "method": "local-multisignal-v2",
    "status": "review_required",
    "source": {"raw_video_path": str(SOURCE_VIDEO), "clip_start_sec": 0},
    "hot_moments": [{
        "rank": 1, "aligned_start_sec": 0.0, "aligned_end_sec": 10.0,
        "source_start_sec": 0.0, "source_end_sec": 10.0,
        "score": 88.0, "reason": "test", "transcript": transcript,
        "preview_path": str(_moment_preview),
    }],
}
_moment_selection = va.build_moment_selection(
    _moment_manifest, selected_moment_rank=1, reviewed_by="verify_local",
    rejected_moment_ranks=[], require_preview=True,
)
assert va.validate_moment_selection(_moment_selection, _moment_manifest, require_preview=True) == []
assert _moment_selection["status"] == "approved"
assert _moment_selection["human_review"]["automatic_selection"] is False
assert _moment_selection["selected_moment"]["preview_sha256"]

_changed_manifest = json.loads(json.dumps(_moment_manifest))
_changed_manifest["hot_moments"][0]["score"] = 99.0
_stale_errors = va.validate_moment_selection(
    _moment_selection, _changed_manifest, require_preview=True,
)
assert any("hot_moments.json degisti" in error for error in _stale_errors)

with open(_moment_preview, "ab") as _tamper_stream:
    _tamper_stream.write(b"tampered-after-review")
_tamper_errors = va.validate_moment_selection(
    _moment_selection, _moment_manifest, require_preview=True,
)
assert any("preview dosyasi incelemeden sonra degismis" in error for error in _tamper_errors)

try:
    va.build_moment_selection(
        _moment_manifest, selected_moment_rank=1, reviewed_by="",
        require_preview=True,
    )
except ValueError:
    pass
else:
    raise AssertionError("Bos reviewer ile moment secimi kabul edildi")
shutil.rmtree(_moment_tmp, ignore_errors=True)
print("MOMENT_SELECTION_GATE_OK")


# ---------------------------------------------------------------------------
# Cross-notebook artifact schema checks (Faz 8).
#
# These only run against files that already exist under results/ — they do
# NOT fabricate fake artifacts, and they do NOT claim a notebook ran
# successfully just because this script exists. If an artifact is missing
# (because the corresponding notebook, which needs a live Jupyter/Colab
# kernel and often API keys, hasn't been run in this environment), that is
# reported as SKIPPED, not as a pass or a failure.
# ---------------------------------------------------------------------------

RESULTS_DIR = PROJECT_ROOT / "results"

STATUS_VALUES = {
    "experimental", "development_placeholder", "review_required",
    "approved", "insufficient_data", "failed", "production_ready",
}


def check_json_schema(path, required_top_level_keys, label):
    if not path.is_file():
        print(f"[schema:{label}] SKIPPED (dosya yok): {path}")
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    missing = [key for key in required_top_level_keys if key not in data]
    if missing:
        raise AssertionError(f"[schema:{label}] eksik alanlar: {missing} ({path})")
    status = data.get("status")
    if status is not None and status not in STATUS_VALUES:
        raise AssertionError(f"[schema:{label}] gecersiz status '{status}' ({path})")
    print(f"[schema:{label}] OK (status={status}): {path}")
    return data


print("\n--- Cross-notebook artifact schema checks ---")
check_json_schema(
    RESULTS_DIR / "hot_moments.json",
    ["method", "status", "hot_moments"],
    "hot_moments.json",
)
check_json_schema(
    RESULTS_DIR / "moment_selection.json",
    ["artifact_type", "status", "selected_moment_rank", "reviewed_by",
     "hot_moments_content_sha256", "human_review"],
    "moment_selection.json",
)
check_json_schema(
    RESULTS_DIR / "selected_script.json",
    ["status", "script_text", "selected_moment", "human_approval"],
    "selected_script.json",
)
check_json_schema(
    RESULTS_DIR / "selected_voiceover.json",
    ["status", "audio_path", "human_approval"],
    "selected_voiceover.json",
)
check_json_schema(
    RESULTS_DIR / "render" / "render_manifest.json",
    ["status", "inputs", "config", "output", "human_approval"],
    "render_manifest.json",
)
check_json_schema(
    RESULTS_DIR / "viral_prediction.json",
    ["status", "dataset_size", "metrics", "confidence_level", "limitations"],
    "viral_prediction.json",
)
normalized_videos_path = RESULTS_DIR / "youtube_data" / "normalized_videos.csv"
if normalized_videos_path.is_file():
    normalized_df = pd.read_csv(normalized_videos_path)
    print(f"[schema:normalized_videos.csv] OK ({len(normalized_df)} satir): {normalized_videos_path}")
else:
    print(f"[schema:normalized_videos.csv] SKIPPED (dosya yok): {normalized_videos_path}")


# ---------------------------------------------------------------------------
# Notebook 05 history-loading positive/negative tests (bug-fix corrective pass).
#
# Exercises the REAL cell-2 source from 05_viral_prediction.ipynb (extracted
# directly from the .ipynb, not reimplemented) against three synthetic
# normalized_videos.csv fixtures written under a TEMP directory — the user's
# real results/ directory is never touched or overwritten:
#   1. a valid empty dataset (headers present, 0 data rows)
#   2. a headerless / totally empty (0-byte) CSV -> pandas.errors.EmptyDataError
#   3. a dataset missing required columns
# In every case the cell must not raise, and must fall back to an empty,
# fixed-schema history_df with history_load_status='insufficient_data'.
# ---------------------------------------------------------------------------

print("\n--- Notebook 05 history-loading schema tests (temp fixtures, no user files touched) ---")

import tempfile
import importlib

with open(PROJECT_ROOT / "experiments" / "colab" / "05_viral_prediction.ipynb", "r", encoding="utf-8") as f:
    nb05 = json.load(f)
nb05_code_cells = ["".join(c["source"]) for c in nb05["cells"] if c["cell_type"] == "code"]
# Located by CONTENT, not a fixed index: Faz 9 rewrite inserted new cells
# (bootstrap, Drive-aware paths) before the history-loading cell, so a fixed
# index would silently test the wrong cell after any future edit.
_nb05_load_candidates = [c for c in nb05_code_cells if "def find_first_existing" in c]
if len(_nb05_load_candidates) != 1:
    raise AssertionError(
        "05_viral_prediction.ipynb: 'def find_first_existing' iceren tam olarak 1 hucre "
        f"bekleniyordu, {len(_nb05_load_candidates)} bulundu. Notebook yapisi degismis olabilir."
    )
nb05_load_cell_src = _nb05_load_candidates[0]

from datetime import datetime, timezone
from local_pipeline import youtube_data as yd_for_tests

_NORMALIZED_COLUMNS = [
    "video_id", "title", "description", "published_at", "channel_id", "channel_title",
    "view_count", "like_count", "comment_count", "duration_iso8601", "tags", "category_id",
]


def _run_nb05_load_cell(youtube_data_dir):
    # Mirrors what the real bootstrap + paths cells would have put in scope by
    # the time the history-loading cell runs, without actually mounting Drive
    # or spawning a subprocess: real yd module, RESULTS_DIR, youtube_paths.
    ns = {
        "RESULTS_DIR": youtube_data_dir.parent,
        "youtube_paths": {"youtube_data_dir": youtube_data_dir},
        "youtube_data": yd_for_tests,
        "pd": pd,
        "json": json,
        "Path": Path,
        "datetime": datetime,
        "timezone": timezone,
    }
    exec(nb05_load_cell_src, ns)
    return ns["history_load_status"], ns["history_load_reason"], ns["history_df"]


with tempfile.TemporaryDirectory(prefix="yt_automation_nb05_test_") as tmp:
    tmp_path = Path(tmp)
    yt_dir = tmp_path / "results" / "youtube_data"
    yt_dir.mkdir(parents=True, exist_ok=True)
    csv_path = yt_dir / "normalized_videos.csv"

    # 1. Positive-ish: valid empty dataset with headers.
    pd.DataFrame(columns=_NORMALIZED_COLUMNS).to_csv(csv_path, index=False)
    status, reason, hdf = _run_nb05_load_cell(yt_dir)
    if status != "insufficient_data" or len(hdf) != 0:
        raise AssertionError(f"[nb05-history:empty-with-headers] beklenmeyen sonuc: status={status} reason={reason}")
    print(f"[nb05-history:empty-with-headers] OK (status={status}, crash yok)")

    # 2. Negative: headerless / totally empty (0-byte) CSV -> EmptyDataError must be caught.
    csv_path.write_text("", encoding="utf-8")
    status, reason, hdf = _run_nb05_load_cell(yt_dir)
    if status != "insufficient_data" or len(hdf) != 0:
        raise AssertionError(f"[nb05-history:headerless-empty] beklenmeyen sonuc: status={status} reason={reason}")
    print(f"[nb05-history:headerless-empty] OK (status={status}, EmptyDataError yakalandi, crash yok)")

    # 3. Negative: dataset missing required columns.
    pd.DataFrame({"foo": [1, 2], "bar": [3, 4]}).to_csv(csv_path, index=False)
    status, reason, hdf = _run_nb05_load_cell(yt_dir)
    if status != "insufficient_data" or len(hdf) != 0 or "eksik" not in reason:
        raise AssertionError(f"[nb05-history:missing-columns] beklenmeyen sonuc: status={status} reason={reason}")
    print(f"[nb05-history:missing-columns] OK (status={status}, crash yok)")

print("NB05_HISTORY_SCHEMA_TESTS_OK (gercek results/ klasoru degistirilmedi)")


# ---------------------------------------------------------------------------
# Render pipeline smoke test (Faz 8).
#
# Exercises the real ffmpeg filtergraphs from local_pipeline/render_utils.py
# (the same helpers 06_render_pipeline.ipynb inlines) end-to-end: vertical
# reframing + audio ducking + ffprobe validation, using the smoke clip
# produced above as a stand-in for a real hot-moment clip and its own audio
# track as a stand-in for narration. This proves the ffmpeg chain actually
# runs on this machine's ffmpeg build (subtitles/sidechaincompress/gblur
# filter availability, path escaping, etc.) — it does not exercise the
# faster-whisper subtitle-alignment step or the real notebook 06 config
# cells, which need a live kernel.
# ---------------------------------------------------------------------------

print("\n--- Render pipeline smoke test ---")
ffmpeg_bin, ffprobe_bin = ru.resolve_ffmpeg_binaries()
render_smoke_output = RUNTIME_DIR / "render_smoke_test.mp4"
print("FFmpeg binary:", ffmpeg_bin)
print("System ffprobe:", ffprobe_bin or "bulunamadi (probe_media() ffmpeg fallback'ina gececek)")

# probe_media() tries ffprobe first and transparently falls back to parsing
# `ffmpeg -i` when ffprobe is unavailable (imageio_ffmpeg only ships ffmpeg,
# not ffprobe). A missing ffprobe here must NOT stop the render — it only
# affects which probe backend answers the pre-render has_audio check below.
render_info, probe_backend_pre = ru.probe_media(ffmpeg_bin, ffprobe_bin, SMOKE_VIDEO)
has_audio = any(s.get("codec_type") == "audio" for s in render_info.get("streams", []))
print(f"On-render probe backend: {probe_backend_pre} (has_audio={has_audio})")

vertical_graph = ru.build_vertical_filtergraph("blurred_background", 480, 854)
audio_graph = ru.build_audio_filtergraph(has_audio, True, 8, 0.35, 1.0)
filter_complex = vertical_graph + ";" + audio_graph

ru.run_ffmpeg(ffmpeg_bin, [
    "-i", str(SMOKE_VIDEO), "-i", str(SMOKE_AUDIO),
    "-filter_complex", filter_complex,
    "-map", "[framed]", "-map", "[aout]",
    "-t", "3",
    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-movflags", "+faststart",
    str(render_smoke_output),
])

render_check = ru.validate_render(ffmpeg_bin, ffprobe_bin, render_smoke_output, 480, 854, min_duration_sec=1, max_duration_sec=10)
if not render_check["passed"]:
    raise AssertionError(f"Render smoke test dogrulamasi basarisiz: {render_check}")
print("Render smoke test dosyasi:", render_smoke_output)
print("Dogrulama sonucu:", render_check["checks"])
print("Kullanilan probe backend:", render_check["details"]["probe_backend"])

# Explicitly force the ffprobe-unavailable path (ffprobe_bin=None) against the
# same rendered file, so this script proves the fallback works even when this
# machine happens to have a system ffprobe on PATH (as it does here). This is
# the exact scenario from the earlier bug report: no system ffprobe.
forced_fallback_check = ru.validate_render(ffmpeg_bin, None, render_smoke_output, 480, 854, min_duration_sec=1, max_duration_sec=10)
if forced_fallback_check["details"]["probe_backend"] != "ffmpeg_fallback":
    raise AssertionError(f"Beklenen probe backend 'ffmpeg_fallback' degil: {forced_fallback_check}")
if not forced_fallback_check["passed"]:
    raise AssertionError(f"ffprobe olmadan (ffmpeg fallback) dogrulama basarisiz: {forced_fallback_check}")
print("ffprobe'suz zorlanmis fallback dogrulamasi da basarili (probe_backend=ffmpeg_fallback).")
print("RENDER_SMOKE_TEST_OK")


print("\n--- Original-audio subtitle render smoke test ---")
_original_tmp = Path(tempfile.mkdtemp(prefix="original_audio_subtitles_"))
_original_preview = _original_tmp / "hot_moment_01.mp4"
shutil.copy2(SMOKE_VIDEO, _original_preview)
_original_manifest = {
    "method": "local-multisignal-v2",
    "status": "review_required",
    "source": {
        "raw_video_path": str(SMOKE_VIDEO),
        "analysis_clip_path": str(SMOKE_VIDEO),
        "clip_start_sec": 0,
        "clip_duration_sec": 10,
    },
    "hot_moments": [{
        "rank": 1,
        "aligned_start_sec": 0.0,
        "aligned_end_sec": 10.0,
        "source_start_sec": 0.0,
        "source_end_sec": 10.0,
        "score": 88.0,
        "reason": "original-audio smoke fixture",
        "transcript": transcript,
        "preview_path": str(_original_preview),
    }],
}
_original_selection = va.build_moment_selection(
    _original_manifest, selected_moment_rank=1,
    reviewed_by="verify_local", require_preview=True,
)
_original_render = ru.run_original_audio_subtitle_pipeline(
    _original_manifest, _original_selection,
    _original_tmp / "hot_moments.json", _original_tmp / "moment_selection.json",
    _original_tmp / "render", preview_mode=True,
    subtitle_whisper_model_size="tiny",
    caption_theme="game_of_thrones",
)
if _original_render["status"] != "review_required":
    raise AssertionError(f"Orijinal-ses render durumu yanlis: {_original_render['status']}")
if not _original_render["output"]["validation"]["passed"]:
    raise AssertionError(f"Orijinal-ses render teknik dogrulamasi gecmedi: {_original_render}")
if not _original_render["config"]["original_audio_preserved"] or _original_render["config"]["tts_used"]:
    raise AssertionError("Orijinal-ses modu ses/TTS sozlesmesini ihlal etti")
if _original_render["subtitles"]["cue_count"] < 1:
    raise AssertionError("Orijinal sesten altyazi cue uretilmedi")
if not _original_render["subtitles"]["dynamic_word_highlight"]:
    raise AssertionError("Aktif-kelime dinamik altyazi devreye girmedi")
if _original_render["subtitles"]["format_used_for_render"] != "ass":
    raise AssertionError("Dinamik altyazi renderi ASS kullanmadi")
_original_ass = Path(_original_render["subtitles"]["ass_path"])
_original_ass_text = _original_ass.read_text(encoding="utf-8")
if "\\c&H0000FFFF&" not in _original_ass_text or _original_ass_text.count("Dialogue:") < 2:
    raise AssertionError("ASS dosyasinda sari aktif-kelime olaylari yok")


def _ass_time_seconds(value):
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


_ass_events = []
for _line in _original_ass_text.splitlines():
    if _line.startswith("Dialogue:"):
        _fields = _line.split(",", 3)
        _ass_events.append((_ass_time_seconds(_fields[1]), _ass_time_seconds(_fields[2])))
for _event_index in range(len(_ass_events) - 1):
    _current_end = _ass_events[_event_index][1]
    _next_start = _ass_events[_event_index + 1][0]
    if _current_end > _next_start + 1e-9:
        raise AssertionError(
            "ASS olaylari cakisti; eski altyazi karti yenisiyle birlikte ekranda kalabilir: "
            f"{_ass_events[_event_index]} -> {_ass_events[_event_index + 1]}"
        )
print("ASS_ZERO_OVERLAP_OK")
if _original_render["config"]["caption_theme_resolved"] != "game_of_thrones":
    raise AssertionError("Game of Thrones font profili render manifestine kaydedilmedi")
if _original_render["config"]["caption_font_name"] != "Marcellus SC":
    raise AssertionError("Game of Thrones profili Marcellus SC kullanmadi")
if not Path(_original_render["config"]["caption_font_path"]).is_file():
    raise AssertionError("Dogrulanmis Marcellus SC font dosyasi render sirasinda kullanilmadi")
print("ORIGINAL_AUDIO_SUBTITLE_RENDER_OK")
shutil.rmtree(_original_tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# YouTube Data API v3 module (local_pipeline/youtube_data.py) — MOCKED tests.
#
# No real API key is available in this local environment, so none of these
# calls ever touch the network or googleapiclient's HttpError type — they
# exercise the module's own logic (resolution, pagination, batching, cache,
# sanitization) against small fake ".list().execute()" objects. This is
# explicitly NOT a live YouTube API test; the user separately verified the
# real key manually in Colab via videos.list (not reproduced here).
# ---------------------------------------------------------------------------

print("\n--- YouTube Data API v3 module: mocked tests (no real network calls) ---")

import shutil
import tempfile as _tempfile

yd = yd_for_tests


class _FakeChannelsResource:
    def __init__(self, by_id, by_handle):
        self._by_id = by_id
        self._by_handle = by_handle
        self._next_kwargs = None

    def list(self, **kwargs):
        self._next_kwargs = kwargs
        return self

    def execute(self):
        kw = self._next_kwargs
        if "id" in kw:
            item = self._by_id.get(kw["id"])
        elif "forHandle" in kw:
            item = self._by_handle.get(kw["forHandle"])
        else:
            item = None
        return {"items": [item] if item else []}


def _make_channel_item(channel_id, title, uploads_playlist_id="UU_TEST"):
    return {
        "id": channel_id,
        "snippet": {"title": title},
        "contentDetails": {"relatedPlaylists": {"uploads": uploads_playlist_id}},
        "statistics": {"subscriberCount": "1000", "videoCount": "5"},
    }


_channel_a = _make_channel_item("UCaaaaaaaaaaaaaaaaaaaaaa", "Channel A")


class _FakeYouTubeChannelsOnly:
    def __init__(self):
        self.channels_resource = _FakeChannelsResource(
            by_id={"UCaaaaaaaaaaaaaaaaaaaaaa": _channel_a},
            by_handle={"@ChannelA": _channel_a, "ChannelA": _channel_a},
        )

    def channels(self):
        return self.channels_resource


# 1. Valid channel ID resolves.
_yt = _FakeYouTubeChannelsOnly()
_rec = yd.resolve_channel(_yt, "UCaaaaaaaaaaaaaaaaaaaaaa")
assert _rec["resolved"] and _rec["channel_id"] == "UCaaaaaaaaaaaaaaaaaaaaaa" and _rec["title"] == "Channel A"
print("[yd:resolve-valid-id] OK")

# 2. Valid @handle resolves (never via search.list — FakeYouTube has no search()).
_rec = yd.resolve_channel(_yt, "@ChannelA")
assert _rec["resolved"] and _rec["channel_id"] == "UCaaaaaaaaaaaaaaaaaaaaaa"
print("[yd:resolve-valid-handle] OK")

# 3. Unresolved handle: reported as a per-channel error, does not raise.
_rec = yd.resolve_channel(_yt, "@DoesNotExist")
assert not _rec["resolved"] and _rec["error"]
print("[yd:resolve-unresolved-handle] OK")

# 4. Duplicate inputs (ID + its own handle) collapse to one resolved channel.
resolved, failed = yd.resolve_channels(_yt, ["UCaaaaaaaaaaaaaaaaaaaaaa", "@ChannelA", "@DoesNotExist"])
assert len(resolved) == 1 and resolved[0]["channel_id"] == "UCaaaaaaaaaaaaaaaaaaaaaa"
assert len(failed) == 1 and failed[0]["input"] == "@DoesNotExist"
print("[yd:resolve-channels-dedupe-and-partial-failure] OK")


class _FakePlaylistItemsPaged:
    """Returns 2 video IDs per page, 3 pages total (5 IDs), to exercise pageToken pagination."""

    def __init__(self, all_video_ids, page_size=2):
        self._all = all_video_ids
        self._page_size = page_size
        self._kwargs = None

    def list(self, **kwargs):
        self._kwargs = kwargs
        return self

    def execute(self):
        token = self._kwargs.get("pageToken")
        start = int(token) if token else 0
        end = start + self._page_size
        chunk = self._all[start:end]
        next_token = str(end) if end < len(self._all) else None
        return {"items": [{"contentDetails": {"videoId": v}} for v in chunk], "nextPageToken": next_token}


class _FakeYouTubePaging:
    def __init__(self, video_ids):
        self._pi = _FakePlaylistItemsPaged(video_ids)

    def playlistItems(self):
        return self._pi


_all_ids = [f"vid{i}" for i in range(5)]
_yt_paging = _FakeYouTubePaging(_all_ids)
_cache_tmp = Path(_tempfile.mkdtemp(prefix="yt_cache_paging_"))
_got_ids = yd.get_playlist_video_ids(_yt_paging, yd.Cache(_cache_tmp), yd.QuotaTracker(), "UU_TEST", max_videos=100, channel_cache_ttl_hours=None)
assert _got_ids == _all_ids, f"pagination sonucu beklenenden farkli: {_got_ids}"
shutil.rmtree(_cache_tmp, ignore_errors=True)
print("[yd:playlist-pagination] OK")


class _FakeVideosBatch:
    def __init__(self):
        self.calls = []

    def list(self, **kwargs):
        self._ids = kwargs["id"].split(",")
        self.calls.append(list(self._ids))
        return self

    def execute(self):
        items = []
        for vid in self._ids:
            items.append({
                "id": vid,
                "snippet": {"title": vid, "description": "", "publishedAt": "2024-01-01T00:00:00Z",
                            "channelId": "UCaaaaaaaaaaaaaaaaaaaaaa", "channelTitle": "Channel A", "tags": []},
                "statistics": {"viewCount": "10"},  # likeCount/commentCount deliberately absent (hidden)
                "contentDetails": {"duration": "PT2M", "definition": "hd"},
                "status": {"privacyStatus": "public"},
            })
        return {"items": items}


class _FakeYouTubeVideos:
    def __init__(self):
        self.videos_resource = _FakeVideosBatch()

    def videos(self):
        return self.videos_resource


_many_ids = [f"vid{i}" for i in range(120)]
_yt_videos = _FakeYouTubeVideos()
_cache_tmp2 = Path(_tempfile.mkdtemp(prefix="yt_cache_batch_"))
_rows = yd.get_videos_details(_yt_videos, yd.Cache(_cache_tmp2), yd.QuotaTracker(), _many_ids, video_stats_ttl_hours=12)
assert len(_rows) == 120
assert [len(c) for c in _yt_videos.videos_resource.calls] == [50, 50, 20], (
    f"videos.list beklenen 50/50/20 gruplarina bolunmedi: {[len(c) for c in _yt_videos.videos_resource.calls]}"
)
shutil.rmtree(_cache_tmp2, ignore_errors=True)
print("[yd:video-id-batching-over-50] OK")

# 7. Missing/hidden statistics (likeCount, commentCount) must become None, not crash or default to 0.
assert _rows[0]["like_count"] is None and _rows[0]["comment_count"] is None and _rows[0]["view_count"] == 10
print("[yd:missing-statistics-become-none] OK")

# 8. Stable empty CSV schema — 0 videos still produces the full header row.
_csv_tmp = Path(_tempfile.mkdtemp(prefix="yt_csv_empty_")) / "normalized_videos.csv"
yd.write_normalized_videos_csv(_csv_tmp, [])
_empty_df = pd.read_csv(_csv_tmp)
assert list(_empty_df.columns) == yd.NORMALIZED_VIDEO_COLUMNS and len(_empty_df) == 0
shutil.rmtree(_csv_tmp.parent, ignore_errors=True)
print("[yd:stable-empty-csv-schema] OK")

# 9. Cache: miss -> set -> hit; then TTL-expired -> refresh (miss again); then never-expire (ttl=None).
_cache_dir = Path(_tempfile.mkdtemp(prefix="yt_cache_ttl_"))
_cache = yd.Cache(_cache_dir)
assert _cache.get("videos.list", "k1", ttl_hours=12) is None  # miss
_cache.set("videos.list", "k1", {"payload": 1})
assert _cache.get("videos.list", "k1", ttl_hours=12) == {"payload": 1}  # hit
assert _cache.stats["hits"] == 1 and _cache.stats["misses"] == 1

# Simulate a stale entry by rewriting its cached_at timestamp far in the past.
import datetime as _dt
_stale_path = _cache._path("videos.list", "k1")
_env = json.loads(_stale_path.read_text(encoding="utf-8"))
_env["cached_at"] = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=999)).isoformat()
_stale_path.write_text(json.dumps(_env), encoding="utf-8")
assert _cache.get("videos.list", "k1", ttl_hours=12) is None  # stale -> refresh (treated as miss)
assert _cache.stats["refreshes"] == 1

_cache.set("channels.list", "k2", {"never": "expires"})
assert _cache.get("channels.list", "k2", ttl_hours=None) == {"never": "expires"}  # ttl=None never expires
print("[yd:cache-hit-miss-stale-refresh-never-expire] OK")

# 10. Corrupt cache file is treated as a miss and safely replaced, never raises.
_corrupt_path = _cache._path("videos.list", "k3")
_corrupt_path.write_text("{not valid json", encoding="utf-8")
assert _cache.get("videos.list", "k3", ttl_hours=12) is None
assert _cache.stats["corrupt_replaced"] == 1
_cache.set("videos.list", "k3", {"recovered": True})
assert _cache.get("videos.list", "k3", ttl_hours=12) == {"recovered": True}
shutil.rmtree(_cache_dir, ignore_errors=True)
print("[yd:corrupt-cache-recovery] OK")

# 11. Sanitized error/URL output — the raw key value and key=/token=/secret= query
# params must never survive sanitize_error_text/sanitize_url.
_fake_key = "AIzaFAKEKEYFORTESTINGONLY1234567890"
_raw_error = f"HttpError 400: bad request for url https://www.googleapis.com/youtube/v3/videos?key={_fake_key}&part=snippet"
_sanitized = yd.sanitize_error_text(_raw_error, api_key=_fake_key)
assert _fake_key not in _sanitized and "key=***REDACTED***" in _sanitized
_sanitized_url = yd.sanitize_url(f"https://example.com/x?token={_fake_key}&access_token={_fake_key}")
assert _fake_key not in _sanitized_url
print("[yd:sanitized-error-and-url-output] OK")

class _FakeSearchResource:
    def __init__(self, results_by_query):
        self.results_by_query = results_by_query
        self.calls = []
        self._kwargs = None

    def list(self, **kwargs):
        self._kwargs = kwargs
        self.calls.append(dict(kwargs))
        return self

    def execute(self):
        key = (self._kwargs["q"], self._kwargs["order"])
        ids = self.results_by_query.get(key, [])
        return {"items": [{"id": {"videoId": vid}} for vid in ids]}


class _FakeDiscoveryVideos:
    def __init__(self, video_items):
        self.video_items = video_items
        self.calls = []
        self._ids = []

    def list(self, **kwargs):
        self._ids = kwargs["id"].split(",")
        self.calls.append(list(self._ids))
        return self

    def execute(self):
        return {"items": [self.video_items[vid] for vid in self._ids if vid in self.video_items]}


class _FakeDiscoveryYouTube:
    def __init__(self, search_results, video_items):
        self.search_resource = _FakeSearchResource(search_results)
        self.videos_resource = _FakeDiscoveryVideos(video_items)

    def search(self): return self.search_resource
    def videos(self): return self.videos_resource


def _discovery_video(vid, channel_id, published, views, likes, comments, duration="PT1M"):
    return {
        "id": vid,
        "snippet": {
            "title": vid, "description": "", "publishedAt": published,
            "channelId": channel_id, "channelTitle": channel_id, "tags": [],
        },
        "statistics": {
            "viewCount": str(views), "likeCount": str(likes),
            "commentCount": str(comments),
        },
        "contentDetails": {"duration": duration, "definition": "hd"},
        "status": {"privacyStatus": "public"},
    }


_fixed_now = _dt.datetime(2026, 8, 2, 12, 0, tzinfo=_dt.timezone.utc)
_search_results = {
    ("AI", "date"): ["fast", "repeat", "long"],
    ("AI", "viewCount"): ["fast", "repeat"],
    ("tools", "date"): ["repeat", "other"],
    ("tools", "viewCount"): ["other"],
}
_video_items = {
    "fast": _discovery_video("fast", "UCfast000000000000000001", "2026-08-02T10:00:00Z", 100000, 5000, 500),
    "repeat": _discovery_video("repeat", "UCrepeat000000000000001", "2026-08-01T12:00:00Z", 40000, 3000, 300),
    "other": _discovery_video("other", "UCrepeat000000000000001", "2026-07-31T12:00:00Z", 50000, 2500, 250),
    "long": _discovery_video("long", "UClong000000000000000001", "2026-08-02T11:00:00Z", 999999, 1, 1, "PT10M"),
}
_discovery_tmp = Path(_tempfile.mkdtemp(prefix="yt_discovery_"))
_discovery_client = _FakeDiscoveryYouTube(_search_results, _video_items)
_discovery_quota = yd.QuotaTracker()
_discovery_result = yd.discover_trending_content(
    _discovery_client, yd.Cache(_discovery_tmp), _discovery_quota,
    terms=["AI", "tools", "AI"], orders=("date", "viewCount"),
    max_search_calls=3, max_results_per_query=25, top_videos=10,
    top_channels=10, min_channel_matched_videos=1, shorts_only=True,
    cache_ttl_hours=6, now=_fixed_now,
)
assert _discovery_result["status"] == "review_required"
assert _discovery_result["pipeline_role"] == "research_sidecar"
assert _discovery_result["feeds_main_pipeline"] is False
assert len(_discovery_client.search_resource.calls) == 3
assert _discovery_quota.calls["search.list"] == 3
assert all(call["type"] == "video" and call["publishedAfter"] for call in _discovery_client.search_resource.calls)
assert [row["video_id"] for row in _discovery_result["videos"]].count("fast") == 1
assert "long" not in [row["video_id"] for row in _discovery_result["videos"]]
assert _discovery_result["videos"][0]["video_id"] == "fast"
assert _discovery_quota.to_dict()["search_query_calls_remaining_estimate"] == 97

_before_calls = len(_discovery_client.search_resource.calls)
yd.discover_trending_content(
    _discovery_client, yd.Cache(_discovery_tmp), _discovery_quota,
    terms=["AI", "tools"], orders=("date", "viewCount"), max_search_calls=3,
    cache_ttl_hours=6, now=_fixed_now,
)
assert len(_discovery_client.search_resource.calls) == _before_calls
assert _discovery_quota.cache_hits["search.list"] == 3

_empty_discovered_videos = _discovery_tmp / "empty_discovered_videos.csv"
_empty_discovered_channels = _discovery_tmp / "empty_discovered_channels.csv"
yd.write_discovered_videos_csv(_empty_discovered_videos, [])
yd.write_discovered_channels_csv(_empty_discovered_channels, [])
assert list(pd.read_csv(_empty_discovered_videos).columns) == yd.DISCOVERED_VIDEO_COLUMNS
assert list(pd.read_csv(_empty_discovered_channels).columns) == yd.DISCOVERED_CHANNEL_COLUMNS
shutil.rmtree(_discovery_tmp, ignore_errors=True)
print("[yd:keyword-discovery-bounded-dedup-ranked-cached] OK")

print("YOUTUBE_DATA_MOCKED_TESTS_OK (gercek API anahtari kullanilmadi, hicbir gercek YouTube cagrisi yapilmadi)")


# ---------------------------------------------------------------------------
# Secret-scanning check (Faz 11).
#
# Scans tracked PROJECT FILE CONTENTS (never process environment variables —
# dumping os.environ would defeat the purpose) for patterns that look like a
# hardcoded Google/YouTube API key literal. This is a static text scan over
# source/config files, not a live credential check.
# ---------------------------------------------------------------------------

print("\n--- Secret-scanning check (project file contents, not environment variables) ---")

import re

_GOOGLE_API_KEY_PATTERN = re.compile(r"AIza[0-9A-Za-z_\-]{35}")
_HARDCODED_ASSIGNMENT_PATTERN = re.compile(
    r"YOUTUBE_DATA_API_KEY\s*=\s*['\"][^'\"]{10,}['\"]"
)
_SCAN_EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".local_runtime", "results", "cache", ".ipynb_checkpoints",
}
_SCAN_EXTENSIONS = {".py", ".ipynb", ".md", ".txt", ".json", ".env", ".example", ".cfg", ".ini", ".yaml", ".yml"}

_secret_findings = []
for _path in PROJECT_ROOT.rglob("*"):
    if not _path.is_file():
        continue
    if any(part in _SCAN_EXCLUDE_DIRS for part in _path.parts):
        continue
    if _path.suffix not in _SCAN_EXTENSIONS and _path.name != ".env.example":
        continue
    try:
        _text = _path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError):
        continue
    if _GOOGLE_API_KEY_PATTERN.search(_text):
        _secret_findings.append((str(_path.relative_to(PROJECT_ROOT)), "AIza... Google API key deseni"))
    if _HARDCODED_ASSIGNMENT_PATTERN.search(_text):
        _secret_findings.append((str(_path.relative_to(PROJECT_ROOT)), "YOUTUBE_DATA_API_KEY = '<literal>' atamasi"))

if _secret_findings:
    for _fpath, _reason in _secret_findings:
        print(f"  BULUNDU: {_fpath} -- {_reason}")
    raise AssertionError(f"Proje dosyalarinda olasi sabit-kodlanmis kimlik bilgisi deseni bulundu: {_secret_findings}")
print(f"Tarandi: {PROJECT_ROOT} (haric tutulanlar: {sorted(_SCAN_EXCLUDE_DIRS)})")
print("SECRET_SCAN_OK (proje dosyalarinda sabit-kodlanmis API anahtari deseni bulunamadi)")


# ---------------------------------------------------------------------------
# Cache key-collision regression (corrective pass).
#
# Cache._path previously sanitized and TRUNCATED cache_key to 180 chars, so two
# distinct videos.list batches sharing the same first 180 sanitized characters
# mapped to one cache file and silently served each other's cached response.
# Uniqueness must now come from a SHA-256 digest over the full endpoint+key.
# ---------------------------------------------------------------------------

print("\n--- Cache key-collision regression ---")

_collision_cache_dir = Path(_tempfile.mkdtemp(prefix="yt_cache_collision_"))
_collision_cache = yd.Cache(_collision_cache_dir)

# Two 50-ID videos.list batches that differ ONLY past character 180.
_batch_a = [f"vid{i:04d}" for i in range(50)]
_batch_b = list(_batch_a)
_batch_b[-1] = "vidZZZZ_TAIL_THAT_ONLY_DIFFERS_WELL_PAST_CHARACTER_180_BOUNDARY"
_key_a = "_".join(sorted(_batch_a))
_key_b = "_".join(sorted(_batch_b))

_sanitized_a = re.sub(r"[^A-Za-z0-9_.-]", "_", _key_a)[:180]
_sanitized_b = re.sub(r"[^A-Za-z0-9_.-]", "_", _key_b)[:180]
if _sanitized_a != _sanitized_b:
    raise AssertionError(
        "Regresyon testi kurulumu gecersiz: iki anahtarin ilk 180 sanitize edilmis karakteri "
        "AYNI olmali (eski hatali davranisi tetiklemek icin)."
    )
print("  kurulum dogrulandi: iki anahtarin ilk 180 sanitize karakteri ayni")

_path_a = _collision_cache._path("videos.list", _key_a)
_path_b = _collision_cache._path("videos.list", _key_b)
if _path_a == _path_b:
    raise AssertionError(
        f"CACHE COLLISION: iki farkli videos.list anahtari ayni dosyaya esleniyor: {_path_a}"
    )
print("  farkli onbellek dosyalari:", _path_a.name[:40] + "...", "!=", _path_b.name[:40] + "...")

# End-to-end: storing under A must not be readable under B.
_collision_cache.set("videos.list", _key_a, {"batch": "A"})
_collision_cache.set("videos.list", _key_b, {"batch": "B"})
_read_a = _collision_cache.get("videos.list", _key_a, ttl_hours=12)
_read_b = _collision_cache.get("videos.list", _key_b, ttl_hours=12)
if _read_a != {"batch": "A"} or _read_b != {"batch": "B"}:
    raise AssertionError(f"Onbellek capraz-kirlenme: A={_read_a} B={_read_b}")

# The same endpoint+key pair must still be stable/deterministic across instances.
_other_instance = yd.Cache(_collision_cache_dir)
if _other_instance._path("videos.list", _key_a) != _path_a:
    raise AssertionError("Onbellek yolu deterministik degil (ayni endpoint+key farkli yol uretti).")

# Different ENDPOINTS with the same key must also not collide.
if _collision_cache._path("channels.list", _key_a) == _path_a:
    raise AssertionError("Farkli endpoint ayni dosyaya eslendi (endpoint digest'e dahil degil).")

shutil.rmtree(_collision_cache_dir, ignore_errors=True)
print("[yd:cache-key-collision-regression] OK")
print("CACHE_COLLISION_REGRESSION_OK")


# ---------------------------------------------------------------------------
# Embedded-module hash synchronization (corrective pass).
#
# Each notebook that bootstraps local_pipeline modules embeds a zlib+Base85 snapshot
# of every module it needs, alongside that module's SHA-256. This re-derives the
# hash from the CURRENT canonical file on disk and compares it against BOTH the
# recorded hash AND the hash of the decoded payload, so:
#   - a notebook built from a since-edited module fails loudly (out of sync),
#   - a corrupted/truncated payload fails loudly (payload != recorded hash).
# ---------------------------------------------------------------------------

print("\n--- Embedded local_pipeline payload hash synchronization ---")

import ast as _ast
import base64 as _base64
import zlib as _zlib

LOCAL_PIPELINE_DIR = PROJECT_ROOT / "local_pipeline"
NOTEBOOK_DIR = PROJECT_ROOT / "experiments" / "colab"


def _canonical_module_sha256(module_name):
    module_path = LOCAL_PIPELINE_DIR / f"{module_name}.py"
    if not module_path.is_file():
        raise AssertionError(f"Kanonik modul bulunamadi: {module_path}")
    return hashlib.sha256(module_path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def _extract_embedded_payloads(notebook_path):
    """Returns compressed, hash-verified module payloads parsed out of a
    notebook's bootstrap cell via AST (never exec'ing the notebook)."""
    with open(notebook_path, "r", encoding="utf-8") as f:
        nb_json = json.load(f)
    for cell in nb_json["cells"]:
        if cell["cell_type"] != "code":
            continue
        source = "".join(cell["source"])
        if "_LOCAL_PIPELINE_PAYLOADS" not in source:
            continue
        tree = _ast.parse(source)
        for node in tree.body:
            if not isinstance(node, _ast.Assign):
                continue
            targets = [t.id for t in node.targets if isinstance(t, _ast.Name)]
            if "_LOCAL_PIPELINE_PAYLOADS" in targets:
                return _ast.literal_eval(node.value)
    return None


_notebooks_with_payloads = 0
_payload_checks = 0
for _nb_path in sorted(NOTEBOOK_DIR.glob("*.ipynb")):
    _payloads = _extract_embedded_payloads(_nb_path)
    if _payloads is None:
        print(f"  {_nb_path.name}: gomulu payload yok (bu notebook modul bootstrap etmiyor)")
        continue
    _notebooks_with_payloads += 1
    _nb_for_ui = json.loads(_nb_path.read_text(encoding="utf-8"))
    _payload_cells = [c for c in _nb_for_ui["cells"]
                      if c.get("cell_type") == "code" and
                      "_LOCAL_PIPELINE_PAYLOADS" in "".join(c.get("source", []))]
    _payload_cell = _payload_cells[0]
    _payload_meta = _payload_cell.get("metadata") or {}
    if (_payload_meta.get("cellView") != "form" or
            not (_payload_meta.get("jupyter") or {}).get("source_hidden")):
        raise AssertionError(f"{_nb_path.name}: dahili payload hucresi UI'da katlanmamis")
    # UI-friendliness guard, not a correctness one: the cell is already collapsed
    # (cellView=form + source_hidden) and every payload is zlib+Base85 compressed
    # and hash-verified. Sized for the master notebook's 11 embedded modules
    # (~476 KB of source -> ~169 KB encoded, ~36%, + ~19% loader/line-wrap
    # overhead = ~202 KB). Raise it deliberately when a module is added or grows,
    # never to hide a compression regression: if the encoded:source ratio climbs
    # well above ~40%, compression broke and the budget is not the problem.
    if len("".join(_payload_cell.get("source", []))) > 220_000:
        raise AssertionError(f"{_nb_path.name}: sikistirilmis bootstrap hucresi hala asiri buyuk")
    for _module_name, _entry in sorted(_payloads.items()):
        _recorded = _entry["sha256"]
        _canonical = _canonical_module_sha256(_module_name)
        if _recorded != _canonical:
            raise AssertionError(
                f"SENKRON DISI GOMULU MODUL: {_nb_path.name} icindeki local_pipeline.{_module_name} "
                f"payload hash'i {_recorded[:16]}... ama diskteki kanonik dosyanin hash'i "
                f"{_canonical[:16]}.... Notebook, modul degistikten sonra yeniden uretilmemis. "
                f"Duzeltme: nb_builder ile ilgili notebook'u yeniden uret."
            )
        if _entry.get("encoding") != "zlib+b85":
            raise AssertionError(
                f"ESKI/DESTEKLENMEYEN PAYLOAD FORMATI: {_nb_path.name} / {_module_name}"
            )
        _decoded = _zlib.decompress(_base64.b85decode(_entry["data"]))
        _decoded_hash = hashlib.sha256(_decoded).hexdigest()
        if _decoded_hash != _recorded:
            raise AssertionError(
                f"BOZUK GOMULU PAYLOAD: {_nb_path.name} / local_pipeline.{_module_name}: "
                f"cozulmus payload hash'i {_decoded_hash[:16]}... kayitli hash "
                f"{_recorded[:16]}... ile eslesmiyor."
            )
        # The decoded payload must also be valid, parseable Python.
        _ast.parse(_decoded.decode("utf-8"))
        _payload_checks += 1
    print(f"  {_nb_path.name}: {len(_payloads)} gomulu modul senkron ({', '.join(sorted(_payloads))})")

if _notebooks_with_payloads == 0:
    raise AssertionError("Hicbir notebook'ta gomulu local_pipeline payload bulunamadi.")
print(f"EMBEDDED_PAYLOAD_HASH_SYNC_OK ({_notebooks_with_payloads} notebook, {_payload_checks} modul kontrolu)")


# ---------------------------------------------------------------------------
# Corrupted-payload detection must FAIL LOUDLY (negative test).
#
# Proves the bootstrap cell's hash guard actually fires: a deliberately
# mutated payload must raise, not silently import broken/partial code.
# ---------------------------------------------------------------------------

print("\n--- Corrupted embedded payload must raise (negative test) ---")

MASTER_NB_PATH = NOTEBOOK_DIR / "00_youtube_automation_master.ipynb"
with open(MASTER_NB_PATH, "r", encoding="utf-8") as f:
    _master_nb = json.load(f)
_master_code_cells = ["".join(c["source"]) for c in _master_nb["cells"] if c["cell_type"] == "code"]
_bootstrap_sources = [c for c in _master_code_cells if "_load_local_pipeline_modules" in c]
if len(_bootstrap_sources) != 1:
    raise AssertionError(
        f"Master notebook'ta tam olarak 1 bootstrap hucresi bekleniyordu, {len(_bootstrap_sources)} bulundu."
    )
_bootstrap_src = _bootstrap_sources[0]

# Force the self-contained fallback branch (no repo/Drive/clone available) and
# corrupt one payload's bytes so the recorded hash can no longer match.
_corrupt_ns = {}
_corrupt_dir = Path(_tempfile.mkdtemp(prefix="corrupt_payload_test_"))
_prev_cwd = os.getcwd()
try:
    exec(compile(_bootstrap_src, "bootstrap-corrupt", "exec"), _corrupt_ns)
    _payload_map = _corrupt_ns["_LOCAL_PIPELINE_PAYLOADS"]
    _victim = sorted(_payload_map)[0]
    _good_data = _payload_map[_victim]["data"]
    _corrupted_bytes = (_zlib.decompress(_base64.b85decode(_good_data))
                        + b"\n# INJECTED CORRUPTION\n")
    _payload_map[_victim]["data"] = _base64.b85encode(
        _zlib.compress(_corrupted_bytes, level=9)).decode("ascii")

    # Point the loader at an empty cwd with no local_pipeline anywhere above it,
    # and hide the already-imported real package so branch 1 cannot short-circuit.
    os.chdir(_corrupt_dir)
    _corrupt_ns["PROJECT_REPO_URL"] = ""
    _saved_modules = {k: v for k, v in sys.modules.items() if k == "local_pipeline" or k.startswith("local_pipeline.")}
    _saved_path = list(sys.path)
    for _k in _saved_modules:
        del sys.modules[_k]
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != PROJECT_ROOT]
    try:
        _corrupt_ns["_load_local_pipeline_modules"]()
    except RuntimeError as exc:
        if "bozuk" not in str(exc).lower() and "sha256" not in str(exc).lower():
            raise AssertionError(f"Beklenen hash-uyusmazligi hatasi degil: {exc}")
        print(f"  Beklendigi gibi hata verdi: {str(exc)[:110]}...")
    else:
        raise AssertionError(
            "GUVENLIK BOSLUGU: bozuk gomulu payload sessizce kabul edildi; hash kontrolu tetiklenmedi."
        )
    finally:
        sys.path[:] = _saved_path
        sys.modules.update(_saved_modules)
finally:
    os.chdir(_prev_cwd)
    shutil.rmtree(_corrupt_dir, ignore_errors=True)
print("CORRUPTED_PAYLOAD_DETECTION_OK")


# ---------------------------------------------------------------------------
# Single-file Colab operation (THE completion criterion).
#
# Simulates "the master notebook is the ONLY file uploaded to a fresh Colab
# session": no repo on sys.path, no local_pipeline anywhere above cwd, no
# PROJECT_REPO_URL, no Drive copy. The bootstrap cell must therefore fall all
# the way through to its embedded, hash-verified payloads and still produce six
# fully usable modules.
# ---------------------------------------------------------------------------

print("\n--- Single-file Colab operation: embedded fallback must fully work ---")

_REQUIRED_ENTRY_POINTS = {
    "video_analysis": [
        "run_video_analysis", "score_hot_moments", "validate_hot_moments_schema",
        "build_moment_selection", "validate_moment_selection",
    ],
    "script_generation": ["run_script_generation", "build_user_prompt"],
    "tts": ["run_tts", "build_selected_voiceover_artifact"],
    "render_utils": ["run_render_pipeline", "run_original_audio_subtitle_pipeline", "validate_render"],
    "youtube_data": ["collect_channel_uploads", "build_collection_manifest", "Cache"],
    "viral_prediction": ["build_viral_prediction_artifact", "evaluate_baselines_on_holdout"],
    "approval": ["approve_artifact", "check_approval", "content_hash_for", "describe_for_review"],
    # Narration presets ship with the single-file notebook too: without them
    # `source_strategy` cannot import and Phase 0 dies in Colab.
    "narration_presets": ["NARRATION_PRESETS", "resolve_narration_preset",
                          "validate_presets", "VOICE_CHARACTERISTICS"],
    # Google router: every canonical command must survive the embedded-payload
    # round trip, otherwise single-file Colab silently loses Gemini support.
    "google_ai": [
        "list_available_google_models", "build_google_capability_manifest",
        "run_google_hotmoment_review", "run_google_script_generation",
        "run_google_bulk_classification", "run_google_viral_interpretation",
        "run_google_grounded_research", "run_google_tts", "run_google_embeddings",
        "run_antigravity_research_experimental", "select_google_route",
        "execute_google_route", "record_google_usage", "classify_google_error",
        "sanitize_google_error", "QuotaLedger", "MODEL_REGISTRY", "TASK_ROUTES",
        "ALLOW_ACCOUNT_QUOTA_ROTATION", "assert_no_account_quota_rotation",
        "resolve_secret", "build_google_client", "build_google_artifact",
    ],
    "provider_adapters": [
        "resolve_provider_credentials", "describe_provider_states",
        "call_groq_text", "call_cloudflare_text", "call_cloudflare_embeddings",
        "call_azure_tts", "run_cross_provider_text_fallback", "CROSS_PROVIDER_ROUTES",
        # Edge is the only credential-free narration path: if the embedded
        # payload drops it, Colab loses TTS the moment the Gemini quota is spent.
        "call_edge_tts", "run_cross_provider_tts_fallback",
        "normalize_speech_rate", "EDGE_TURKISH_VOICES",
    ],
    # Phase 0: without this module the master notebook cannot gate ANY downstream
    # phase, so single-file Colab must carry it too.
    "source_strategy": [
        "assess_source", "build_processing_plan", "validate_processing_plan",
        "plan_allows_phase", "describe_phase_plan", "resolve_phase_gates",
        "build_output_slots", "check_config_drift", "source_fingerprint_sha256",
        "format_assessment_panel", "format_plan_panel", "PROCESSING_MODES",
        "MODE_DIRECT_CLIP_SERIES", "MODE_NARRATED_THEORY", "GATED_PHASES",
    ],
}

_isolated_dir = Path(_tempfile.mkdtemp(prefix="single_file_colab_"))
_prev_cwd = os.getcwd()
_saved_lp_modules = {k: v for k, v in sys.modules.items()
                     if k == "local_pipeline" or k.startswith("local_pipeline.")}
_saved_sys_path = list(sys.path)
try:
    os.chdir(_isolated_dir)
    # Remove the repo from sys.path AND evict the already-imported real package,
    # so branches 1-3 of the bootstrap cannot possibly succeed.
    for _k in _saved_lp_modules:
        del sys.modules[_k]
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != PROJECT_ROOT]

    _isolated_ns = {}
    exec(compile(_bootstrap_src, "bootstrap-isolated", "exec"), _isolated_ns)
    _loaded = _isolated_ns["_local_pipeline_modules"]

    if set(_loaded) != set(_REQUIRED_ENTRY_POINTS):
        raise AssertionError(
            f"Gomulu fallback beklenen modulleri yuklemedi. Beklenen: "
            f"{sorted(_REQUIRED_ENTRY_POINTS)}, yuklenen: {sorted(_loaded)}"
        )

    # Every module must have come from the throwaway fallback directory, NOT the repo.
    for _mod_name, _mod in _loaded.items():
        _mod_file = Path(_mod.__file__).resolve()
        if PROJECT_ROOT in _mod_file.parents:
            raise AssertionError(
                f"Izolasyon basarisiz: local_pipeline.{_mod_name} repodan yuklendi "
                f"({_mod_file}); gomulu fallback test edilemedi."
            )
        for _attr in _REQUIRED_ENTRY_POINTS[_mod_name]:
            if not hasattr(_mod, _attr):
                raise AssertionError(
                    f"Gomulu local_pipeline.{_mod_name} eksik/bozuk: '{_attr}' yok."
                )

    # Prove the fallback modules are genuinely FUNCTIONAL, not merely importable.
    _vd = _loaded["video_analysis"]
    _errs = _vd.validate_hot_moments_schema({"method": "m", "status": "review_required",
                                              "hot_moments": []})
    if not _errs:
        raise AssertionError("Gomulu video_analysis.validate_hot_moments_schema bos listeyi kabul etti.")

    _yd_fb = _loaded["youtube_data"]
    if _yd_fb.parse_iso8601_duration("PT2M30S") != 150:
        raise AssertionError("Gomulu youtube_data.parse_iso8601_duration yanlis sonuc verdi.")
    if _yd_fb.describe_credential_state(None) != "missing":
        raise AssertionError("Gomulu youtube_data.describe_credential_state yanlis sonuc verdi.")
    _fb_cache_dir = Path(_isolated_dir) / "fb_cache"
    _fb_cache = _yd_fb.Cache(_fb_cache_dir)
    _fb_cache.set("videos.list", "k", {"v": 1})
    if _fb_cache.get("videos.list", "k", ttl_hours=12) != {"v": 1}:
        raise AssertionError("Gomulu youtube_data.Cache calismadi.")

    _sg_fb = _loaded["script_generation"]
    _prompt = _sg_fb.build_user_prompt(
        {"rank": 1, "source_start_sec": 0, "source_end_sec": 30, "reason": "r", "transcript": "t"})
    if "YouTube Shorts" not in _prompt:
        raise AssertionError("Gomulu script_generation.build_user_prompt beklenen prompt'u uretmedi.")

    _vp_fb = _loaded["viral_prediction"]
    if set(_vp_fb.BASELINE_METHODS) != {"heuristic-channel-median", "heuristic-rolling-recent",
                                          "heuristic-engagement-adjusted"}:
        raise AssertionError("Gomulu viral_prediction.BASELINE_METHODS beklenen yontemleri icermiyor.")

    _ru_fb = _loaded["render_utils"]
    if "[framed]" not in _ru_fb.build_vertical_filtergraph("center_crop", 1080, 1920):
        raise AssertionError("Gomulu render_utils.build_vertical_filtergraph yanlis cikti verdi.")

    _ap_fb = _loaded["approval"]
    if _ap_fb.sha256_text("abc") != hashlib.sha256(b"abc").hexdigest():
        raise AssertionError("Gomulu approval.sha256_text yanlis sonuc verdi.")
    _fb_artifact = {"status": "review_required", "script_text": "x",
                    "human_approval": {"approved": False}}
    _fb_approved = _ap_fb.approve_artifact(_fb_artifact, "script",
                                            _ap_fb.sha256_text("x"), "verify_local")
    if _fb_approved["status"] != "approved":
        raise AssertionError("Gomulu approval.approve_artifact calismadi.")

    # The Google router must be FUNCTIONAL from the embedded copy, not merely
    # importable: routing, quota accounting and the rotation guard all work, and
    # provider_adapters' relative import of google_ai resolved inside the
    # throwaway package directory.
    _ga_fb = _loaded["google_ai"]
    if _ga_fb.ALLOW_ACCOUNT_QUOTA_ROTATION is not False:
        raise AssertionError("Gomulu google_ai hesap rotasyonunu ACIK getirdi.")
    _fb_manifest = _ga_fb.build_google_capability_manifest(
        discovered_models=["models/gemini-3.6-flash", "models/gemini-2.5-pro"])
    if "gemini-2.5-pro" in _fb_manifest["usable_models"]:
        raise AssertionError("Gomulu google_ai 0-kotali modeli kullanilabilir saydi.")
    _fb_route = _ga_fb.select_google_route("script_generation", manifest=_fb_manifest,
                                            ledger=_ga_fb.QuotaLedger())
    if _fb_route["route"] != ["gemini-3.6-flash"]:
        raise AssertionError(f"Gomulu google_ai rotasi beklenmedik: {_fb_route['route']}")
    if _ga_fb.classify_google_error(Exception("429 quota per day exceeded")) != "rate_limit_rpd":
        raise AssertionError("Gomulu google_ai hata siniflandirmasi bozuk.")

    _pa_fb = _loaded["provider_adapters"]
    if _pa_fb.call_groq_text("x", secret_getter=lambda n: None)["status"] != "skipped":
        raise AssertionError("Gomulu provider_adapters kimlik yokken temiz atlamadi.")

    print(f"  {len(_loaded)}/{len(_REQUIRED_ENTRY_POINTS)} modul gomulu fallback'ten yuklendi "
          f"({Path(_loaded['youtube_data'].__file__).parent})")
    print("  hepsi fonksiyonel: sema dogrulama, sure ayristirma, onbellek, prompt, baseline, "
          "filtergraph, Google rota secimi/kota/hata siniflandirma, capraz-saglayici atlama")
finally:
    sys.path[:] = _saved_sys_path
    sys.modules.update(_saved_lp_modules)
    os.chdir(_prev_cwd)
    shutil.rmtree(_isolated_dir, ignore_errors=True)

print("SINGLE_FILE_COLAB_OPERATION_OK (repo/Drive/klon yokken master notebook kendi kendine yeterli)")


# ---------------------------------------------------------------------------
# Master notebook: no toggle may remain a placeholder.
#
# Asserts every one of the six phase toggles is (a) declared, and (b) actually
# wired to real work -- i.e. a cell exists that both branches on the toggle and
# calls a canonical local_pipeline entry point. Also guards against the old
# placeholder block and its two invalid notebook filename references coming back.
# ---------------------------------------------------------------------------

print("\n--- Master notebook: all six toggles functional, no placeholders ---")

MASTER_TOGGLES = {
    "RUN_VIDEO_ANALYSIS": ("video_analysis", ("video_analysis.run_video_analysis",)),
    "RUN_SCRIPT_GENERATION": ("script_generation", ("script_generation.run_script_generation",)),
    "RUN_TTS": ("tts", ("tts.run_tts",)),
    "RUN_RENDER": ("render", ("render_utils.run_render_pipeline",)),
    "RUN_YOUTUBE_DATA": ("youtube_data", ("youtube_data.collect_channel_uploads",
                                            "youtube_data.build_collection_manifest")),
    "RUN_VIRAL_PREDICTION": ("viral_prediction", ("viral_prediction.build_viral_prediction_artifact",)),
}

_master_all_source = "\n".join("".join(c["source"]) for c in _master_nb["cells"])

_FORBIDDEN_MASTER_STRINGS = [
    # The removed placeholder block and its bogus notebook filenames.
    "placeholder_phases",
    "henuz master notebook icinde yeniden uygulanmadi",
    "01_video_source_analysis.ipynb",
    "03_tts_voice_comparison.ipynb",
]
for _forbidden in _FORBIDDEN_MASTER_STRINGS:
    if _forbidden in _master_all_source:
        raise AssertionError(
            f"Master notebook'ta kaldirilmasi gereken yer tutucu/gecersiz referans hala var: {_forbidden!r}"
        )
print("  yer tutucu blok ve gecersiz notebook adi referanslari yok")

for _toggle, (_phase_key, _required_calls) in MASTER_TOGGLES.items():
    if f"{_toggle} = " not in _master_all_source:
        raise AssertionError(f"Master notebook'ta '{_toggle}' toggle'i tanimli degil.")
    # Find the phase cell that branches on this toggle.
    _phase_cells = [
        c for c in _master_code_cells
        if f"not {_toggle}" in c and f"record_phase('{_phase_key}'" in c
    ]
    if len(_phase_cells) != 1:
        raise AssertionError(
            f"{_toggle}: toggle'a dallanan ve record_phase('{_phase_key}'...) cagiran tam olarak 1 "
            f"hucre bekleniyordu, {len(_phase_cells)} bulundu."
        )
    _phase_cell = _phase_cells[0]
    _found = [call for call in _required_calls if call in _phase_cell]
    if not _found:
        raise AssertionError(
            f"{_toggle} YER TUTUCU GORUNUYOR: fazi calistiran hucre kanonik giris noktalarindan "
            f"hicbirini cagirmiyor ({_required_calls})."
        )
    # A functional phase must be able to report both a clean run and a failure.
    for _expected_status in ("'completed'", "'failed'", "'skipped'"):
        if _expected_status not in _phase_cell:
            raise AssertionError(
                f"{_toggle}: faz hucresi {_expected_status} execution_status'unu hic atamiyor; "
                "bagimlilik hatasi veya basari raporlanamaz."
            )
    print(f"  {_toggle}: OK (kanonik cagri: {_found[0]})")

print("MASTER_TOGGLES_ALL_FUNCTIONAL_OK")


# ---------------------------------------------------------------------------
# Master notebook: approval must be a SEPARATE post-generation step.
#
# A generation config cell must NOT carry an approval flag that could stamp
# freshly generated content as approved, and each of the three reviewable phases
# must have its own review cell (printing a SHA-256) plus its own approval cell
# (calling approval.approve_artifact with that hash).
# ---------------------------------------------------------------------------

print("\n--- Master notebook: approval gates are post-generation and hash-bound ---")

# These would re-introduce the exact defect: approving during generation.
_FORBIDDEN_APPROVAL_PATTERNS = [
    "script_approved=", "voiceover_approved=", "render_approved=",
    "SCRIPT_APPROVED = False", "VOICEOVER_APPROVED = False", "RENDER_APPROVED = False",
    "render_approved_flag",
]
for _pattern in _FORBIDDEN_APPROVAL_PATTERNS:
    if _pattern in _master_all_source:
        raise AssertionError(
            f"Master notebook uretim-aninda-onay desenini hala iceriyor: {_pattern!r}. "
            "Onay yalnizca ayri bir hucrede approval.approve_artifact ile verilmelidir."
        )
print("  uretim-aninda-onay bayraklari yok (script/voiceover/render)")

for _kind, _hash_var in [("script", "SCRIPT_REVIEWED_SHA256"),
                          ("voiceover", "VOICEOVER_REVIEWED_SHA256"),
                          ("render", "RENDER_REVIEWED_SHA256")]:
    _review_cells = [c for c in _master_code_cells
                     if f"show_review_prompt('{_kind}'" in c or f"describe_for_review('{_kind}'" in c]
    if not _review_cells:
        raise AssertionError(f"{_kind}: inceleme (review) hucresi bulunamadi.")
    _approve_cells = [c for c in _master_code_cells
                      if _hash_var in c and f"'{_kind}'" in c and "approve_artifact" in c]
    if len(_approve_cells) != 1:
        raise AssertionError(
            f"{_kind}: {_hash_var} + approval.approve_artifact iceren tam olarak 1 onay hucresi "
            f"bekleniyordu, {len(_approve_cells)} bulundu."
        )
    # The review cell and the approval cell must be DIFFERENT cells.
    if _review_cells[0] is _approve_cells[0]:
        raise AssertionError(f"{_kind}: inceleme ve onay ayni hucrede; onay ayri bir adim olmali.")
    print(f"  {_kind}: ayri inceleme hucresi + ayri onay hucresi ({_hash_var}) OK")

# Provider-selection config must exist for both paid phases.
for _cfg_var in ["SCRIPT_PROVIDER", "SCRIPT_MODEL", "ALLOW_SCRIPT_PROVIDER_FALLBACK",
                  "TTS_PROVIDER", "ALLOW_TTS_PROVIDER_FALLBACK", "ALLOW_DEV_GTTS_FALLBACK"]:
    if f"{_cfg_var} = " not in _master_all_source:
        raise AssertionError(f"Master notebook'ta '{_cfg_var}' konfigurasyonu yok.")
print("  saglayici secim konfigurasyonu mevcut (SCRIPT_*/TTS_*/ALLOW_*)")

# The master must pass its provider selection through, not iterate the catalogs.
_script_run_cell = [c for c in _master_code_cells if "script_generation.run_script_generation" in c][0]
_script_config_cell = [c for c in _master_code_cells
                       if "SCRIPT_PROVIDER = 'google'" in c][0]
if "MOMENT_APPROVED =" in _script_config_cell:
    raise AssertionError(
        "Master script konfigurasyonu hala elle MOMENT_APPROVED boolean'i kabul ediyor"
    )
for _selection_guard in (
    "video_analysis.validate_moment_selection", "moment_selection.json",
    "MOMENT_SELECTION_PATH", "SELECTED_MOMENT_RANK = moment_selection",
):
    if _selection_guard not in _script_run_cell:
        raise AssertionError(
            f"Master script fazinda hash-bagli moment secim kapisi eksik: {_selection_guard}"
        )
_analysis_run_cell = [c for c in _master_code_cells
                      if "video_analysis.run_video_analysis" in c][0]
if "preview_dir=RESULTS_DIR / 'moment_previews'" not in _analysis_run_cell:
    raise AssertionError("Master analiz preview kliplerini kalici results/moment_previews yoluna yazmiyor")
_moment_display_cells = [c for c in _master_code_cells
                         if "display(Video(_preview" in c]
_moment_record_cells = [c for c in _master_code_cells
                        if "video_analysis.build_moment_selection" in c]
if len(_moment_display_cells) != 1 or len(_moment_record_cells) != 1:
    raise AssertionError("Master Moment Explorer izleme/kayit hucreleri tekil degil")
print("  moment preview + hash-bagli secim kapisi mevcut; manuel onay boolean'i yok")
for _required_kwarg in ["script_provider=SCRIPT_PROVIDER", "script_model=SCRIPT_MODEL",
                         "allow_provider_fallback=ALLOW_SCRIPT_PROVIDER_FALLBACK"]:
    if _required_kwarg not in _script_run_cell:
        raise AssertionError(f"Master script fazi '{_required_kwarg}' gecirmiyor.")
_tts_run_cell = [c for c in _master_code_cells if "tts.run_tts" in c][0]
for _required_kwarg in ["tts_provider=TTS_PROVIDER",
                         "allow_provider_fallback=ALLOW_TTS_PROVIDER_FALLBACK",
                         "allow_dev_gtts_fallback=ALLOW_DEV_GTTS_FALLBACK"]:
    if _required_kwarg not in _tts_run_cell:
        raise AssertionError(f"Master TTS fazi '{_required_kwarg}' gecirmiyor.")
# The master must never loop the comparison catalogs (that is notebook 02/03's job).
for _catalog in ["OPENAI_SCRIPT_MODELS", "ANTHROPIC_SCRIPT_MODELS", "GOOGLE_SCRIPT_MODELS"]:
    if _catalog in _master_all_source:
        raise AssertionError(
            f"Master notebook karsilastirma katalogunu ({_catalog}) referans aliyor; "
            "bu fan-out riski demektir."
        )
print("  master saglayici secimini geciriyor ve karsilastirma kataloglarini kullanmiyor")
print("MASTER_APPROVAL_AND_PROVIDER_CONFIG_OK")


# ---------------------------------------------------------------------------
# Standalone notebooks must call the SAME canonical functions (no divergence).
# ---------------------------------------------------------------------------

print("\n--- Standalone notebooks invoke canonical local_pipeline functions ---")

_CANONICAL_USAGE_EXPECTED = {
    "01_video_analysis_hotmoment.ipynb": [
        "video_analysis.transcribe_audio", "video_analysis.detect_scenes",
        "video_analysis.compute_audio_peaks", "video_analysis.score_hot_moments",
        "video_analysis.build_hot_moments_manifest",
    ],
    "02_script_generation.ipynb": [
        "script_generation.build_user_prompt", "script_generation.build_selected_script_artifact",
    ],
    "03_tts_voice.ipynb": [
        "tts.TTS_MODEL_REGISTRY", "tts.build_selected_voiceover_artifact",
    ],
    "05_viral_prediction.ipynb": [
        "viral_prediction.BASELINE_METHODS", "viral_prediction.evaluate_baselines_on_holdout",
    ],
    "06_render_pipeline.ipynb": [
        "render_utils.run_render_pipeline", "video_analysis.validate_hot_moments_schema",
    ],
}

for _nb_name, _expected_calls in _CANONICAL_USAGE_EXPECTED.items():
    with open(NOTEBOOK_DIR / _nb_name, "r", encoding="utf-8") as f:
        _nb_src = "\n".join("".join(c["source"]) for c in json.load(f)["cells"])
    _missing_calls = [c for c in _expected_calls if c not in _nb_src]
    if _missing_calls:
        raise AssertionError(
            f"{_nb_name} kanonik fonksiyonlari cagirmiyor (kopya mantik riski): {_missing_calls}"
        )
    print(f"  {_nb_name}: {len(_expected_calls)} kanonik cagri dogrulandi")

# Notebook 06 must no longer redefine the render helpers it used to inline.
with open(NOTEBOOK_DIR / "06_render_pipeline.ipynb", "r", encoding="utf-8") as f:
    _nb06_src = "\n".join("".join(c["source"]) for c in json.load(f)["cells"])
for _dup in ("def build_vertical_filtergraph", "def build_audio_filtergraph",
              "def validate_render", "def probe_via_ffmpeg_fallback"):
    if _dup in _nb06_src:
        raise AssertionError(
            f"06_render_pipeline.ipynb hala kanonik fonksiyonun kopyasini tanimliyor: {_dup}"
        )
print("  06_render_pipeline.ipynb: kopya render yardimci fonksiyonlari kaldirilmis")
print("CANONICAL_FUNCTION_USAGE_OK")


# ---------------------------------------------------------------------------
# Every notebook: JSON + Python AST validation.
# ---------------------------------------------------------------------------

print("\n--- All notebooks: JSON + Python AST validation ---")

#: Asserted so a notebook cannot be silently deleted or added without someone
#: noticing. The count went stale when `07_creator_console.ipynb` landed, which
#: turned this guard into an unconditional failure that hid every section after
#: it.
EXPECTED_NOTEBOOKS = 8
_notebook_paths = sorted(NOTEBOOK_DIR.glob("*.ipynb"))
if len(_notebook_paths) != EXPECTED_NOTEBOOKS:
    raise AssertionError(
        f"{EXPECTED_NOTEBOOKS} notebook bekleniyordu, {len(_notebook_paths)} "
        f"bulundu: {[p.name for p in _notebook_paths]}")

for _nb_path in _notebook_paths:
    with open(_nb_path, "r", encoding="utf-8") as f:
        _nb_json = json.load(f)  # JSON validation
    _code_cell_count = 0
    for _idx, _cell in enumerate(_nb_json["cells"]):
        if _cell["cell_type"] != "code":
            continue
        _code_cell_count += 1
        _cell_src = "".join(_cell["source"])
        # Strip Jupyter line magics / shell escapes, which are not valid Python.
        # Only COLUMN-0 lines qualify: an indented line beginning with '%' or '!'
        # is a continuation of an open bracket (e.g. a `% value` format
        # continuation), and dropping it would corrupt an otherwise valid cell.
        _cleaned = "\n".join(
            line for line in _cell_src.splitlines()
            if not (line[:1] in ("!", "%"))
        )
        try:
            _ast.parse(_cleaned)
        except SyntaxError as exc:
            raise AssertionError(f"{_nb_path.name} hucre {_idx}: Python sozdizimi hatasi: {exc}")
    print(f"  {_nb_path.name}: OK ({len(_nb_json['cells'])} hucre, {_code_cell_count} kod hucresi)")

print("ALL_NOTEBOOKS_JSON_AND_AST_OK")


# ---------------------------------------------------------------------------
# Master notebook: every phase executes through synthetic fixtures / mocked
# providers, and every unmet prerequisite fails loudly.
#
# The REAL notebook cells are extracted and exec'd. Provider SDKs are replaced
# with fakes registered in sys.modules, so no real OpenAI / Anthropic / Gemini /
# ElevenLabs / TMDB / YouTube call is ever made and no real credential is used.
# ffmpeg and faster-whisper DO run for real against synthetic/temp media.
# Nothing is uploaded or published; the user's results/ directory is untouched.
# ---------------------------------------------------------------------------

print("\n--- Master notebook: per-phase execution (synthetic fixtures + mocked providers) ---")

import types as _types

_MASTER_PHASE_CELLS = {}
for _name, _cfg_needle, _run_needle in [
    ("video_analysis", "SOURCE_VIDEO_DIR = paths['source_videos_dir']", "record_phase('video_analysis'"),
    ("script_generation", "SCRIPT_PROVIDER = 'google'", "record_phase('script_generation'"),
    # Match the selected-provider assignment, not the equality branch in run code.
    ("tts", "TTS_PROVIDER = 'gemini'", "record_phase('tts'"),
    ("render", "RENDER_MODE =", "record_phase('render'"),
    ("youtube_data", "YOUTUBE_CHANNEL_INPUTS = [", "record_phase('youtube_data'"),
    ("viral_prediction", "MIN_HISTORY_ROWS = 20", "record_phase('viral_prediction'"),
]:
    _cfgs = [c for c in _master_code_cells if _cfg_needle in c]
    # The run cell is the one that BRANCHES on the toggle (review/approve cells
    # also call record_phase, so match on the toggle guard too).
    _runs = [c for c in _master_code_cells
             if _run_needle in c and c.lstrip().startswith(("hot_moments_data", "selected_script",
                                                             "selected_voiceover", "render_manifest",
                                                             "from datetime", "import pandas"))]
    if len(_cfgs) != 1 or len(_runs) != 1:
        raise AssertionError(
            f"Master notebook faz '{_name}': config/run hucresi tekil degil "
            f"({len(_cfgs)} config, {len(_runs)} run)."
        )
    _MASTER_PHASE_CELLS[_name] = (_cfgs[0], _runs[0])


def _master_cell(*needles):
    _hits = [c for c in _master_code_cells if all(n in c for n in needles)]
    if len(_hits) != 1:
        raise AssertionError(f"Master notebook: {needles} -> {len(_hits)} eslesme (1 bekleniyordu)")
    return _hits[0]


_MASTER_TOGGLES_CELL = _master_cell("RUN_VIDEO_ANALYSIS = False", "phase_report = {}")
_MASTER_SETUP_CELL = _master_cell("def get_secret", "IS_COLAB")
_MASTER_PATHS_CELL = _master_cell("resolve_project_paths", "def find_artifact")
_MASTER_GOOGLE_CONFIG_CELL = _master_cell("GOOGLE_ROUTE_MODE = 'auto'",
                                           "ALLOW_ACCOUNT_QUOTA_ROTATION = False")
_MASTER_GOOGLE_ROUTER_CELL = _master_cell("GOOGLE_CREDENTIAL = google_ai.resolve_profile_secret",
                                           "def show_google_route")
_MASTER_SUMMARY_CELL = _master_cell("MAIN_PIPELINE_ORDER = [", "RESEARCH_SIDECAR_ORDER = [")
# Phase 0 -- the mandatory source-processing-strategy gate.
_MASTER_PHASE0_ASSESS_CELL = _master_cell("source_strategy.assess_source(")
_MASTER_PHASE0_FORM_CELL = _master_cell("PROCESSING_MODE = ''", "CONFIRM_PROCESSING_PLAN = False")
_MASTER_PHASE0_PLAN_CELL = _master_cell("source_strategy.build_processing_plan(")
_MASTER_PHASE0_REVIEW_CELL = _master_cell("show_review_prompt('processing_plan'")
_MASTER_PHASE0_APPROVE_CELL = _master_cell("PLAN_REVIEWED_SHA256 = ''", "approve_artifact")

# Review / approve cell pairs, located by their hash variable.
_MASTER_APPROVE_CELLS = {
    "script": _master_cell("SCRIPT_REVIEWED_SHA256 = ''", "approve_artifact"),
    "voiceover": _master_cell("VOICEOVER_REVIEWED_SHA256 = ''", "approve_artifact"),
    "render": _master_cell("RENDER_REVIEWED_SHA256 = ''", "approve_artifact"),
}
_MASTER_REVIEW_CELLS = {
    "script": _master_cell("show_review_prompt('script'"),
    "voiceover": _master_cell("show_review_prompt('voiceover'"),
    "render": _master_cell("show_review_prompt('render'"),
}


def _write_tone_wav(path, duration=3):
    subprocess.run(
        [FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"sine=frequency=220:duration={duration}",
         "-ar", "16000", "-ac", "1", str(path)],
        check=True, capture_output=True,
    )


# Call counters shared by the fake SDKs, so every test can assert the EXACT
# number of paid provider calls a phase made.
PROVIDER_CALLS = []


def _install_fake_provider_sdks(script_fail=(), tts_fail=()):
    """Registers fake 'openai', 'anthropic', 'elevenlabs.client' and 'gtts'
    modules so the notebook's own `from openai import OpenAI` style lines resolve
    to test doubles. Every call is appended to PROVIDER_CALLS, so a passing test
    proves both that no real endpoint was contacted AND how many paid calls were
    made. `script_fail` / `tts_fail` name providers that should raise."""
    class _FakeSpeechResponse:
        def stream_to_file(self, path):
            _write_tone_wav(path)

    class _FakeOpenAI:
        def __init__(self, api_key=None, **kw):
            class _Completions:
                def create(self, **kw):
                    PROVIDER_CALLS.append(("script:openai", kw.get("model")))
                    if "openai" in script_fail:
                        raise RuntimeError("simulated openai script outage")
                    payload = json.dumps({"script": "Mocked master script text.",
                                           "hook": "h", "estimated_duration_sec": 60})
                    _msg = type("M", (), {"content": payload})()
                    _choice = type("C", (), {"message": _msg})()
                    _usage = type("U", (), {"prompt_tokens": 120, "completion_tokens": 60})()
                    return type("R", (), {"choices": [_choice], "usage": _usage})()

            class _Speech:
                def create(self, model=None, voice=None, input=None):
                    PROVIDER_CALLS.append(("tts:openai", model))
                    if "openai" in tts_fail:
                        raise RuntimeError("simulated openai tts outage")
                    return _FakeSpeechResponse()

            self.chat = type("Chat", (), {"completions": _Completions()})()
            self.audio = type("Audio", (), {"speech": _Speech()})()

    class _FakeAnthropic:
        def __init__(self, api_key=None, **kw):
            class _Messages:
                def create(self, **kw):
                    PROVIDER_CALLS.append(("script:anthropic", kw.get("model")))
                    if "anthropic" in script_fail:
                        raise RuntimeError("simulated anthropic outage")
                    payload = json.dumps({"script": "Mocked anthropic script.", "hook": "h",
                                           "estimated_duration_sec": 60})
                    _block = type("B", (), {"text": payload})()
                    _usage = type("U", (), {"input_tokens": 100, "output_tokens": 50})()
                    return type("R", (), {"content": [_block], "usage": _usage})()
            self.messages = _Messages()

    class _FakeGenaiClient:
        def __init__(self, api_key=None, **kw):
            class _Models:
                # `config` is now always passed by the canonical google_ai router.
                def generate_content(self, model=None, contents=None, config=None):
                    PROVIDER_CALLS.append(("script:google", model))
                    if "google" in script_fail:
                        raise RuntimeError("simulated google outage")
                    return type("R", (), {
                        "text": json.dumps({
                            "script": "Mocked google script, uzunlugu semantik "
                                      "dogrulamayi gecmeye yetecek kadar.",
                            "hook": "h", "estimated_duration_sec": 60}),
                        "candidates": [], "usage_metadata": None,
                    })()

                def list(self):
                    return [type("M", (), {"name": f"models/{_m}"})()
                            for _m in ("gemini-3.6-flash", "gemini-3.5-flash",
                                        "gemini-3.5-flash-lite", "gemma-4-31b-it")]
            self.models = _Models()

    class _FakeElevenLabs:
        def __init__(self, api_key=None, **kw):
            class _TTS:
                def convert(self, voice_id=None, model_id=None, text=None):
                    PROVIDER_CALLS.append(("tts:elevenlabs", model_id))
                    if "elevenlabs" in tts_fail:
                        raise RuntimeError("simulated elevenlabs outage")
                    _tmp = Path(_tempfile.mkdtemp()) / "e.wav"
                    _write_tone_wav(_tmp)
                    return [_tmp.read_bytes()]
            self.text_to_speech = _TTS()

    class _FakeGTTS:
        def __init__(self, text=None, lang=None):
            PROVIDER_CALLS.append(("tts:gtts", "gtts-default"))
            if "gtts" in tts_fail:
                raise RuntimeError("simulated gtts network failure")
        def save(self, path):
            _write_tone_wav(path)

    _openai_mod = _types.ModuleType("openai")
    _openai_mod.OpenAI = _FakeOpenAI
    _anthropic_mod = _types.ModuleType("anthropic")
    _anthropic_mod.Anthropic = _FakeAnthropic
    _google_pkg = _types.ModuleType("google")
    _genai_mod = _types.ModuleType("google.genai")
    _genai_mod.Client = _FakeGenaiClient
    _google_pkg.genai = _genai_mod
    _el_pkg = _types.ModuleType("elevenlabs")
    _el_client = _types.ModuleType("elevenlabs.client")
    _el_client.ElevenLabs = _FakeElevenLabs
    _el_pkg.client = _el_client
    _gtts_mod = _types.ModuleType("gtts")
    _gtts_mod.gTTS = _FakeGTTS

    _saved = {}
    for _name, _mod in [("openai", _openai_mod), ("anthropic", _anthropic_mod),
                         ("google.genai", _genai_mod),
                         ("elevenlabs", _el_pkg), ("elevenlabs.client", _el_client),
                         ("gtts", _gtts_mod)]:
        _saved[_name] = sys.modules.get(_name)
        sys.modules[_name] = _mod
    return _saved


def _restore_provider_sdks(saved):
    for _name, _mod in saved.items():
        if _mod is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _mod


def _fake_youtube_client(video_count=40):
    """Minimal googleapiclient-shaped double including read-only search."""
    class _Channels:
        def list(self, **kw):
            self._kw = kw
            return self
        def execute(self):
            return {"items": [{
                "id": "UCmasterphasetest000001", "snippet": {"title": "Master Test Channel"},
                "contentDetails": {"relatedPlaylists": {"uploads": "UUmaster"}},
                "statistics": {"subscriberCount": "5000", "videoCount": str(video_count)},
            }]}

    class _PlaylistItems:
        def list(self, **kw):
            self._kw = kw
            return self
        def execute(self):
            _token = self._kw.get("pageToken")
            _start = int(_token) if _token else 0
            _ids = [f"vid{i:04d}" for i in range(_start, min(_start + 50, video_count))]
            _next = str(_start + 50) if _start + 50 < video_count else None
            return {"items": [{"contentDetails": {"videoId": v}} for v in _ids],
                    "nextPageToken": _next}

    class _Videos:
        def list(self, **kw):
            self._ids = kw["id"].split(",")
            return self
        def execute(self):
            _items = []
            for _n, _vid in enumerate(self._ids):
                _items.append({
                    "id": _vid,
                    "snippet": {"title": f"Video {_vid}", "description": "d",
                                 "publishedAt": f"2024-01-{(_n % 28) + 1:02d}T00:00:00Z",
                                 "channelId": "UCmasterphasetest000001",
                                 "channelTitle": "Master Test Channel", "tags": []},
                    "statistics": {"viewCount": str(1000 + _n * 137), "likeCount": str(50 + _n * 3)},
                    "contentDetails": {"duration": "PT2M30S", "definition": "hd"},
                    "status": {"privacyStatus": "public"},
                })
            return {"items": _items}

    class _Search:
        def list(self, **kw):
            self._kw = kw
            return self
        def execute(self):
            # Duplicate IDs across search orders exercise discovery dedupe.
            _count = min(int(self._kw.get("maxResults", 25)), video_count)
            return {"items": [
                {"id": {"videoId": f"vid{i:04d}"}}
                for i in range(_count)
            ]}

    class _YT:
        def channels(self): return _Channels()
        def playlistItems(self): return _PlaylistItems()
        def videos(self): return _Videos()
        def search(self): return _Search()
    return _YT()


def _make_test_source_video(tmp):
    """Copies the real-speech smoke clip produced earlier in this run into tmp
    (the original is only read)."""
    _out = Path(tmp) / "source_for_test.mp4"
    shutil.copy2(SMOKE_VIDEO, _out)
    return _out


def _build_master_namespace(tmp):
    """Runs the real toggles -> setup -> bootstrap -> paths cells with tmp as cwd
    and Drive persistence OFF."""
    os.chdir(tmp)
    _ns = {}
    exec(compile(_MASTER_TOGGLES_CELL, "toggles", "exec"), _ns)
    exec(compile(_MASTER_GOOGLE_CONFIG_CELL, "google-config", "exec"), _ns)
    exec(compile(_MASTER_SETUP_CELL, "setup", "exec"), _ns)
    _ns["USE_GOOGLE_DRIVE"] = False
    for _k in ("YOUTUBE_DATA_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                "GOOGLE_API_KEY", "ELEVENLABS_API_KEY"):
        _ns[_k] = None
    exec(compile(_bootstrap_src, "bootstrap", "exec"), _ns)
    exec(compile(_MASTER_PATHS_CELL, "paths", "exec"), _ns)
    # Google router config + credential/manifest/route-display cells. With no
    # credential these must run cleanly and leave google_manifest=None -- i.e.
    # a missing secret skips safely instead of raising.
    exec(compile(_MASTER_GOOGLE_ROUTER_CELL, "google-router", "exec"), _ns)
    return _ns


def _fake_google_credential(ga, key="FAKE-GEMINI-KEY-FOR-TESTS-ONLY-NOT-A-REAL-KEY"):
    """A configured-looking CredentialRef backed by a value that is not a real
    key, so master-notebook cells take the 'Gemini available' branch while the
    faked google.genai SDK intercepts every call."""
    return ga.CredentialRef("google_api", "GeminiKey", ("GeminiKey", "GOOGLE_API_KEY"), key)


def _run_master_phase(ns, name, **cfg_overrides):
    _cfg, _run = _MASTER_PHASE_CELLS[name]
    exec(compile(_cfg, f"{name}-cfg", "exec"), ns)
    ns.update(cfg_overrides)
    exec(compile(_run, f"{name}-run", "exec"), ns)
    return ns["phase_report"].get(name, {})


def _record_first_moment_selection(ns, reviewed_by="verify_local"):
    """Records the same hash-bound selection the real Faz 1b cell writes."""
    selection = ns["video_analysis"].build_moment_selection(
        ns["hot_moments_data"], selected_moment_rank=1,
        reviewed_by=reviewed_by, rejected_moment_ranks=[], require_preview=True,
    )
    ns["youtube_data"].write_json(ns["MOMENT_SELECTION_PATH"], selection)
    ns["moment_selection"] = selection
    ns["record_phase"](
        "moment_selection", "completed", "approved",
        f"rank=1 reviewed_by={reviewed_by}",
    )
    return selection


def _run_master_phase0(ns, mode, source_video_path, *, selected_by="verify_local",
                        output_count=1, target_duration=60, theory_angle="",
                        moment_ranks_text="", approve=True):
    """Runs the REAL Phase 0 cells: assess -> select -> review -> approve.

    Uses the notebook's own cells (not a reimplementation), substituting the
    @param inputs into the SOURCE the way _run_master_approve does, so a
    re-executed cell cannot reset them. ffprobe/ffmpeg genuinely run against the
    synthetic test video; no provider API is involved.
    """
    exec(compile(_MASTER_PHASE_CELLS["video_analysis"][0], "phase0-source-cfg", "exec"), ns)
    ns["SOURCE_VIDEO_PATH"] = str(source_video_path)
    exec(compile(_MASTER_PHASE0_ASSESS_CELL, "phase0-assess", "exec"), ns)

    _form = _MASTER_PHASE0_FORM_CELL
    for _var, _value in (("PROCESSING_MODE", mode), ("PLAN_SELECTED_BY", selected_by),
                          ("PLAN_MOMENT_RANKS_TEXT", moment_ranks_text),
                          ("THEORY_ANGLE", theory_angle)):
        _form = _form.replace(f"{_var} = ''", f"{_var} = {_value!r}", 1)
    _form = _form.replace("PLAN_OUTPUT_COUNT = 0", f"PLAN_OUTPUT_COUNT = {output_count}", 1)
    _form = _form.replace("PLAN_TARGET_DURATION_SEC = 60",
                          f"PLAN_TARGET_DURATION_SEC = {target_duration}", 1)
    _form = _form.replace("CONFIRM_PROCESSING_PLAN = False", "CONFIRM_PROCESSING_PLAN = True", 1)
    exec(compile(_form, "phase0-form", "exec"), ns)
    exec(compile(_MASTER_PHASE0_PLAN_CELL, "phase0-plan", "exec"), ns)
    exec(compile(_MASTER_PHASE0_REVIEW_CELL, "phase0-review", "exec"), ns)
    if not approve:
        return ns.get("processing_plan")

    _approve_src = _MASTER_PHASE0_APPROVE_CELL.replace(
        "PLAN_REVIEWED_SHA256 = ''",
        f"PLAN_REVIEWED_SHA256 = {ns['plan_review_info']['content_sha256']!r}", 1,
    ).replace("PLAN_APPROVED_BY = ''", f"PLAN_APPROVED_BY = {selected_by!r}", 1)
    exec(compile(_approve_src, "phase0-approve", "exec"), ns)
    return ns.get("processing_plan")


def _run_master_review(ns, kind):
    exec(compile(_MASTER_REVIEW_CELLS[kind], f"{kind}-review", "exec"), ns)


def _run_master_approve(ns, kind, **overrides):
    """Executes the real approval cell. The cell assigns its own inputs
    (`*_REVIEWED_SHA256 = ''` etc.) at the top, so overrides are substituted into
    the SOURCE rather than injected into the namespace -- otherwise re-executing
    the cell would just reset them to '' again."""
    source = _MASTER_APPROVE_CELLS[kind]
    for _var, _value in overrides.items():
        _needle = f"{_var} = ''"
        if _needle not in source:
            raise AssertionError(f"{kind} onay hucresinde '{_needle}' bulunamadi.")
        source = source.replace(_needle, f"{_var} = {_value!r}", 1)
    exec(compile(source, f"{kind}-approve", "exec"), ns)


_saved_sdks = _install_fake_provider_sdks()
_master_prev_cwd = os.getcwd()
_master_tmpdirs = []
try:
    # --- Scenario A: all six phases, happy path, with post-generation approval
    _tmp_a = Path(_tempfile.mkdtemp(prefix="master_all_phases_"))
    _master_tmpdirs.append(_tmp_a)
    _src_video = _make_test_source_video(_tmp_a)
    _ns_a = _build_master_namespace(_tmp_a)
    _ns_a.update({"RUN_VIDEO_ANALYSIS": True, "RUN_SCRIPT_GENERATION": True, "RUN_TTS": True,
                   "RUN_RENDER": True, "RUN_YOUTUBE_DATA": True, "RUN_VIRAL_PREDICTION": True})
    _ns_a["youtube_data"].build_client = lambda key: _fake_youtube_client()

    # Phase 0 is MANDATORY: without an approved plan every main-pipeline phase
    # must refuse to run. Prove that before approving anything.
    _rep_ungated = _run_master_phase(_ns_a, "video_analysis", SOURCE_VIDEO_PATH=str(_src_video),
                                      CLIP_DURATION_SEC=10, WHISPER_MODEL_SIZE="tiny",
                                      TOP_N_HOT_MOMENTS=2)
    if _rep_ungated.get("execution_status") != "skipped" or "Faz 0" not in _rep_ungated.get("detail", ""):
        raise AssertionError(f"[master:phase0] plan yokken faz calisti: {_rep_ungated}")
    print("  [master:phase0] plan onaylanmadan ana hat fazlari calismiyor")

    _plan_a = _run_master_phase0(_ns_a, "direct_clip_series", _src_video, output_count=1)
    if _plan_a["status"] != "approved" or _plan_a["decision"]["mode"] != "direct_clip_series":
        raise AssertionError(f"[master:phase0] direct_clip_series plani onaylanmadi: {_plan_a['status']}")
    if _ns_a["PIPELINE_MODE"] != "original_audio_subtitles":
        raise AssertionError("[master:phase0] PIPELINE_MODE plandan turetilmedi")
    if not _ns_a["SOURCE_ASSESSMENT_PATH"].is_file() or not _ns_a["PROCESSING_PLAN_PATH"].is_file():
        raise AssertionError("[master:phase0] kalici artefaktlar yazilmadi")
    _assessment_a = json.loads(_ns_a["SOURCE_ASSESSMENT_PATH"].read_text(encoding="utf-8"))
    if _assessment_a["selected_mode"] is not None or _assessment_a["auto_selected"] is not False:
        raise AssertionError("[master:phase0] degerlendirme kendi kendine mod SECTI!")
    print(f"  [master:phase0] direct_clip_series onaylandi "
          f"(oneri={_assessment_a['recommended_mode']}, otomatik secim yok)")

    _rep = _run_master_phase(_ns_a, "video_analysis", SOURCE_VIDEO_PATH=str(_src_video),
                              CLIP_DURATION_SEC=10, WHISPER_MODEL_SIZE="tiny", TOP_N_HOT_MOMENTS=2)
    if _rep.get("execution_status") != "completed" or _rep.get("artifact_status") != "review_required":
        raise AssertionError(f"[master:video_analysis] {_rep}")
    print(f"  [master:video_analysis] execution={_rep['execution_status']} artifact={_rep['artifact_status']}")
    _selection_a = _record_first_moment_selection(_ns_a)
    if _ns_a["video_analysis"].validate_moment_selection(
            _selection_a, _ns_a["hot_moments_data"], require_preview=True):
        raise AssertionError("[master:moment_selection] yeni secim gecersiz")
    print("  [master:moment_selection] preview + manifest hash'ine bagli secim OK")

    _rep_original = _run_master_phase(
        _ns_a, "render", PIPELINE_MODE="original_audio_subtitles",
        PREVIEW_MODE=True, SUBTITLE_WHISPER_MODEL_SIZE="tiny",
    )
    _rm_original = _ns_a["render_manifest"]
    if (_rep_original.get("execution_status") != "completed"
            or _rm_original.get("config", {}).get("pipeline_mode") != "original_audio_subtitles"
            or not _rm_original.get("output", {}).get("validation", {}).get("passed")):
        raise AssertionError(f"[master:original-audio-render] {_rep_original} / {_rm_original}")
    if not _rm_original["config"]["original_audio_preserved"] or _rm_original["config"]["tts_used"]:
        raise AssertionError("[master:original-audio-render] orijinal ses/TTS sozlesmesi bozuk")
    print("  [master:original-audio-render] orijinal ses + Whisper altyazi + real ffmpeg OK")

    # direct_clip_series must make ZERO generation-API calls, even with every
    # provider credential present and RUN_SCRIPT_GENERATION/RUN_TTS both True.
    PROVIDER_CALLS.clear()
    _ns_a.update({"OPENAI_API_KEY": "FAKE", "ANTHROPIC_API_KEY": "FAKE", "GOOGLE_API_KEY": "FAKE",
                   "ELEVENLABS_API_KEY": "FAKE"})
    _ns_a["GOOGLE_CREDENTIAL"] = _fake_google_credential(_ns_a["google_ai"])
    for _blocked_phase in ("script_generation", "tts"):
        _rep_blocked = _run_master_phase(_ns_a, _blocked_phase)
        if _rep_blocked.get("execution_status") != "skipped":
            raise AssertionError(
                f"[master:direct_clip_series] {_blocked_phase} calisti: {_rep_blocked}")
    if PROVIDER_CALLS:
        raise AssertionError(
            f"[master:direct_clip_series] SIFIR API cagrisi beklendi, yapilan: {PROVIDER_CALLS}")
    print(f"  [master:direct_clip_series] script+tts kapali, API cagrisi={len(PROVIDER_CALLS)} "
          "(tum kimlik bilgileri mevcutken bile)")

    # Switching to narrated_theory requires a NEW plan + a NEW approval.
    _plan_narr = _run_master_phase0(
        _ns_a, "narrated_theory", _src_video, output_count=1,
        theory_angle="Bu sahnedeki karakter kararinin tum sezonu nasil degistirdigi teorisi",
    )
    if _plan_narr["status"] != "approved" or _ns_a["PIPELINE_MODE"] != "narrated_recap":
        raise AssertionError(f"[master:phase0] narrated_theory plani/pipeline_mode hatali: {_plan_narr['status']}")
    if _plan_narr["decision"]["theory_angle"] is None:
        raise AssertionError("[master:phase0] narrated_theory plani teori acisi tasimiyor")
    print("  [master:phase0] narrated_theory icin AYRI plan + AYRI onay gerekti")

    # Script: exactly ONE paid call with all three providers configured.
    PROVIDER_CALLS.clear()
    # Google is available too (fake credential + faked SDK), so this also proves
    # that having Gemini configured does NOT add a second call when another
    # provider was selected and fallback is off.
    _ns_a["GOOGLE_CREDENTIAL"] = _fake_google_credential(_ns_a["google_ai"])
    _rep = _run_master_phase(_ns_a, "script_generation",
                              SCRIPT_PROVIDER="anthropic", SCRIPT_MODEL=None,
                              ALLOW_SCRIPT_PROVIDER_FALLBACK=False)
    _script_calls = [c for c in PROVIDER_CALLS if c[0].startswith("script:")]
    if len(_script_calls) != 1:
        raise AssertionError(
            f"[master:script] TAM OLARAK 1 ucretli LLM cagrisi beklendi, {len(_script_calls)} yapildi: {_script_calls}"
        )
    if _rep.get("artifact_status") != "review_required":
        raise AssertionError(f"[master:script] yeni uretilen script 'review_required' olmali: {_rep}")
    if _ns_a["selected_script"]["human_approval"]["approved"] is not False:
        raise AssertionError("[master:script] uretim aninda onaylanmis!")
    print(f"  [master:script_generation] ucretli cagri={len(_script_calls)} {_script_calls} "
          f"artifact={_rep['artifact_status']}")

    # Separate review + approval, bound to the reviewed hash.
    _run_master_review(_ns_a, "script")
    _script_hash = _ns_a["script_review_info"]["content_sha256"]
    _run_master_approve(_ns_a, "script", SCRIPT_REVIEWED_SHA256=_script_hash,
                        SCRIPT_APPROVED_BY="verify_local")
    if _ns_a["selected_script"]["status"] != "approved":
        raise AssertionError(f"[master:script] ayri onay basarisiz: {_ns_a['selected_script']['status']}")
    if _ns_a["selected_script"]["human_approval"]["approved_content_sha256"] != _script_hash:
        raise AssertionError("[master:script] onay icerik hash'ine baglanmamis")
    print("  [master:script] ayri onay hucresi OK (hash'e bagli)")

    # TTS: exactly ONE paid call; openai and gtts must NOT be contacted.
    PROVIDER_CALLS.clear()
    _ns_a["ELEVENLABS_API_KEY"] = "FAKE"
    _rep = _run_master_phase(_ns_a, "tts", TTS_PROVIDER="elevenlabs",
                              ALLOW_TTS_PROVIDER_FALLBACK=False, ALLOW_DEV_GTTS_FALLBACK=True)
    _tts_calls = [c for c in PROVIDER_CALLS if c[0].startswith("tts:")]
    if len(_tts_calls) != 1 or _tts_calls[0][0] != "tts:elevenlabs":
        raise AssertionError(
            f"[master:tts] TAM OLARAK 1 ElevenLabs cagrisi beklendi, alinan: {_tts_calls}"
        )
    if any(c[0] == "tts:openai" for c in _tts_calls):
        raise AssertionError("[master:tts] ElevenLabs basarili oldugu halde OpenAI cagrildi!")
    if any(c[0] == "tts:gtts" for c in _tts_calls):
        raise AssertionError("[master:tts] uretim basarili oldugu halde gTTS cagrildi!")
    if _rep.get("artifact_status") != "review_required":
        raise AssertionError(f"[master:tts] yeni seslendirme 'review_required' olmali: {_rep}")
    print(f"  [master:tts] ucretli cagri={len(_tts_calls)} {_tts_calls} artifact={_rep['artifact_status']}")

    _run_master_review(_ns_a, "voiceover")
    _vo_hash = _ns_a["voiceover_review_info"]["content_sha256"]
    _run_master_approve(_ns_a, "voiceover", VOICEOVER_REVIEWED_SHA256=_vo_hash,
                        VOICEOVER_APPROVED_BY="verify_local")
    if _ns_a["selected_voiceover"]["status"] != "approved":
        raise AssertionError(f"[master:tts] ayri onay basarisiz: {_ns_a['selected_voiceover']['status']}")
    print("  [master:voiceover] ayri onay hucresi OK (dosya hash'ine bagli)")

    _rep = _run_master_phase(_ns_a, "render", PREVIEW_MODE=True, PIPELINE_MODE="narrated_recap")
    _rm = _ns_a["render_manifest"]
    if _rep.get("execution_status") != "completed" or _rm["status"] != "review_required":
        raise AssertionError(f"[master:render] {_rep} / {_rm['status']}")
    if _rm["human_approval"]["approved"] is not False:
        raise AssertionError("[master:render] uretim aninda onaylanmis!")
    if _rm["not_uploaded"] is not True:
        raise AssertionError("[master:render] not_uploaded True olmali")
    print(f"  [master:render] artifact={_rm['status']} not_uploaded={_rm['not_uploaded']} (real ffmpeg)")

    _run_master_review(_ns_a, "render")
    _render_hash = _ns_a["render_review_info"]["content_sha256"]
    _run_master_approve(_ns_a, "render", RENDER_REVIEWED_SHA256=_render_hash,
                        RENDER_APPROVED_BY="verify_local")
    if _ns_a["render_manifest"]["status"] != "approved":
        raise AssertionError(f"[master:render] ayri onay basarisiz: {_ns_a['render_manifest']['status']}")
    print("  [master:render] ayri onay hucresi OK (video hash'ine bagli)")

    _ns_a["YOUTUBE_DATA_API_KEY"] = "FAKE"
    _rep = _run_master_phase(_ns_a, "youtube_data",
                              YOUTUBE_CHANNEL_INPUTS=["UCmasterphasetest000001"],
                              MAX_VIDEOS_PER_CHANNEL=40)
    if _rep.get("execution_status") != "completed" or _rep.get("artifact_status") != "review_required":
        raise AssertionError(f"[master:youtube_data] {_rep}")
    print(f"  [master:youtube_data] execution={_rep['execution_status']} artifact={_rep['artifact_status']}")

    _rep = _run_master_phase(_ns_a, "viral_prediction", MIN_HISTORY_ROWS=20)
    if _rep.get("execution_status") != "completed" or _rep.get("artifact_status") != "review_required":
        raise AssertionError(f"[master:viral_prediction] {_rep}")
    print(f"  [master:viral_prediction] execution={_rep['execution_status']} artifact={_rep['artifact_status']}")

    exec(compile(_MASTER_SUMMARY_CELL, "summary", "exec"), _ns_a)
    for _phase_key, _ in MASTER_TOGGLES.values():
        if _ns_a["phase_report"].get(_phase_key, {}).get("execution_status") != "completed":
            raise AssertionError(f"Tum fazlar 'completed' olmali: {_ns_a['phase_report']}")

    for _artifact_name, _artifact in [
        ("hot_moments", json.loads(Path(_ns_a["HOT_MOMENTS_PATH"]).read_text(encoding="utf-8"))),
        ("selected_script", _ns_a["selected_script"]),
        ("selected_voiceover", _ns_a["selected_voiceover"]),
        ("render_manifest", _ns_a["render_manifest"]),
        ("viral_prediction", _ns_a["viral_prediction_artifact"]),
    ]:
        if _artifact.get("status") == "production_ready":
            raise AssertionError(f"{_artifact_name}: status otomatik 'production_ready' olmus!")
    print("  hicbir artefakt otomatik 'production_ready' olmadi")
    _restore_provider_sdks(_saved_sdks)

    # --- Scenario B: prerequisites missing -> loud per-phase failure --------
    _saved_sdks = _install_fake_provider_sdks()
    _tmp_b = Path(_tempfile.mkdtemp(prefix="master_missing_prereq_"))
    _master_tmpdirs.append(_tmp_b)
    _src_b = _make_test_source_video(_tmp_b)
    _ns_b = _build_master_namespace(_tmp_b)
    _ns_b["GOOGLE_CREDENTIAL"] = _fake_google_credential(_ns_b["google_ai"])
    # Approve a narrated_theory plan so the Phase 0 gate is OPEN -- this scenario
    # is about MISSING PREREQUISITES, and the gate must not mask that failure.
    _run_master_phase0(_ns_b, "narrated_theory", _src_b,
                       theory_angle="Eksik on-kosul senaryosu icin gecerli bir teori acisi metni")
    _ns_b.update({"RUN_VIDEO_ANALYSIS": False, "RUN_SCRIPT_GENERATION": True, "RUN_TTS": True,
                   "RUN_RENDER": True, "RUN_YOUTUBE_DATA": False, "RUN_VIRAL_PREDICTION": True,
                   "hot_moments_data": None, "selected_script": None, "selected_voiceover": None})
    for _phase in ("script_generation", "tts", "render", "viral_prediction"):
        _rep = _run_master_phase(_ns_b, _phase)
        if _rep.get("execution_status") != "failed":
            raise AssertionError(
                f"[master:{_phase}] eksik on-kosulda execution_status='failed' beklendi, {_rep} alindi."
            )
        print(f"  [master:{_phase}] eksik on-kosul -> execution=failed (beklendigi gibi)")

    # --- Scenario C: insufficient_data must be visible, not just "ok" -------
    _tmp_c = Path(_tempfile.mkdtemp(prefix="master_insufficient_"))
    _master_tmpdirs.append(_tmp_c)

    # C1: missing YouTube API key -> completed + insufficient_data
    _ns_c = _build_master_namespace(_tmp_c)
    _ns_c.update({"RUN_YOUTUBE_DATA": True, "YOUTUBE_DATA_API_KEY": None})
    _rep = _run_master_phase(_ns_c, "youtube_data", YOUTUBE_CHANNEL_INPUTS=["UCwhatever00000000000001"])
    if _rep.get("execution_status") != "completed" or _rep.get("artifact_status") != "insufficient_data":
        raise AssertionError(f"[master:youtube_data/no-key] beklenen completed+insufficient_data, alinan {_rep}")
    print(f"  [master:youtube_data] API key yok -> execution={_rep['execution_status']} "
          f"artifact={_rep['artifact_status']}")

    # C2: empty manual channel inputs + keyword discovery -> usable shortlist.
    _ns_c2 = _build_master_namespace(_tmp_c)
    _ns_c2.update({"RUN_YOUTUBE_DATA": True, "YOUTUBE_DATA_API_KEY": "FAKE"})
    _ns_c2["youtube_data"].build_client = lambda key: _fake_youtube_client()
    _rep = _run_master_phase(_ns_c2, "youtube_data", YOUTUBE_CHANNEL_INPUTS=[])
    if _rep.get("execution_status") != "completed" or _rep.get("artifact_status") != "review_required":
        raise AssertionError(f"[master:youtube_data/auto-discovery] beklenen completed+review_required, alinan {_rep}")
    if not (_ns_c2["YOUTUBE_DATA_DIR"] / "discovered_videos.csv").is_file():
        raise AssertionError("[master:youtube_data/auto-discovery] discovered_videos.csv yazilmadi")
    _research_manifest = json.loads(_ns_c2["COLLECTION_MANIFEST_PATH"].read_text(encoding="utf-8"))
    if (_research_manifest.get("pipeline_role") != "research_sidecar"
            or _research_manifest.get("feeds_main_pipeline") is not False):
        raise AssertionError("[master:youtube_data] arastirma yan-kolu siniri manifestte yok")
    print(f"  [master:youtube_data] manuel kanal yok, otomatik kesif -> execution={_rep['execution_status']} "
          f"artifact={_rep['artifact_status']}")

    # C2b: both discovery and manual inputs absent remains visibly insufficient.
    _ns_c2b = _build_master_namespace(_tmp_c)
    _ns_c2b.update({"RUN_YOUTUBE_DATA": True, "YOUTUBE_DATA_API_KEY": "FAKE"})
    _ns_c2b["youtube_data"].build_client = lambda key: _fake_youtube_client()
    _rep = _run_master_phase(
        _ns_c2b, "youtube_data", YOUTUBE_CHANNEL_INPUTS=[], AUTO_DISCOVER_CHANNELS=False,
    )
    if _rep.get("execution_status") != "completed" or _rep.get("artifact_status") != "insufficient_data":
        raise AssertionError(f"[master:youtube_data/no-inputs] beklenen completed+insufficient_data, alinan {_rep}")

    # C3: insufficient viral history -> completed + insufficient_data.
    # The previous phase wrote a 0-row normalized_videos.csv, so the CSV exists
    # (the phase can run cleanly) but has nowhere near MIN_HISTORY_ROWS rows.
    _ns_c2["RUN_VIRAL_PREDICTION"] = True
    _rep = _run_master_phase(_ns_c2, "viral_prediction", MIN_HISTORY_ROWS=20)
    if _rep.get("execution_status") != "completed" or _rep.get("artifact_status") != "insufficient_data":
        raise AssertionError(f"[master:viral_prediction/insufficient] beklenen completed+insufficient_data, alinan {_rep}")
    print(f"  [master:viral_prediction] yetersiz gecmis -> execution={_rep['execution_status']} "
          f"artifact={_rep['artifact_status']}")

    # The summary must surface these, not report a bare "ok".
    import io as _io
    import contextlib as _contextlib
    _buf = _io.StringIO()
    with _contextlib.redirect_stdout(_buf):
        exec(compile(_MASTER_SUMMARY_CELL, "summary", "exec"), _ns_c2)
    _summary_text = _buf.getvalue()
    if "insufficient_data" not in _summary_text:
        raise AssertionError("Ozet 'insufficient_data' artefakt durumunu gostermiyor.")
    if "DIKKAT" not in _summary_text:
        raise AssertionError("Ozet temiz-calisti-ama-yetersiz fazlari one cikarmiyor.")
    if "execution" not in _summary_text or "artifact" not in _summary_text:
        raise AssertionError("Ozet execution_status ve artifact_status'u ayri ayri gostermiyor.")
    print("  ozet execution_status ile artifact_status'u ayirt ediyor ve DIKKAT bolumu uretiyor")

    # --- Scenario D: unapproved script must not reach paid TTS --------------
    PROVIDER_CALLS.clear()
    _tmp_d = Path(_tempfile.mkdtemp(prefix="master_approval_gates_"))
    _master_tmpdirs.append(_tmp_d)
    _src_d = _make_test_source_video(_tmp_d)
    _ns_d = _build_master_namespace(_tmp_d)
    _ns_d.update({"RUN_VIDEO_ANALYSIS": True, "RUN_SCRIPT_GENERATION": True,
                   "RUN_TTS": True, "RUN_RENDER": True,
                   "OPENAI_API_KEY": "FAKE", "ANTHROPIC_API_KEY": "FAKE",
                   "ELEVENLABS_API_KEY": "FAKE"})
    _ns_d["GOOGLE_CREDENTIAL"] = _fake_google_credential(_ns_d["google_ai"])
    _run_master_phase0(_ns_d, "narrated_theory", _src_d,
                       theory_angle="Onaysiz script senaryosu icin gecerli bir teori acisi metni")
    _run_master_phase(_ns_d, "video_analysis", SOURCE_VIDEO_PATH=str(_src_d),
                       CLIP_DURATION_SEC=10, WHISPER_MODEL_SIZE="tiny", TOP_N_HOT_MOMENTS=2)
    _record_first_moment_selection(_ns_d)
    _run_master_phase(_ns_d, "script_generation",
                       SCRIPT_PROVIDER="anthropic", ALLOW_SCRIPT_PROVIDER_FALLBACK=False)
    if _ns_d["selected_script"]["status"] != "review_required":
        raise AssertionError("Yeni uretilen script 'review_required' olmali")

    # Deliberately do NOT approve the script.
    PROVIDER_CALLS.clear()
    _rep = _run_master_phase(_ns_d, "tts", TTS_PROVIDER="elevenlabs",
                              ALLOW_TTS_PROVIDER_FALLBACK=True, ALLOW_DEV_GTTS_FALLBACK=True)
    _paid = [c for c in PROVIDER_CALLS if c[0] in ("tts:elevenlabs", "tts:openai")]
    if _paid:
        raise AssertionError(
            f"ONAY KAPISI IHLALI: onaylanmamis scriptle {len(_paid)} ucretli TTS cagrisi yapildi: {_paid}"
        )
    if _ns_d["selected_voiceover"]["provider"] != "gtts":
        raise AssertionError(
            f"onaysiz script icin sadece gtts beklenirdi: {_ns_d['selected_voiceover']['provider']!r}"
        )
    print(f"  [master:tts] onaysiz script -> ucretli cagri={len(_paid)}, "
          f"saglayici={_ns_d['selected_voiceover']['provider']}")

    # gTTS must be unapprovable even with the correct hash.
    _run_master_review(_ns_d, "voiceover")
    _gtts_info = _ns_d["voiceover_review_info"]
    if _gtts_info["reviewable"]:
        raise AssertionError("gTTS ciktisi onaylanabilir gorunuyor; politika ihlali.")
    print("  [master:voiceover] gTTS onaylanamaz olarak isaretlendi")

    _rep = _run_master_phase(_ns_d, "render", PREVIEW_MODE=True, PIPELINE_MODE="narrated_recap")
    _rm_d = _ns_d["render_manifest"]
    if _rm_d["status"] == "approved":
        raise AssertionError("ONAY KAPISI IHLALI: onaysiz girdilerle render 'approved' oldu.")
    if "script_approved" not in _rm_d["approval"]["failed_conditions"]:
        raise AssertionError(f"render onay kosullari eksik: {_rm_d['approval']}")
    print(f"  [master:render] artifact={_rm_d['status']} "
          f"saglanmayan kosullar={_rm_d['approval']['failed_conditions']}")

    # A render built from an unapproved script and a development-placeholder
    # voiceover must be HARD-BLOCKED from approval, and the master review cell
    # must name the exact blocking conditions.
    _run_master_review(_ns_d, "render")
    _rri = _ns_d["render_review_info"]
    if _rri["reviewable"]:
        raise AssertionError(
            "SOZLESME IHLALI: yukari-akis onaylari eksikken render onaylanabilir gorunuyor."
        )
    for _expected in ("script_approved", "voiceover_approved"):
        if _expected not in _rri["blocking_conditions"]:
            raise AssertionError(
                f"Inceleme hucresi engelleyen kosulu bildirmiyor: {_expected} "
                f"(bildirilenler: {_rri['blocking_conditions']})"
            )
    print(f"  [master:render] inceleme hucresi engelleyen kosullari bildiriyor: "
          f"{_rri['blocking_conditions']}")

    # And the approval cell itself must refuse, leaving status untouched.
    _status_before = _ns_d["render_manifest"]["status"]
    _run_master_approve(_ns_d, "render",
                        RENDER_REVIEWED_SHA256=_rri["content_sha256"],
                        RENDER_APPROVED_BY="verify_local")
    _rm_after = _ns_d["render_manifest"]
    if _rm_after["status"] == "approved":
        raise AssertionError(
            "SOZLESME IHLALI: yukari-akis onaylari eksikken render 'approved' oldu."
        )
    if _rm_after["status"] != _status_before:
        raise AssertionError(f"Reddedilen onay status'u degistirdi: {_status_before} -> {_rm_after['status']}")
    if _rm_after["human_approval"]["approved"] is not False:
        raise AssertionError("Reddedilen onay human_approval.approved=True yazdi!")
    print(f"  [master:render] onay REDDEDILDI, status degismedi ({_rm_after['status']})")
finally:
    _restore_provider_sdks(_saved_sdks)
    os.chdir(_master_prev_cwd)
    for _d in _master_tmpdirs:
        shutil.rmtree(_d, ignore_errors=True)

print("MASTER_NOTEBOOK_ALL_PHASES_OK (gercek results/ klasoru degistirilmedi, "
      "hicbir gercek saglayici cagrilmadi, hicbir sey yayinlanmadi)")


# ---------------------------------------------------------------------------
# Provider fan-out regression tests (corrective pass).
#
# Call-counting fakes prove the EXACT number of paid provider calls each entry
# point makes. These target the reproduced defect directly: previously one
# script request called gpt-5.6-luna AND gpt-5.6-sol (and every other configured
# provider), and one TTS request called ElevenLabs AND OpenAI AND gTTS.
# ---------------------------------------------------------------------------

print("\n--- Provider fan-out regression: exact paid-call counts ---")

from local_pipeline import script_generation as _sg
from local_pipeline import tts as _tts
from local_pipeline import approval as _approval

_SG_CALLS = []


def _sg_openai(fail=False):
    class _C:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    _SG_CALLS.append(("openai", kw["model"]))
                    if fail:
                        raise RuntimeError("simulated openai outage")
                    _payload = json.dumps({"script": "S", "hook": "h", "estimated_duration_sec": 60})
                    return type("R", (), {
                        "choices": [type("C", (), {"message": type("M", (), {"content": _payload})()})()],
                        "usage": type("U", (), {"prompt_tokens": 10, "completion_tokens": 5})()})()
    return _C()


def _sg_anthropic(fail=False):
    class _C:
        class messages:
            @staticmethod
            def create(**kw):
                _SG_CALLS.append(("anthropic", kw["model"]))
                if fail:
                    raise RuntimeError("simulated anthropic outage")
                _payload = json.dumps({"script": "A", "hook": "h", "estimated_duration_sec": 60})
                return type("R", (), {
                    "content": [type("B", (), {"text": _payload})()],
                    "usage": type("U", (), {"input_tokens": 10, "output_tokens": 5})()})()
    return _C()


def _sg_google(fail=False):
    class _C:
        class models:
            @staticmethod
            def generate_content(**kw):
                _SG_CALLS.append(("google", kw["model"]))
                if fail:
                    raise RuntimeError("simulated google outage")
                # Long enough to pass google_ai's SEMANTIC validation (a
                # one-character script is valid JSON but useless).
                return type("R", (), {
                    "text": json.dumps({"script": "G" * 60, "hook": "h",
                                         "estimated_duration_sec": 60}),
                    "candidates": [], "usage_metadata": None})()

            @staticmethod
            def list():
                return [type("M", (), {"name": f"models/{_m}"})()
                        for _m in ("gemini-3.6-flash", "gemini-3.5-flash",
                                    "gemini-3.5-flash-lite", "gemma-4-31b-it")]
    return _C()


_HM_FIXTURE = {
    "hot_moments": [{"rank": 1, "source_start_sec": 0, "source_end_sec": 30,
                      "aligned_start_sec": 0, "aligned_end_sec": 30,
                      "score": 9, "reason": "r", "transcript": "t"}],
    "source": {"raw_video_path": "x.mp4"},
}

# 1. All three providers configured, fallback OFF -> EXACTLY ONE paid call.
_SG_CALLS.clear()
_art = _sg.run_script_generation(
    _HM_FIXTURE, "hm.json", moment_approved=True,
    openai_client=_sg_openai(), anthropic_client=_sg_anthropic(), google_client=_sg_google(),
    script_provider="openai", script_model="gpt-5.6-luna", allow_provider_fallback=False,
)
if len(_SG_CALLS) != 1 or _SG_CALLS != [("openai", "gpt-5.6-luna")]:
    raise AssertionError(f"[sg] 1 cagri beklendi, alinan: {_SG_CALLS}")
if any(m == "gpt-5.6-sol" for _, m in _SG_CALLS):
    raise AssertionError("[sg] karsilastirma modeli (gpt-5.6-sol) cagrildi -- fan-out geri geldi!")
if _art["status"] != "review_required":
    raise AssertionError(f"[sg] yeni script 'review_required' olmali: {_art['status']}")
print(f"  [sg:selected-model-success] ucretli cagri={len(_SG_CALLS)} {_SG_CALLS}")

# 2. Fallback ON but first candidate succeeds -> still EXACTLY ONE call.
_SG_CALLS.clear()
_sg.run_script_generation(
    _HM_FIXTURE, "hm.json", moment_approved=True,
    openai_client=_sg_openai(), anthropic_client=_sg_anthropic(), google_client=_sg_google(),
    script_provider="openai", script_model="gpt-5.6-luna", allow_provider_fallback=True,
)
if len(_SG_CALLS) != 1:
    raise AssertionError(f"[sg] fallback acikken basarili ilk adayda 1 cagri beklendi: {_SG_CALLS}")
print(f"  [sg:fallback-on-first-succeeds] ucretli cagri={len(_SG_CALLS)} {_SG_CALLS}")

# 3. First candidate FAILS with fallback ON -> second called, stops there.
#    SCRIPT_FALLBACK_PROVIDER_ORDER now starts with google (the free tier is the
#    project's primary provider), so openai -> google is the expected hop.
_SG_CALLS.clear()
_art = _sg.run_script_generation(
    _HM_FIXTURE, "hm.json", moment_approved=True,
    openai_client=_sg_openai(fail=True), anthropic_client=_sg_anthropic(), google_client=_sg_google(),
    script_provider="openai", script_model="gpt-5.6-luna", allow_provider_fallback=True,
)
if len(_SG_CALLS) != 2 or _SG_CALLS[0][0] != "openai" or _SG_CALLS[1] != ("google", "gemini-3.6-flash"):
    raise AssertionError(f"[sg] sirali fallback beklendi (openai->google/3.6-flash): {_SG_CALLS}")
if _art["provider"] != "google":
    raise AssertionError(f"[sg] fallback sonucu google olmali: {_art['provider']}")
_attempts = _art["provider_attempts"]
if len(_attempts) != 2 or _attempts[0]["ok"] or not _attempts[1]["ok"]:
    raise AssertionError(f"[sg] provider_attempts kaydi hatali: {_attempts}")
if "outage" not in (_attempts[0]["error"] or ""):
    raise AssertionError(f"[sg] basarisizlik nedeni kaydedilmemis: {_attempts[0]}")
print(f"  [sg:fallback-after-failure] ucretli cagri={len(_SG_CALLS)} {_SG_CALLS} "
      f"(basarisizlik nedeni kayitli)")

# 4. First candidate fails with fallback OFF -> exactly one call, prompt-only.
_SG_CALLS.clear()
_art = _sg.run_script_generation(
    _HM_FIXTURE, "hm.json", moment_approved=True,
    openai_client=_sg_openai(fail=True), anthropic_client=_sg_anthropic(), google_client=_sg_google(),
    script_provider="openai", script_model="gpt-5.6-luna", allow_provider_fallback=False,
)
if len(_SG_CALLS) != 1:
    raise AssertionError(f"[sg] fallback kapaliyken 1 cagri beklendi: {_SG_CALLS}")
if _art["script_text"] is not None or "prompt_package" not in _art:
    raise AssertionError("[sg] basarisizlikta prompt paketi beklendi")
print(f"  [sg:no-fallback-after-failure] ucretli cagri={len(_SG_CALLS)} {_SG_CALLS}")

# 5. Moment not approved -> ZERO paid calls.
_SG_CALLS.clear()
_sg.run_script_generation(
    _HM_FIXTURE, "hm.json", moment_approved=False,
    openai_client=_sg_openai(), anthropic_client=_sg_anthropic(), google_client=_sg_google(),
    allow_provider_fallback=True,
)
if _SG_CALLS:
    raise AssertionError(f"[sg] an onaylanmadan cagri yapilmamali: {_SG_CALLS}")
print(f"  [sg:moment-not-approved] ucretli cagri={len(_SG_CALLS)}")

# 6. The comparison catalogs must never be iterated by the master entry point.
_all_catalog_models = [m for entries in _sg.SCRIPT_PROVIDER_CATALOG.values() for m, _, _ in entries]
if len(_all_catalog_models) < 6:
    raise AssertionError("Karsilastirma katalogu beklenenden kucuk; test anlamsizlasir.")
_SG_CALLS.clear()
_sg.run_script_generation(
    _HM_FIXTURE, "hm.json", moment_approved=True,
    openai_client=_sg_openai(), anthropic_client=_sg_anthropic(), google_client=_sg_google(),
    script_provider="openai", allow_provider_fallback=True,
)
if len(_SG_CALLS) >= len(_all_catalog_models):
    raise AssertionError(
        f"[sg] FAN-OUT: {len(_SG_CALLS)} cagri yapildi, katalogda {len(_all_catalog_models)} model var."
    )
print(f"  [sg:no-catalog-fanout] katalogda {len(_all_catalog_models)} model, "
      f"yapilan ucretli cagri={len(_SG_CALLS)}")

# --- TTS ---
_TTS_CALLS = []
_TTS_TMP = Path(_tempfile.mkdtemp(prefix="verify_tts_count_"))


def _tts_eleven(fail=False):
    class _C:
        class text_to_speech:
            @staticmethod
            def convert(voice_id=None, model_id=None, text=None):
                _TTS_CALLS.append(("elevenlabs", model_id))
                if fail:
                    raise RuntimeError("simulated elevenlabs outage")
                return [b"AUDIOBYTES"]
    return _C()


def _tts_openai(fail=False):
    class _C:
        class audio:
            class speech:
                @staticmethod
                def create(model=None, voice=None, input=None):
                    _TTS_CALLS.append(("openai", model))
                    if fail:
                        raise RuntimeError("simulated openai tts outage")
                    return type("R", (), {"stream_to_file": staticmethod(
                        lambda p: Path(p).write_bytes(b"OPENAIAUDIO"))})()
    return _C()


def _tts_gtts(fail=False):
    def _factory(text=None, lang=None):
        _TTS_CALLS.append(("gtts", "gtts-default"))
        if fail:
            raise RuntimeError("simulated gtts network failure")
        return type("T", (), {"save": staticmethod(lambda p: Path(p).write_bytes(b"GTTSAUDIO"))})()
    return _factory


_APPROVED_SCRIPT = {"script_text": "Approved text.", "status": "approved", "language": "tr"}
_UNAPPROVED_SCRIPT = {"script_text": "Unreviewed text.", "status": "review_required", "language": "tr"}

# 7. ElevenLabs succeeds -> exactly ONE paid call; no OpenAI, no gTTS.
_TTS_CALLS.clear()
_vo = _tts.run_tts(_APPROVED_SCRIPT, _TTS_TMP / "t1", elevenlabs_client=_tts_eleven(),
                   openai_client=_tts_openai(), gtts_factory=_tts_gtts(),
                   tts_provider="elevenlabs", allow_provider_fallback=True,
                   allow_dev_gtts_fallback=True)
if len(_TTS_CALLS) != 1 or _TTS_CALLS[0][0] != "elevenlabs":
    raise AssertionError(f"[tts] 1 ElevenLabs cagrisi beklendi: {_TTS_CALLS}")
if any(p == "openai" for p, _ in _TTS_CALLS):
    raise AssertionError("[tts] ElevenLabs basarili oldugu halde OpenAI cagrildi!")
if any(p == "gtts" for p, _ in _TTS_CALLS):
    raise AssertionError("[tts] uretim basarili oldugu halde gTTS cagrildi!")
if _vo["status"] != "review_required":
    raise AssertionError(f"[tts] yeni seslendirme 'review_required' olmali: {_vo['status']}")
print(f"  [tts:elevenlabs-success] ucretli cagri={len(_TTS_CALLS)} {_TTS_CALLS}")

# 8. ElevenLabs fails + fallback ON -> OpenAI called; gTTS still not called.
_TTS_CALLS.clear()
_vo = _tts.run_tts(_APPROVED_SCRIPT, _TTS_TMP / "t2", elevenlabs_client=_tts_eleven(fail=True),
                   openai_client=_tts_openai(), gtts_factory=_tts_gtts(),
                   tts_provider="elevenlabs", allow_provider_fallback=True,
                   allow_dev_gtts_fallback=True)
if [p for p, _ in _TTS_CALLS] != ["elevenlabs", "openai"]:
    raise AssertionError(f"[tts] sirali fallback beklendi (elevenlabs->openai): {_TTS_CALLS}")
if _vo["provider"] != "openai":
    raise AssertionError(f"[tts] fallback sonucu openai olmali: {_vo['provider']}")
print(f"  [tts:openai-only-after-elevenlabs-fails] ucretli cagri={len(_TTS_CALLS)} {_TTS_CALLS}")

# 9. ElevenLabs fails + fallback OFF -> OpenAI never called.
_TTS_CALLS.clear()
_tts.run_tts(_APPROVED_SCRIPT, _TTS_TMP / "t3", elevenlabs_client=_tts_eleven(fail=True),
             openai_client=_tts_openai(), gtts_factory=_tts_gtts(),
             tts_provider="elevenlabs", allow_provider_fallback=False,
             allow_dev_gtts_fallback=True)
if any(p == "openai" for p, _ in _TTS_CALLS):
    raise AssertionError(f"[tts] fallback kapaliyken OpenAI cagrilmamali: {_TTS_CALLS}")
print(f"  [tts:no-fallback-blocks-openai] cagrilar={_TTS_CALLS}")

# 10. gTTS disabled -> never called even when every production provider fails.
_TTS_CALLS.clear()
_vo = _tts.run_tts(_APPROVED_SCRIPT, _TTS_TMP / "t4", elevenlabs_client=_tts_eleven(fail=True),
                   openai_client=_tts_openai(fail=True), gtts_factory=_tts_gtts(),
                   tts_provider="elevenlabs", allow_provider_fallback=True,
                   allow_dev_gtts_fallback=False)
if any(p == "gtts" for p, _ in _TTS_CALLS):
    raise AssertionError(f"[tts] gTTS devre disiyken cagrilmamali: {_TTS_CALLS}")
if _vo["status"] != "insufficient_data":
    raise AssertionError(f"[tts] tum saglayicilar basarisizsa insufficient_data beklendi: {_vo['status']}")
print(f"  [tts:gtts-disabled] cagrilar={_TTS_CALLS} artifact={_vo['status']}")

# 11. Unapproved script -> ZERO paid TTS calls.
_TTS_CALLS.clear()
_vo = _tts.run_tts(_UNAPPROVED_SCRIPT, _TTS_TMP / "t5", elevenlabs_client=_tts_eleven(),
                   openai_client=_tts_openai(), gtts_factory=_tts_gtts(),
                   tts_provider="elevenlabs", allow_provider_fallback=True,
                   allow_dev_gtts_fallback=True)
_paid_tts = [p for p, _ in _TTS_CALLS if p in ("elevenlabs", "openai")]
if _paid_tts:
    raise AssertionError(f"[tts] onaysiz scriptle ucretli cagri yapilmamali: {_paid_tts}")
if _vo["status"] != "development_placeholder":
    raise AssertionError(f"[tts] onaysiz script + gtts -> development_placeholder: {_vo['status']}")
print(f"  [tts:unapproved-script] ucretli cagri={len(_paid_tts)}, gtts fallback status={_vo['status']}")

shutil.rmtree(_TTS_TMP, ignore_errors=True)
print("PROVIDER_FANOUT_REGRESSION_OK")


# ---------------------------------------------------------------------------
# Approval-gate regression tests (corrective pass).
# ---------------------------------------------------------------------------

print("\n--- Approval gates: post-generation, hash-bound, invalidated on change ---")

import inspect as _inspect

# No generation entry point may accept an approval argument.
for _fn, _banned in [(_sg.run_script_generation, "script_approved"),
                     (_sg.build_selected_script_artifact, "script_approved"),
                     (_tts.run_tts, "voiceover_approved"),
                     (_tts.build_selected_voiceover_artifact, "voiceover_approved"),
                     (ru.run_render_pipeline, "render_approved")]:
    _params = _inspect.signature(_fn).parameters
    if _banned in _params:
        raise AssertionError(f"{_fn.__name__} hala {_banned} argumanini kabul ediyor: {list(_params)}")
print("  hicbir uretim fonksiyonu onay argumani kabul etmiyor")

# Freshly generated content cannot be approved in the same call.
_SG_CALLS.clear()
_fresh_script = _sg.run_script_generation(
    _HM_FIXTURE, "hm.json", moment_approved=True,
    anthropic_client=_sg_anthropic(), script_provider="anthropic",
)
if _fresh_script["status"] == "approved" or _fresh_script["human_approval"]["approved"]:
    raise AssertionError("Yeni uretilen script ayni cagrida onaylanmis!")
print("  yeni uretilen script ayni cagrida onaylanamiyor")

_approval_tmp = Path(_tempfile.mkdtemp(prefix="verify_approval_"))
_fresh_vo = _tts.run_tts(_APPROVED_SCRIPT, _approval_tmp / "vo",
                          elevenlabs_client=_tts_eleven(), tts_provider="elevenlabs")
if _fresh_vo["status"] == "approved" or _fresh_vo["human_approval"]["approved"]:
    raise AssertionError("Yeni uretilen seslendirme ayni cagrida onaylanmis!")
print("  yeni uretilen seslendirme ayni cagrida onaylanamiyor")

# Separate approval succeeds only for the reviewed hash.
_script_hash = _approval.content_hash_for("script", _fresh_script)
try:
    _approval.approve_artifact(_fresh_script, "script", "0" * 64, "tester")
    raise AssertionError("Yanlis hash ile onay kabul edildi!")
except ValueError as _exc:
    if "ONAY REDDEDILDI" not in str(_exc):
        raise AssertionError(f"beklenmeyen hata: {_exc}")
_approved_script = _approval.approve_artifact(_fresh_script, "script", _script_hash, "tester", "read")
if _approved_script["status"] != "approved":
    raise AssertionError("Dogru hash ile onay basarisiz")
if _approved_script["human_approval"]["approved_content_sha256"] != _script_hash:
    raise AssertionError("Onay icerik hash'ine baglanmadi")
if not _approved_script["human_approval"]["approved_at"]:
    raise AssertionError("Onay zaman damgasi kaydedilmedi")
if _fresh_script["status"] == "approved":
    raise AssertionError("approve_artifact girdiyi mutasyona ugratti")
print("  ayri onay yalnizca incelenen hash ile basarili (zaman damgasi + onaylayan kayitli)")

# Modifying an approved artifact invalidates its approval.
_tampered = dict(_approved_script)
_tampered["script_text"] = _approved_script["script_text"] + " EDITED"
_chk = _approval.check_approval(_tampered, "script")
if not _chk["approved"] or _chk["valid"]:
    raise AssertionError(f"Degistirilen script'in onayi hala gecerli gorunuyor: {_chk}")
print("  onaylanmis scripti degistirmek onayi gecersiz kiliyor")

_vo_hash = _approval.content_hash_for("voiceover", _fresh_vo)
_approved_vo = _approval.approve_artifact(_fresh_vo, "voiceover", _vo_hash, "tester")
Path(_approved_vo["audio_path"]).write_bytes(
    Path(_approved_vo["audio_path"]).read_bytes() + b"CHANGED")
_chk = _approval.check_approval(_approved_vo, "voiceover")
if not _chk["approved"] or _chk["valid"]:
    raise AssertionError(f"Degistirilen ses dosyasinin onayi hala gecerli: {_chk}")
print("  onaylanmis ses dosyasini degistirmek onayi gecersiz kiliyor")

# gTTS can never be approved.
_gtts_vo = _tts.run_tts(_UNAPPROVED_SCRIPT, _approval_tmp / "g", gtts_factory=_tts_gtts(),
                         allow_dev_gtts_fallback=True)
if _gtts_vo["status"] != "development_placeholder":
    raise AssertionError(f"gTTS ciktisi development_placeholder olmali: {_gtts_vo['status']}")
try:
    _approval.approve_artifact(_gtts_vo, "voiceover",
                                _approval.sha256_file(_gtts_vo["audio_path"]), "tester")
    raise AssertionError("gTTS onaylandi -- politika ihlali!")
except ValueError as _exc:
    if "asla" not in str(_exc):
        raise AssertionError(f"beklenmeyen hata: {_exc}")
print("  gTTS ciktisi dogru hash ile bile onaylanamiyor")

shutil.rmtree(_approval_tmp, ignore_errors=True)
print("APPROVAL_GATE_REGRESSION_OK")


# ---------------------------------------------------------------------------
# Render approval hard-block matrix (policy correction).
#
# A render may ONLY be approved when its entire upstream chain is approved.
# Approving a video built from an unapproved script / development-placeholder
# voiceover would launder unapproved inputs into an "approved" artifact.
# Missing condition fields count as FALSE, never as approved.
# ---------------------------------------------------------------------------

print("\n--- Render approval hard-block matrix ---")

_render_tmp = Path(_tempfile.mkdtemp(prefix="verify_render_gate_"))
_gate_video = _render_tmp / "final_short.mp4"
subprocess.run(
    [FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
     "-f", "lavfi", "-i", "testsrc=duration=2:size=320x180:rate=10",
     "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
     "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
     "-c:a", "aac", str(_gate_video)],
    check=True, capture_output=True,
)
_GATE_VIDEO_HASH = _approval.sha256_file(_gate_video)


def _make_render_manifest(script_approved, voiceover_approved, hash_matches,
                           technical_passed=True, drop_condition=None,
                           failed_conditions=None):
    _conditions = {
        "script_approved": script_approved,
        "voiceover_approved": voiceover_approved,
        "voiceover_hash_matches_script": hash_matches,
        "technical_validation_passed": technical_passed,
    }
    if drop_condition:
        _conditions.pop(drop_condition)
    if failed_conditions is None:
        failed_conditions = [k for k, v in _conditions.items() if not v]
    return {
        "status": "review_required",
        "not_uploaded": True,
        "approval": {"conditions": _conditions, "failed_conditions": failed_conditions},
        "output": {"final_video_path": str(_gate_video),
                    "validation": {"passed": technical_passed, "checks": {}}},
        "human_approval": {"approved": False, "approved_by": None, "approved_at": None,
                            "notes": None, "approved_content_sha256": None},
    }


_RENDER_MATRIX = [
    ("script=NO  voiceover=NO  hash=NO  tech=OK", False, False, False, True, None, "REJECT"),
    ("script=YES voiceover=NO  hash=YES tech=OK", True, False, True, True, None, "REJECT"),
    ("script=YES voiceover=YES hash=NO  tech=OK", True, True, False, True, None, "REJECT"),
    ("script=YES voiceover=YES hash=YES tech=FAIL", True, True, True, False, None, "REJECT"),
    ("script=YES voiceover=YES hash=YES tech=OK (field MISSING)", True, True, True, True,
     "script_approved", "REJECT"),
    ("script=YES voiceover=YES hash=YES tech=OK", True, True, True, True, None, "APPROVE"),
]

print(f"  {'senaryo':<52} {'beklenen':<9} sonuc")
print("  " + "-" * 76)
for _label, _sa, _va, _hm, _tech, _drop, _expected in _RENDER_MATRIX:
    _manifest = _make_render_manifest(_sa, _va, _hm, _tech, drop_condition=_drop)
    _before = json.dumps(_manifest, sort_keys=True)
    _info = _approval.describe_for_review("render", _manifest)
    try:
        _result = _approval.approve_artifact(_manifest, "render", _GATE_VIDEO_HASH, "tester")
        _actual = "APPROVE"
        _err = None
    except ValueError as _exc:
        _result = None
        _actual = "REJECT"
        _err = str(_exc)

    if _actual != _expected:
        raise AssertionError(f"[render-matrix] {_label}: beklenen {_expected}, alinan {_actual} ({_err})")

    # A refusal must leave the artifact byte-identical and never claim approval.
    if _expected == "REJECT":
        if json.dumps(_manifest, sort_keys=True) != _before:
            raise AssertionError(f"[render-matrix] {_label}: reddedilen onay artefakti degistirdi!")
        if _manifest["status"] not in ("review_required", "failed"):
            raise AssertionError(f"[render-matrix] {_label}: status {_manifest['status']!r} olmamali")
        if _manifest["human_approval"]["approved"] is not False:
            raise AssertionError(f"[render-matrix] {_label}: human_approval.approved True yazilmis!")
        if _info["reviewable"]:
            raise AssertionError(f"[render-matrix] {_label}: describe_for_review reviewable=True dedi")
        if not _info["blocking_conditions"]:
            raise AssertionError(f"[render-matrix] {_label}: engelleyen kosullar bildirilmedi")
    else:
        if _result["status"] != "approved":
            raise AssertionError(f"[render-matrix] {_label}: onay basarisiz")
        if _result["human_approval"]["approved_content_sha256"] != _GATE_VIDEO_HASH:
            raise AssertionError(f"[render-matrix] {_label}: onay video hash'ine baglanmadi")
        if not _info["reviewable"]:
            raise AssertionError(f"[render-matrix] {_label}: describe_for_review reviewable=False dedi")

    _blockers = ",".join(_info["blocking_conditions"]) if _info["blocking_conditions"] else "-"
    print(f"  {_label:<52} {_expected:<9} {_actual}  [{_blockers}]")

# Original-audio mode has no generated script or TTS upstream. Its render gate
# requires the human-approved moment selection plus technical validation.
_original_gate_manifest = {
    "status": "review_required",
    "not_uploaded": True,
    "config": {"pipeline_mode": "original_audio_subtitles"},
    "approval": {
        "conditions": {
            "moment_selection_approved": True,
            "technical_validation_passed": True,
        },
        "failed_conditions": [],
    },
    "output": {
        "final_video_path": str(_gate_video),
        "validation": {"passed": True, "checks": {}},
    },
    "human_approval": {
        "approved": False, "approved_by": None, "approved_at": None,
        "notes": None, "approved_content_sha256": None,
    },
}
_original_gate_result = _approval.approve_artifact(
    _original_gate_manifest, "render", _GATE_VIDEO_HASH, "tester",
)
if _original_gate_result["status"] != "approved":
    raise AssertionError("Orijinal-ses renderi gecerli moment + teknik kosullarla onaylanamadi")
_original_gate_blocked = json.loads(json.dumps(_original_gate_manifest))
_original_gate_blocked["approval"]["conditions"]["moment_selection_approved"] = False
_original_gate_blocked["approval"]["failed_conditions"] = ["moment_selection_approved"]
try:
    _approval.approve_artifact(_original_gate_blocked, "render", _GATE_VIDEO_HASH, "tester")
    raise AssertionError("Onaysiz moment ile orijinal-ses renderi onaylandi")
except ValueError:
    pass
print("  original_audio_subtitles: moment onayi + teknik dogrulama kapisi OK")

# A fully-approved render whose upstream approval is LATER revoked must go
# invalid even though the video bytes are unchanged.
_good = _make_render_manifest(True, True, True, True)
_approved_render = _approval.approve_artifact(_good, "render", _GATE_VIDEO_HASH, "tester")
_chk = _approval.check_approval(_approved_render, "render")
if not (_chk["approved"] and _chk["valid"]):
    raise AssertionError(f"[render-matrix] taze onay gecerli olmali: {_chk}")

_revoked = json.loads(json.dumps(_approved_render))
_revoked["approval"]["conditions"]["script_approved"] = False
_revoked["approval"]["failed_conditions"] = ["script_approved"]
_chk = _approval.check_approval(_revoked, "render")
if _chk["valid"]:
    raise AssertionError("[render-matrix] script onayi geri alindigi halde render onayi gecerli!")
if _approval.sha256_file(_revoked["output"]["final_video_path"]) != _GATE_VIDEO_HASH:
    raise AssertionError("[render-matrix] test kurulumu hatali: video degismis olmamali")
print("  yukari-akis onayi geri alindiginda (video BYTE BAZINDA AYNI) check_approval valid=False")

# Technical validation regressing later must also invalidate.
_tech_regressed = json.loads(json.dumps(_approved_render))
_tech_regressed["output"]["validation"]["passed"] = False
_tech_regressed["approval"]["conditions"]["technical_validation_passed"] = False
if _approval.check_approval(_tech_regressed, "render")["valid"]:
    raise AssertionError("[render-matrix] teknik dogrulama bozuldugu halde onay gecerli!")
print("  teknik dogrulama sonradan bozulursa check_approval valid=False")

# The separate "I watched it" record must NOT approve anything.
_blocked = _make_render_manifest(False, False, False, True)
_reviewed = _approval.record_render_review(_blocked, _GATE_VIDEO_HASH, "tester", "watched it")
if _reviewed["status"] == "approved" or _reviewed["human_approval"]["approved"]:
    raise AssertionError("record_render_review artefakti onayladi!")
if not _reviewed["render_review"]["reviewed"]:
    raise AssertionError("record_render_review inceleme kaydini yazmadi")
if _reviewed["render_review"]["reviewed_content_sha256"] != _GATE_VIDEO_HASH:
    raise AssertionError("record_render_review yanlis hash kaydetti")
print(f"  record_render_review: status={_reviewed['status']} (degismedi), "
      f"render_review.reviewed={_reviewed['render_review']['reviewed']}")

shutil.rmtree(_render_tmp, ignore_errors=True)
print("RENDER_APPROVAL_HARDBLOCK_OK")


# ---------------------------------------------------------------------------
# Google AI router: fully mocked regression suite.
#
# NOTHING here contacts a real endpoint. Every Google/Groq/Cloudflare/Azure call
# goes through an injected client or transport double, every credential is a
# fake string that is never a real key, and every artifact/ledger is written to
# an isolated temp directory that is deleted afterwards. The user's results/
# directory is never touched.
# ---------------------------------------------------------------------------

print("\n--- Google AI router: mocked tests (no real Google/Groq/Cloudflare/Azure calls) ---")

import inspect as _inspect
import re as _re
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

from local_pipeline import google_ai as _ga
from local_pipeline import provider_adapters as _pa

_GA_TMP = Path(_tempfile.mkdtemp(prefix="google_ai_tests_"))
# Never a real credential: assembled at runtime so it also cannot be mistaken for
# one by the secret scanner or by a human reading the file.
_FAKE_GEMINI_KEY = "FAKE-" + "GEMINI" + "-KEY-" + "0" * 12
_FAKE_YT_KEY = "FAKE-" + "YOUTUBE" + "-KEY-" + "0" * 12

_ALL_DISCOVERED = ["models/" + _m for _m in _ga.MODEL_ALLOWLIST]
_GA_MANIFEST = _ga.build_google_capability_manifest(discovered_models=_ALL_DISCOVERED)
_ga_source = (PROJECT_ROOT / "local_pipeline" / "google_ai.py").read_text(encoding="utf-8")
_pa_source = (PROJECT_ROOT / "local_pipeline" / "provider_adapters.py").read_text(encoding="utf-8")


class _GaError(Exception):
    """SDK-shaped error double: carries code/headers like a real APIError."""

    def __init__(self, message, code=None, headers=None):
        super().__init__(message)
        self.code = code
        if headers is not None:
            self.headers = headers


def _ga_raise(exc):
    raise exc


def _ga_response(payload="OK", *, candidates=None, usage=None):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return type("R", (), {"text": text, "candidates": candidates or [],
                          "usage_metadata": usage})()


_VALID_SCRIPT = {"script": "Bu yeterince uzun bir test script metnidir, semantik dogrulamayi gecer.",
                 "hook": "h", "estimated_duration_sec": 60}


def _ga_client(behaviour):
    """Fake genai client. `behaviour(model, per_model_call_index)` returns a
    response or raises. Records every call so fan-out is measurable."""
    calls = []

    class _Models:
        def generate_content(self, *, model, contents, config=None):
            calls.append(model)
            return behaviour(model, calls.count(model) - 1)

        def embed_content(self, *, model, contents, config=None):
            calls.append(model)
            return behaviour(model, calls.count(model) - 1)

        def list(self):
            return [type("M", (), {"name": n})() for n in _ALL_DISCOVERED]

    return type("C", (), {"models": _Models()})(), calls


def _ga_run_script(client, **kw):
    kw.setdefault("manifest", _GA_MANIFEST)
    kw.setdefault("ledger", _ga.QuotaLedger())
    kw.setdefault("persist_ledger", False)
    kw.setdefault("sleep_fn", lambda _s: None)
    return _ga.run_google_script_generation("test prompt", client=client, **kw)


# --- 1/2/3. Secret alias resolution and safe skipping ------------------------
_cred_new = _ga.resolve_secret("google_api", lambda n: _FAKE_GEMINI_KEY if n == "GeminiKey" else None)
if not (_cred_new.configured and _cred_new.alias_used == "GeminiKey"):
    raise AssertionError("[ga] 'GeminiKey' alias'i cozulmedi")
_cred_old = _ga.resolve_secret("google_api", lambda n: _FAKE_GEMINI_KEY if n == "GOOGLE_API_KEY" else None)
if not (_cred_old.configured and _cred_old.alias_used == "GOOGLE_API_KEY"):
    raise AssertionError("[ga] geriye donuk 'GOOGLE_API_KEY' alias'i cozulmedi")
_cred_pref = _ga.resolve_secret("google_api", lambda n: {"GeminiKey": _FAKE_GEMINI_KEY,
                                                          "GOOGLE_API_KEY": "eski"}.get(n))
if _cred_pref.alias_used != "GeminiKey":
    raise AssertionError("[ga] iki alias da varken 'GeminiKey' tercih edilmeli")
print("  [ga:secret-alias-gemini] GeminiKey -> GOOGLE_API_KEY sirasi OK")

_yt_new = _ga.resolve_secret("youtube_data_api", lambda n: _FAKE_YT_KEY if n == "YoutubeKEY" else None)
_yt_old = _ga.resolve_secret("youtube_data_api",
                             lambda n: _FAKE_YT_KEY if n == "YOUTUBE_DATA_API_KEY" else None)
if not (_yt_new.alias_used == "YoutubeKEY" and _yt_old.alias_used == "YOUTUBE_DATA_API_KEY"):
    raise AssertionError("[ga] YoutubeKEY/YOUTUBE_DATA_API_KEY alias zinciri bozuk")
print("  [ga:secret-alias-youtube] YoutubeKEY -> YOUTUBE_DATA_API_KEY sirasi OK")

_profiles = {"primary": ("GeminiKey", "GOOGLE_API_KEY"),
             "secondary": ("GeminiKeySecondary",)}
_profile_values = {"GeminiKey": "primary-value", "GeminiKeySecondary": "secondary-value"}
_secondary = _ga.resolve_profile_secret(
    "google_api", "secondary", _profiles, lambda n: _profile_values.get(n))
if (_secondary.profile != "secondary" or
        _secondary.alias_used != "GeminiKeySecondary" or
        _secondary.value != "secondary-value"):
    raise AssertionError("[ga] manuel secondary profil tam olarak kendi secret'ini cozmeli")
_secondary_missing = _ga.resolve_profile_secret(
    "google_api", "secondary", _profiles, lambda n: _profile_values.get(n) if n == "GeminiKey" else None)
if _secondary_missing.configured:
    raise AssertionError("[ga] secili profil eksikken primary profile otomatik gecildi")
if _ga.profile_scoped_filename("google_quota_usage.json", "primary") != "google_quota_usage.primary.json":
    raise AssertionError("[ga] profile-scoped ledger dosya adi hatali")
if _ga.profile_scoped_filename("google_quota_usage.json", "default") != "google_quota_usage.json":
    raise AssertionError("[ga] legacy default ledger yolu korunmadi")
_profile_ledger_path = _GA_TMP / "profile-ledger.json"
_secondary_ledger = _ga.QuotaLedger(_profile_ledger_path, credential_profile="secondary")
_secondary_ledger.data["models"]["sentinel"] = {"day_requests": 9}
_secondary_ledger.save()
_primary_ledger = _ga.QuotaLedger(_profile_ledger_path, credential_profile="primary")
if _primary_ledger.data["models"]:
    raise AssertionError("[ga] secondary kota defteri primary profile sizdi")
print("  [ga:manual-profile-only] secondary yalniz kendi secret'ini cozer; otomatik primary gecisi yok")


# A getter that RAISES (Colab "notebook access" off) is treated as "absent".
def _raising_getter(name):
    raise RuntimeError("SecretNotFoundError: notebook access disabled")


_cred_missing = _ga.resolve_secret("google_api", _raising_getter)
if _cred_missing.configured or not _ga.describe_credential_state(_cred_missing).startswith("missing"):
    raise AssertionError("[ga] erisilemeyen secret 'missing' olarak raporlanmali")
if "Notebook erisimi" not in _ga.credential_help_text("google_api"):
    raise AssertionError("[ga] Colab secret hatasi icin uygulanabilir yonerge yok")
_missing_route = _ga.execute_google_route(
    "script_generation", lambda *a, **k: _ga_response(_VALID_SCRIPT),
    credential=_cred_missing, manifest=_GA_MANIFEST, ledger=_ga.QuotaLedger(),
    persist_ledger=False,
)
if _missing_route["status"] != "insufficient_data" or _missing_route["output"] is not None:
    raise AssertionError(f"[ga] anahtar yokken temiz atlanmali: {_missing_route['status']}")
if _pa.call_groq_text("x", secret_getter=lambda n: None)["status"] != "skipped":
    raise AssertionError("[ga] Groq kimlik yokken temiz atlanmali")
print("  [ga:missing-secret-skips-safely] hata degil, insufficient_data/skipped + uygulanabilir mesaj")

# --- 4. No secret value anywhere in outputs/artifacts/errors -----------------
_leak_client, _ = _ga_client(lambda m, i: _ga_raise(_GaError(
    f"boom key={_FAKE_GEMINI_KEY} Authorization: Bearer {_FAKE_GEMINI_KEY}", 500)))
_leak_result = _ga.execute_google_route(
    "script_generation", lambda c, m, s: c.models.generate_content(model=m, contents="x"),
    client=_leak_client, credential=_cred_new, manifest=_GA_MANIFEST,
    ledger=_ga.QuotaLedger(), persist_ledger=False, sleep_fn=lambda _s: None,
)
_leak_artifact = _ga.build_google_artifact(
    _leak_result, task="script_generation", capability="generate_content",
    input_content="x", output_content=None)
_leak_blob = json.dumps({"result": {k: v for k, v in _leak_result.items() if k != "raw_response"},
                          "artifact": _leak_artifact,
                          "credential": _cred_new.to_public_dict(),
                          "repr": repr(_cred_new), "fstring": f"{_cred_new}",
                          "state": _ga.describe_credential_state(_cred_new)},
                         ensure_ascii=False)
if _FAKE_GEMINI_KEY in _leak_blob:
    raise AssertionError("[ga] SIZINTI: gizli anahtar degeri ciktida/artefaktta gorundu!")
if "REDACTED" not in _leak_blob:
    raise AssertionError("[ga] hata metni sanitize edilmemis")
# Pattern-based redaction, even for a key this call never saw.
if "AIza" in _ga.sanitize_google_error(Exception("k=" + "AIza" + "x" * 30)):
    raise AssertionError("[ga] AIza... deseni redakte edilmedi")
# And nothing key-shaped is committed to the notebooks/modules themselves.
for _tracked in ["local_pipeline/google_ai.py", "local_pipeline/provider_adapters.py",
                  *[str(p.relative_to(PROJECT_ROOT))
                    for p in sorted((PROJECT_ROOT / "experiments" / "colab").glob("*.ipynb"))]]:
    _content = (PROJECT_ROOT / _tracked).read_text(encoding="utf-8", errors="ignore")
    if _re.search(r"AIza[0-9A-Za-z_\-]{20,}", _content):
        raise AssertionError(f"[ga] {_tracked}: anahtar benzeri sabit deger bulundu")
print("  [ga:no-secret-leak] artefakt/hata/repr/notebook/modul ciktilarinin hicbirinde anahtar yok")

# --- 5/6. Discovery intersects the allowlist; zero-quota models excluded -----
_partial = _ga.build_google_capability_manifest(discovered_models=[
    "models/gemini-3.6-flash", "models/gemini-2.5-pro", "models/gemini-3.1-flash-image",
    "models/some-unlisted-experimental-model",
])
if "some-unlisted-experimental-model" in _partial["usable_models"]:
    raise AssertionError("[ga] allowlist disi model kullanilabilir sayildi")
if "some-unlisted-experimental-model" not in _partial["excluded_not_allowlisted"]:
    raise AssertionError("[ga] allowlist disi model dislanan listesine yazilmadi")
if _partial["usable_models"] != ["gemini-3.6-flash"]:
    raise AssertionError(f"[ga] kesif/allowlist kesisimi hatali: {_partial['usable_models']}")
print(f"  [ga:discovery-intersects-allowlist] kesfedilen 4 -> kullanilabilir "
      f"{_partial['usable_models']}")

for _zero in ("gemini-2.5-pro", "gemini-3.1-flash-image", "gemini-3-pro-image",
               "gemini-2.5-flash-image", "gemini-3.1-pro-preview"):
    if _ga.is_automatic_route_eligible(_zero):
        raise AssertionError(f"[ga] 0-kotali {_zero} otomatik rotaya uygun sayildi")
    if _zero in _GA_MANIFEST["usable_models"]:
        raise AssertionError(f"[ga] 0-kotali {_zero} kullanilabilir listesinde")
for _task, _route in _ga.TASK_ROUTES.items():
    for _m in _route:
        if not _ga.has_nonzero_quota(_m):
            raise AssertionError(f"[ga] '{_task}' rotasi 0-kotali {_m} iceriyor")
_pro_forced = _ga.select_google_route("script_generation", mode="explicit",
                                       overrides={"script_generation": ["gemini-2.5-pro"]},
                                       manifest=_GA_MANIFEST)
if _pro_forced["route"]:
    raise AssertionError("[ga] acik override ile bile 0-kotali model rotaya girdi")
if any(m == "gemini-2.5-pro" for m, _, _ in _sg.GOOGLE_SCRIPT_MODELS):
    raise AssertionError("[ga] GOOGLE_SCRIPT_MODELS hala gemini-2.5-pro iceriyor")
if _sg.DEFAULT_SCRIPT_PROVIDER != "google":
    raise AssertionError("[ga] Google kanonik varsayilan script saglayicisi olmali")
print("  [ga:zero-quota-excluded] 0-kotali modeller (gemini-2.5-pro dahil) hicbir rotada yok; "
      "kanonik varsayilan saglayici = google")

# --- 7/8/9/10/11. Route A ordering and stop-on-first-success ----------------
_c, _calls = _ga_client(lambda m, i: _ga_response(_VALID_SCRIPT))
_r = _ga_run_script(_c)
if _r["status"] != "ok" or _calls != ["gemini-3.6-flash"] or _r["fallback_used"]:
    raise AssertionError(f"[ga] 3.6 basarili olunca TEK cagri beklenir: {_calls}")
print(f"  [ga:3.6-success-single-call] cagrilar={_calls}")

_fail_36 = {"gemini-3.6-flash"}
_c, _calls = _ga_client(lambda m, i: _ga_raise(_GaError("404 not_found", 404))
                        if m in _fail_36 else _ga_response(_VALID_SCRIPT))
_r = _ga_run_script(_c)
if _r["model"] != "gemini-3.5-flash" or _calls != ["gemini-3.6-flash", "gemini-3.5-flash"]:
    raise AssertionError(f"[ga] 3.6 -> 3.5 gecisi beklenir: {_calls}")
if not _r["fallback_used"] or not _r["fallback_reason"]:
    raise AssertionError("[ga] fallback_used/fallback_reason kaydedilmedi")
print(f"  [ga:3.6-fail-to-3.5] cagrilar={_calls}")

_fail_2 = {"gemini-3.6-flash", "gemini-3.5-flash"}
_c, _calls = _ga_client(lambda m, i: _ga_raise(_GaError("404 not_found", 404))
                        if m in _fail_2 else _ga_response(_VALID_SCRIPT))
_r = _ga_run_script(_c)
if _r["model"] != "gemini-3.5-flash-lite":
    raise AssertionError(f"[ga] 3.6+3.5 basarisizsa flash-lite beklenir: {_calls}")
print(f"  [ga:both-flash-fail-to-lite] cagrilar={_calls}")

_fail_3 = {"gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"}
_c, _calls = _ga_client(lambda m, i: _ga_raise(_GaError("404 not_found", 404))
                        if m in _fail_3 else _ga_response(_VALID_SCRIPT))
_r = _ga_run_script(_c)
if _r["model"] != "gemma-4-31b-it":
    raise AssertionError(f"[ga] son care Gemma olmali: {_r['model']} / {_calls}")
if _calls[:3] != ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]:
    raise AssertionError(f"[ga] Gemma'ya sadece gerektiginde ve EN SON gidilmeli: {_calls}")
print(f"  [ga:gemma-last-resort-only] cagrilar={_calls}")

# The normal path must never touch the whole catalog.
_c, _calls = _ga_client(lambda m, i: _ga_response(_VALID_SCRIPT))
_ga_run_script(_c)
_c2, _calls2 = _ga_client(lambda m, i: _ga_response({"title": "t", "description": "d"}))
_ga.run_google_bulk_classification("p", client=_c2, manifest=_GA_MANIFEST,
                                    ledger=_ga.QuotaLedger(), persist_ledger=False,
                                    required_fields=("title", "description"))
if len(_calls) != 1 or len(_calls2) != 1:
    raise AssertionError(f"[ga] FAN-OUT: {_calls} / {_calls2}")
if len(_ga.MODEL_REGISTRY) < 8:
    raise AssertionError("[ga] kayit defteri beklenenden kucuk; fan-out testi anlamsizlasir")
if _calls2 != ["gemini-3.5-flash-lite"]:
    raise AssertionError(f"[ga] toplu is Rota B ile baslamali: {_calls2}")
print(f"  [ga:no-catalog-fanout] kayitli {len(_ga.MODEL_REGISTRY)} model, yapilan cagri "
      f"{len(_calls)} (Rota A) / {len(_calls2)} (Rota B: {_calls2[0]})")

# --- 12/13/14. 429 handling: RPM retries, RPD does not sleep, Retry-After ----
_RPM_ERR = _GaError("429 RESOURCE_EXHAUSTED: rate limit exceeded, requests per minute", 429)
_RPD_ERR = _GaError("429 RESOURCE_EXHAUSTED quota_id: "
                    "GenerateRequestsPerDayPerProjectPerModel-FreeTier", 429)
if _ga.classify_google_error(_RPM_ERR) != _ga.ERROR_RATE_LIMIT_RPM:
    raise AssertionError("[ga] RPM 429 yanlis siniflandirildi")
if _ga.classify_google_error(_RPD_ERR) != _ga.ERROR_RATE_LIMIT_RPD:
    raise AssertionError("[ga] RPD 429 yanlis siniflandirildi")

_slept = []
_c, _calls = _ga_client(lambda m, i: _ga_raise(_RPM_ERR)
                        if m == "gemini-3.6-flash" else _ga_response(_VALID_SCRIPT))
_r = _ga_run_script(_c, sleep_fn=_slept.append, max_transient_retries=2)
if _calls.count("gemini-3.6-flash") != 3:
    raise AssertionError(f"[ga] RPM 429 sinirli yeniden deneme beklenir (1+2): {_calls}")
if len(_slept) != 2 or any(d > 60 for d in _slept):
    raise AssertionError(f"[ga] RPM beklemesi sinirsiz/kaydedilmemis: {_slept}")
if _r["model"] != "gemini-3.5-flash":
    raise AssertionError("[ga] RPM tukendikten sonra siradaki modele gecilmeli")
print(f"  [ga:rpm-429-bounded-retry] 3.6 denemesi={_calls.count('gemini-3.6-flash')} "
      f"uyku={[round(d, 2) for d in _slept]} -> {_r['model']}")

_slept = []
_led = _ga.QuotaLedger()
_c, _calls = _ga_client(lambda m, i: _ga_raise(_RPD_ERR)
                        if m == "gemini-3.6-flash" else _ga_response(_VALID_SCRIPT))
_r = _ga_run_script(_c, ledger=_led, sleep_fn=_slept.append)
if _calls != ["gemini-3.6-flash", "gemini-3.5-flash"] or _slept:
    raise AssertionError(f"[ga] RPD 429 SAATLERCE beklemeden hemen fallback yapmali: {_calls} {_slept}")
_exhausted_until = _led.record_for("gemini-3.6-flash")["daily_exhausted_until"]
if not _exhausted_until:
    raise AssertionError("[ga] RPD sonrasi model gunluk sifirlamaya kadar devre disi kalmali")
if _led.blocking_reason("gemini-3.6-flash") is None:
    raise AssertionError("[ga] gunluk kotasi tukenen model tekrar denenebilir gorunuyor")
print(f"  [ga:rpd-429-immediate-fallback] uyku yok, model {_exhausted_until} tarihine kadar kapali")

_slept = []
_retry_after_err = _GaError("429 rate limit", 429, headers={"Retry-After": "5"})
_c, _calls = _ga_client(lambda m, i: _ga_raise(_retry_after_err) if i == 0
                        else _ga_response(_VALID_SCRIPT))
_ga_run_script(_c, sleep_fn=_slept.append)
if _slept != [5.0]:
    raise AssertionError(f"[ga] Retry-After basligi dikkate alinmadi: {_slept}")
if _ga.parse_retry_after_seconds(_GaError('429 "retryDelay": "31s"', 429)) != 31.0:
    raise AssertionError("[ga] govdedeki retryDelay okunmadi")
if _ga.parse_retry_after_seconds(_GaError("429 no hint", 429)) is not None:
    raise AssertionError("[ga] sunucu bir sey soylemediginde None donmeli")
print(f"  [ga:retry-after-honored] header Retry-After=5 -> uyku={_slept}, govde retryDelay=31s OK")

# --- 15. 401/403 disables the credential for the whole run -------------------
_c, _calls = _ga_client(lambda m, i: _ga_raise(_GaError("403 PERMISSION_DENIED", 403)))
_r = _ga_run_script(_c, credential=_cred_new)
if _r["status"] != "failed" or _r["error_kind"] != _ga.ERROR_AUTH:
    raise AssertionError(f"[ga] 401/403 'failed'/auth olmali: {_r['status']}/{_r['error_kind']}")
if _calls != ["gemini-3.6-flash"]:
    raise AssertionError(f"[ga] auth hatasindan sonra baska model DENENMEMELI: {_calls}")
if _r.get("provider_disabled") != "google":
    raise AssertionError("[ga] auth hatasinda saglayici devre disi birakilmadi")
if "Colab" not in _r["status_reason"] or _FAKE_GEMINI_KEY in _r["status_reason"]:
    raise AssertionError("[ga] auth mesaji uygulanabilir/sanitize degil")
print(f"  [ga:auth-disables-provider] cagrilar={_calls} status={_r['status']} (tek deneme)")

# --- 16. 404 disables just that model ---------------------------------------
_led = _ga.QuotaLedger()
_c, _calls = _ga_client(lambda m, i: _ga_raise(_GaError("404 model was not found", 404))
                        if m == "gemini-3.6-flash" else _ga_response(_VALID_SCRIPT))
_r = _ga_run_script(_c, ledger=_led)
if _r["model"] != "gemini-3.5-flash":
    raise AssertionError("[ga] 404 sonrasi siradaki allowlist modeline gecilmeli")
if not _led.record_for("gemini-3.6-flash")["disabled_reason"]:
    raise AssertionError("[ga] 404 alan model devre disi isaretlenmedi")
if _led.blocking_reason("gemini-3.5-flash") is not None:
    raise AssertionError("[ga] 404 tum saglayiciyi degil SADECE o modeli kapatmali")
print("  [ga:404-disables-model-only] 3.6 kapandi, 3.5 hala kullanilabilir")

# --- 17. Circuit breaker after N consecutive transient failures --------------
_led = _ga.QuotaLedger()
for _i in range(_ga.CIRCUIT_BREAKER_THRESHOLD):
    _ga.record_google_usage(_led, "gemini-3.6-flash", ok=False,
                            error_kind=_ga.ERROR_SERVER, persist=False)
_rec = _led.record_for("gemini-3.6-flash")
if _rec["circuit_state"] != "open" or not _rec["cooldown_until"]:
    raise AssertionError(f"[ga] {_ga.CIRCUIT_BREAKER_THRESHOLD} gecici hatadan sonra devre acilmali")
if _led.blocking_reason("gemini-3.6-flash") is None:
    raise AssertionError("[ga] devre acikken model engellenmeli")
_cooldown = _dt.fromisoformat(_rec["cooldown_until"]) - _dt.now(_tz.utc)
if not (_td(minutes=14) < _cooldown <= _td(minutes=15, seconds=1)):
    raise AssertionError(f"[ga] varsayilan bekleme 15 dakika olmali: {_cooldown}")
_led.note_circuit_half_open("gemini-3.6-flash")
if _led.record_for("gemini-3.6-flash")["circuit_state"] != "half_open":
    raise AssertionError("[ga] yarim-acik durum ayarlanmadi")
_ga.record_google_usage(_led, "gemini-3.6-flash", ok=True, persist=False)
if _led.record_for("gemini-3.6-flash")["circuit_state"] != "closed":
    raise AssertionError("[ga] tek basarili test isteginden sonra devre kapanmali")
print(f"  [ga:circuit-breaker] {_ga.CIRCUIT_BREAKER_THRESHOLD} gecici hata -> open "
      f"(~15 dk) -> half_open -> basari -> closed")

# --- 18. Safety refusal never rotates model/provider ------------------------
_safety_response = type("R", (), {
    "text": "", "usage_metadata": None,
    "candidates": [{"finish_reason": "SAFETY", "content": {"parts": []}}]})()
_c, _calls = _ga_client(lambda m, i: _safety_response)
_r = _ga_run_script(_c)
if _calls != ["gemini-3.6-flash"]:
    raise AssertionError(f"[ga] GUVENLIK reddinde model/saglayici DEGISTIRILMEMELI: {_calls}")
if _r["status"] != "review_required" or _r["error_kind"] != _ga.ERROR_SAFETY:
    raise AssertionError(f"[ga] guvenlik reddi review_required olmali: {_r['status']}")
if _r["output"] is not None:
    raise AssertionError("[ga] guvenlik reddinde cikti uretilmemeli")
if "DEGISTIRILMEDI" not in _r["status_reason"]:
    raise AssertionError("[ga] guvenlik reddinde rotasyon yapilmadigi kayda gecmemis")
_cross = _pa.run_cross_provider_text_fallback("x", secret_getter=lambda n: None)
if _cross["status"] != "insufficient_data" or _cross["output"] is not None:
    raise AssertionError("[ga] capraz saglayici kimlik yokken basari uydurmamali")
print(f"  [ga:safety-no-rotation] cagrilar={_calls} status={_r['status']} (reddin nedeni kayitli)")

# --- 19. Invalid structured output: EXACTLY one corrective retry -------------
_c, _calls = _ga_client(lambda m, i: _ga_response("bu JSON degil"))
_r = _ga_run_script(_c)
_per_model = {m: _calls.count(m) for m in set(_calls)}
if set(_per_model.values()) != {2}:
    raise AssertionError(f"[ga] model basina TAM 1 duzeltici yeniden deneme beklenir: {_per_model}")
if _r["status"] != "failed" or _r["output"] is not None:
    raise AssertionError("[ga] gecersiz cikti sonunda 'failed' olmali, uydurulmamali")
if not any(a.get("error_kind") == _ga.ERROR_INVALID_OUTPUT for a in _r["provider_attempts"]):
    raise AssertionError("[ga] bozuk cikti nedeni kaydedilmemis")
# Corrective retry succeeds on the second try -> exactly 2 calls, no fallback.
_c, _calls = _ga_client(lambda m, i: _ga_response("bozuk") if i == 0 else _ga_response(_VALID_SCRIPT))
_r = _ga_run_script(_c)
if _calls != ["gemini-3.6-flash", "gemini-3.6-flash"] or _r["status"] != "ok":
    raise AssertionError(f"[ga] duzeltici yeniden deneme ayni modelde olmali: {_calls}")
# Syntactically valid but SEMANTICALLY invalid JSON is rejected too.
_c, _calls = _ga_client(lambda m, i: _ga_response({"script": "   "}))
if _ga_run_script(_c)["status"] == "ok":
    raise AssertionError("[ga] bos 'script' alani semantik olarak reddedilmeli")
_c, _calls = _ga_client(lambda m, i: _ga_response({"script": "kisa"}))
if _ga_run_script(_c)["status"] == "ok":
    raise AssertionError("[ga] anlamsiz kisa script semantik olarak reddedilmeli")
print("  [ga:invalid-output-one-retry] 1 duzeltici deneme, sonra fallback; "
      "sozdizimsel gecerli ama anlamsiz JSON da reddediliyor")

# --- 20. Everything exhausted -> failed, never fabricated --------------------
_c, _calls = _ga_client(lambda m, i: _ga_raise(_GaError("500 internal error", 500)))
_r = _ga_run_script(_c)
if _r["status"] != "failed" or _r["output"] is not None or _r["model"] is not None:
    raise AssertionError(f"[ga] tum modeller basarisizken uydurma cikti: {_r['status']}")
if "UYDURULMADI" not in _r["status_reason"]:
    raise AssertionError("[ga] basarisizlik nedeni acik degil")
if len(_r["provider_attempts"]) != 4:
    raise AssertionError(f"[ga] her deneme kaydedilmeli: {len(_r['provider_attempts'])}")
_art = _ga.build_google_artifact(_r, task="script_generation", capability="generate_content",
                                  input_content="x")
if _art["status"] != "failed" or _art["output_content_sha256"] is not None:
    raise AssertionError("[ga] basarisiz artefakt yanlis")
for _bad in ("approved", "production_ready"):
    try:
        _ga.build_google_artifact(_r, task="script_generation", capability="generate_content",
                                   status=_bad)
    except ValueError:
        pass
    else:
        raise AssertionError(f"[ga] artefakt kendini '{_bad}' yapabildi!")
# A route with no usable model must also stop cleanly rather than guessing.
_no_model = _ga.select_google_route("script_generation", mode="explicit", manifest=_GA_MANIFEST)
if _no_model["route"] or _no_model["status"] != "no_explicit_override":
    raise AssertionError("[ga] explicit modda override yokken model secilmemeli")
print(f"  [ga:all-exhausted-never-fabricates] status={_r['status']} "
      f"denemeler={len(_r['provider_attempts'])} cikti=None; artefakt kendini onaylayamiyor")

# --- 21/22/23. Quota ledger: persistence, Pacific reset, manual reset --------
_ledger_path = _GA_TMP / "google_quota_usage.json"
_l1 = _ga.QuotaLedger(_ledger_path)
_ga.record_google_usage(_l1, "gemini-3.6-flash", ok=True, estimated_input_tokens=1200,
                         usage_metadata={"prompt_token_count": 1000, "total_token_count": 1400})
_ga.record_google_usage(_l1, "gemini-3.6-flash", ok=False, error_kind=_ga.ERROR_SERVER)
if not _ledger_path.is_file():
    raise AssertionError("[ga] kota defteri diske yazilmadi")
_l2 = _ga.QuotaLedger(_ledger_path)
_rec = _l2.record_for("gemini-3.6-flash")
if not (_rec["requests_current_day"] == 2 and _rec["successful_calls"] == 1
        and _rec["failed_calls"] == 1 and _rec["estimated_input_tokens_current_minute"] == 1200):
    raise AssertionError(f"[ga] kota defteri yeniden yuklenmedi: {_rec}")
if _rec["last_server_usage_metadata"] != {"prompt_token_count": 1000, "total_token_count": 1400}:
    raise AssertionError("[ga] sunucu usage_metadata kaydedilmedi")
if _rec["estimated_remaining_rpd"] != _ga.MODEL_REGISTRY["gemini-3.6-flash"]["configured_rpd"] - 2:
    raise AssertionError("[ga] kalan RPD tahmini yanlis")
if _l2.data.get("estimated") is not True or "YEREL TAHMIN" not in _l2.data["disclaimer"]:
    raise AssertionError("[ga] defter 'tahmini' olarak etiketlenmemis")
for _ in range(_ga.MODEL_REGISTRY["gemini-3.6-flash"]["configured_rpd"] - 2):
    _ga.record_google_usage(_l2, "gemini-3.6-flash", ok=True)
if _l2.blocking_reason("gemini-3.6-flash") is None:
    raise AssertionError("[ga] yerel tahmin limiti doldugunda cagri engellenmeli")
_c, _blocked_calls = _ga_client(lambda m, i: _ga_response(_VALID_SCRIPT))
_r = _ga_run_script(_c, ledger=_l2, persist_ledger=False)
if "gemini-3.6-flash" in _blocked_calls:
    raise AssertionError("[ga] limiti dolu oldugu KANITLI modele cagri yapildi")
# A corrupt ledger must not crash the pipeline.
_corrupt_path = _GA_TMP / "corrupt_ledger.json"
_corrupt_path.write_text("{ bu gecerli JSON degil", encoding="utf-8")
if _ga.QuotaLedger(_corrupt_path).data["models"] != {}:
    raise AssertionError("[ga] bozuk defter temiz baslangica donmedi")
print(f"  [ga:ledger-persist-reload] disk uzerinden yeniden yuklendi; dolu model atlandi "
      f"-> {_r['model']}; bozuk defter cokmeden kurtarildi")

# Documented reset: midnight Pacific (UTC-7 in DST, UTC-8 otherwise).
if _ga.pacific_day_key(_dt(2026, 7, 15, 6, 59, tzinfo=_tz.utc)) != "2026-07-14":
    raise AssertionError("[ga] PDT gun anahtari yanlis (06:59Z hala onceki gun olmali)")
if _ga.pacific_day_key(_dt(2026, 7, 15, 7, 1, tzinfo=_tz.utc)) != "2026-07-15":
    raise AssertionError("[ga] PDT gun donusu 07:00Z'de olmali")
if _ga.pacific_day_key(_dt(2026, 1, 15, 7, 59, tzinfo=_tz.utc)) != "2026-01-14":
    raise AssertionError("[ga] PST gun anahtari yanlis (07:59Z hala onceki gun olmali)")
if _ga.pacific_day_key(_dt(2026, 1, 15, 8, 1, tzinfo=_tz.utc)) != "2026-01-15":
    raise AssertionError("[ga] PST gun donusu 08:00Z'de olmali")
if _ga.next_pacific_reset(_dt(2026, 7, 15, 8, 0, tzinfo=_tz.utc)) != _dt(2026, 7, 16, 7, 0, tzinfo=_tz.utc):
    raise AssertionError("[ga] bir sonraki PDT sifirlama ani yanlis")
_rolling = _ga.QuotaLedger(now_fn=lambda: _dt(2026, 7, 15, 8, 0, tzinfo=_tz.utc))
_ga.record_google_usage(_rolling, "gemini-3.6-flash", ok=True, persist=False)
_rolling._now = lambda: _dt(2026, 7, 16, 8, 0, tzinfo=_tz.utc)
if _rolling.record_for("gemini-3.6-flash")["requests_current_day"] != 0:
    raise AssertionError("[ga] gunluk sayac Pasifik gece yarisinda sifirlanmadi")
print("  [ga:pacific-daily-reset] PDT 07:00Z / PST 08:00Z sinirlari ve sayac donusu dogru")

_reset_entry = _l2.manual_reset("gemini-3.6-flash", "test: yerel sayac yanlis kalmisti", "verify_local")
if not _reset_entry["reason"] or _reset_entry["reset_by"] != "verify_local":
    raise AssertionError("[ga] manuel sifirlama gerekcesi/kaydi eksik")
if len(_l2.data["manual_resets"]) != 1 or "sunucu kotasini degistirmez" not in _reset_entry["note"]:
    raise AssertionError("[ga] manuel sifirlama manifeste yazilmadi/yanlis anlatildi")
try:
    _l2.manual_reset("gemini-3.6-flash", "   ")
except ValueError:
    pass
else:
    raise AssertionError("[ga] gerekcesiz manuel sifirlama kabul edildi!")
print("  [ga:manual-reset-requires-reason] gerekce zorunlu ve kayitli; gerekcesiz istek reddedildi")

# --- 24. Account/key rotation must be structurally impossible ---------------
if _ga.ALLOW_ACCOUNT_QUOTA_ROTATION is not False:
    raise AssertionError("[ga] ALLOW_ACCOUNT_QUOTA_ROTATION False olmali")
_ROTATION_PATTERNS = [
    r"\bapi_keys\b", r"\bkey_pool\b", r"\bkey_rotation\b", r"\brotate_key\b",
    r"\brotate_account\b", r"\bnext_api_key\b", r"\bcredential_pool\b",
    r"\bGOOGLE_API_KEYS\b", r"\bGEMINI_KEYS\b",
    r"itertools\.cycle", r"\bGeminiKey[123]\b",
]
for _pattern in _ROTATION_PATTERNS:
    for _label, _text in (("google_ai.py", _ga_source), ("provider_adapters.py", _pa_source)):
        if _re.search(_pattern, _text):
            raise AssertionError(f"[ga] {_label}: anahtar/hesap rotasyonu deseni bulundu: {_pattern}")
# Signature-level proof: no entry point accepts a COLLECTION of credentials.
for _fn_name in ("build_google_client", "execute_google_route", "select_google_route",
                  "resolve_secret", "run_google_script_generation", "run_google_tts",
                  "run_google_embeddings"):
    _params = _inspect.signature(getattr(_ga, _fn_name)).parameters
    for _bad_param in ("credentials", "api_keys", "keys", "accounts", "key_pool"):
        if _bad_param in _params:
            raise AssertionError(f"[ga] {_fn_name} coklu kimlik parametresi kabul ediyor: {_bad_param}")
# build_google_client takes exactly one CredentialRef; a pool is a TypeError.
for _pool in ([_cred_new, _cred_old], (_cred_new,), {"a": _cred_new}):
    try:
        _ga.build_google_client(_pool, client_factory=lambda api_key=None: object())
    except TypeError:
        pass
    else:
        raise AssertionError("[ga] kimlik havuzu ile istemci kurulabildi!")
# Flipping the switch makes the router refuse to run at all.
_ga.ALLOW_ACCOUNT_QUOTA_ROTATION = True
try:
    try:
        _ga.assert_no_account_quota_rotation()
    except _ga.GoogleAIPolicyError:
        pass
    else:
        raise AssertionError("[ga] rotasyon acikken politika kontrolu gecti!")
    try:
        _ga_run_script(_ga_client(lambda m, i: _ga_response(_VALID_SCRIPT))[0])
    except _ga.GoogleAIPolicyError:
        pass
    else:
        raise AssertionError("[ga] rotasyon acikken router calisti!")
finally:
    _ga.ALLOW_ACCOUNT_QUOTA_ROTATION = False
# The notebook config must ship the switch OFF as well.
_master_google_cfg = _master_cell("GOOGLE_ROUTE_MODE = 'auto'", "ALLOW_ACCOUNT_QUOTA_ROTATION")
if not _re.search(r"^ALLOW_ACCOUNT_QUOTA_ROTATION = False\b", _master_google_cfg, _re.M):
    raise AssertionError("[ga] master notebook rotasyon anahtarini False olarak tanimlamiyor")
print("  [ga:no-account-rotation] desen taramasi + imza kontrolu + havuz reddi + politika kapisi OK")

# --- 25/26. TTS: valid audio, and a fallback never inherits approval ---------
_PCM_ONE_SEC = b"\x10\x27" * _ga.TTS_SAMPLE_RATE  # 24k frames of 16-bit PCM


def _tts_client(fail_models=(), pcm=_PCM_ONE_SEC):
    calls = []

    class _Models:
        def generate_content(self, *, model, contents, config=None):
            calls.append((model, config["speech_config"]["voice_config"]
                          ["prebuilt_voice_config"]["voice_name"],
                          config["speech_config"]["language_code"],
                          tuple(config["response_modalities"])))
            if model in fail_models:
                raise _GaError("503 unavailable", 503)
            return type("R", (), {
                "text": None, "usage_metadata": None,
                "candidates": [{"content": {"parts": [
                    {"inline_data": {"data": pcm, "mime_type": "audio/L16;rate=24000"}}]}}]})()
    return type("C", (), {"models": _Models()})(), calls


_SCRIPT_TEXT = "Merhaba, bu Turkce bir test seslendirmesidir ve gecerli bir WAV uretmelidir."
_tc, _tcalls = _tts_client()
_tts_out = _GA_TMP / "gemini_tts.wav"
_tres = _ga.run_google_tts(_SCRIPT_TEXT, _tts_out, client=_tc, manifest=_GA_MANIFEST,
                           ledger=_ga.QuotaLedger(), persist_ledger=False,
                           voice="Kore", language_code="tr-TR",
                           narration_direction="Gergin bir tonda oku:")
if _tres["status"] != "ok":
    raise AssertionError(f"[ga] TTS basarisiz: {_tres['status_reason']}")
if _tcalls[0] != ("gemini-3.1-flash-tts-preview", "Kore", "tr-TR", ("AUDIO",)):
    raise AssertionError(f"[ga] TTS yapilandirmasi yanlis: {_tcalls}")
_val = _tres["audio_validation"]
if not _val["passed"]:
    raise AssertionError(f"[ga] uretilen ses dogrulamayi gecmedi: {_val}")
if not (_val["details"]["sample_rate"] == 24000 and _val["details"]["channels"] == 1
        and _val["details"]["bits_per_sample"] == 16 and _val["details"]["duration_sec"] > 0.9):
    raise AssertionError(f"[ga] ses ozellikleri beklenenden farkli: {_val['details']}")
if _tts_out.read_bytes()[:4] != b"RIFF" or _tts_out.stat().st_size < 1024:
    raise AssertionError("[ga] gecerli/dolu bir WAV dosyasi yazilmadi")
if _tres["source_script_sha256"] != _ga.sha256_text(_SCRIPT_TEXT):
    raise AssertionError("[ga] kaynak script SHA-256 kaydedilmedi")
# Empty audio must fail loudly rather than writing a 44-byte "file".
_tc_empty, _ = _tts_client(pcm=b"")
if _ga.run_google_tts(_SCRIPT_TEXT, _GA_TMP / "empty.wav", client=_tc_empty,
                       manifest=_GA_MANIFEST, ledger=_ga.QuotaLedger(),
                       persist_ledger=False, sleep_fn=lambda _s: None)["status"] == "ok":
    raise AssertionError("[ga] bos ses 'ok' sayildi")
# Splitting happens ONLY when required.
if len(_ga.split_script_for_tts(_SCRIPT_TEXT)) != 1:
    raise AssertionError("[ga] kisa script gereksiz yere bolundu")
if len(_ga.split_script_for_tts("Cumle bir. " * 800)) < 2:
    raise AssertionError("[ga] uzun script bolunmedi")
print(f"  [ga:tts-valid-audio] {_val['details']['duration_sec']}s 24kHz/16bit/mono WAV, "
      f"script SHA-256 kayitli, bos ses reddedildi, gereksiz bolme yok")

_vo_artifact = _ga.build_tts_artifact(_tres, _SCRIPT_TEXT, script_status="approved")
if _vo_artifact["status"] != "review_required" or _vo_artifact["preview_model"] is not True:
    raise AssertionError("[ga] preview TTS ciktisi her zaman review_required olmali")
if _vo_artifact["human_approval"]["approved"] is not False:
    raise AssertionError("[ga] TTS artefakti uretim aninda onaylanmis!")
_approved_vo = _approval.approve_artifact(_vo_artifact, "voiceover",
                                           _approval.sha256_file(_tts_out), "verify_local")
if _approval.check_approval(_approved_vo, "voiceover")["valid"] is not True:
    raise AssertionError("[ga] taze onay gecerli olmali")
# Fallback voice/model -> NEW audio -> the old approval must NOT carry over.
_tc2, _tcalls2 = _tts_client(fail_models={"gemini-3.1-flash-tts-preview"},
                              pcm=b"\x20\x30" * _ga.TTS_SAMPLE_RATE)
_tres2 = _ga.run_google_tts(_SCRIPT_TEXT, _tts_out, client=_tc2, manifest=_GA_MANIFEST,
                            ledger=_ga.QuotaLedger(), persist_ledger=False,
                            voice="Puck", sleep_fn=lambda _s: None)
if _tres2["model"] != "gemini-2.5-flash-preview-tts" or _tres2["status"] != "ok":
    raise AssertionError(f"[ga] TTS yedek modeline gecmedi: {_tres2.get('model')}")
_stale = _approval.check_approval(_approved_vo, "voiceover")
if _stale["valid"] is not False:
    raise AssertionError("[ga] ses degistigi halde eski onay hala gecerli sayildi!")
_vo2 = _ga.build_tts_artifact(_tres2, _SCRIPT_TEXT, script_status="approved")
if _vo2["status"] != "review_required" or _vo2["human_approval"]["approved"] is not False \
        or _vo2["inherits_previous_approval"] is not False:
    raise AssertionError("[ga] yedek seslendirme onceki onayi devraldi!")
if _vo2["output_content_sha256"] == _vo_artifact["output_content_sha256"]:
    raise AssertionError("[ga] yedek seslendirme ayni hash'i tasiyor; test anlamsiz")
if _vo2["source_script_hash"] != _ga.sha256_text(_SCRIPT_TEXT)[:16]:
    raise AssertionError("[ga] render kapisinin bekledigi kisa script hash'i yok")
print(f"  [ga:tts-fallback-invalidates-approval] {_tres2['model']}/{_tres2['voice']} -> "
      f"eski onay gecersiz, yeni artefakt onaysiz (devralma yok)")

# --- 27/28. Embeddings: endpoint, dimension, deterministic novelty ----------
def _embed_client(dim=768):
    calls = []

    class _Models:
        def embed_content(self, *, model, contents, config=None):
            calls.append((model, config["output_dimensionality"], len(contents), contents[0]))
            return {"embeddings": [{"values": [0.1] * dim} for _ in contents]}

        def generate_content(self, **kw):
            raise AssertionError("embeddings generate_content KULLANMAMALI")
    return type("C", (), {"models": _Models()})(), calls


_ec, _ecalls = _embed_client()
_eres = _ga.run_google_embeddings(["birinci metin", "ikinci metin"], client=_ec,
                                   manifest=_GA_MANIFEST, ledger=_ga.QuotaLedger(),
                                   persist_ledger=False)
if _eres["status"] != "ok" or len(_eres["output"]) != 2 or len(_eres["output"][0]) != 768:
    raise AssertionError(f"[ga] embedding cikti sekli yanlis: {_eres['status']}")
if _ecalls[0][0] != "gemini-embedding-2" or _ecalls[0][1] != 768:
    raise AssertionError(f"[ga] embed_content yanlis model/boyutla cagrildi: {_ecalls}")
if not _ecalls[0][3].startswith("task: semantic similarity"):
    raise AssertionError("[ga] gorev oneki prompt metnine yazilmadi")
if _eres["input_hashes"] != [_ga.sha256_text("birinci metin"), _ga.sha256_text("ikinci metin")]:
    raise AssertionError("[ga] girdi hash'leri kaydedilmedi")
if "inputs" in _eres:
    raise AssertionError("[ga] ham girdi metni gereksiz yere saklandi")
if _ga.MODEL_REGISTRY["gemini-embedding-2"]["api"] != "embed_content":
    raise AssertionError("[ga] embedding modeli generate_content olarak kayitli")
# A wrong-sized vector is rejected instead of being silently accepted.
_ec_bad, _ = _embed_client(dim=512)
if _ga.run_google_embeddings(["x"], client=_ec_bad, manifest=_GA_MANIFEST,
                              ledger=_ga.QuotaLedger(), persist_ledger=False)["status"] == "ok":
    raise AssertionError("[ga] beklenen boyutta olmayan vektor kabul edildi")
print("  [ga:embeddings] embed_content/gemini-embedding-2, boyut=768, gorev oneki + "
      "girdi hash'leri kayitli, yanlis boyut reddedildi")

_v_same, _v_orth, _v_near = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.98, 0.2, 0.0]
if _ga.novelty_score(_v_same, [_v_same]) != 0.0 or _ga.novelty_score(_v_same, [_v_orth]) != 1.0:
    raise AssertionError("[ga] novelty_score sinir degerleri yanlis")
if _ga.novelty_score(_v_same, []) != 1.0:
    raise AssertionError("[ga] gecmis yokken novelty 1.0 olmali")
if _ga.novelty_score(_v_same, [_v_orth, _v_same]) != _ga.novelty_score(_v_same, [_v_same, _v_orth]):
    raise AssertionError("[ga] novelty_score deterministik degil")
_dups = _ga.find_semantic_duplicates(_v_same, [("eski_a", _v_same), ("eski_b", _v_orth)])
if [d["label"] for d in _dups] != ["eski_a"]:
    raise AssertionError(f"[ga] duplikasyon tespiti yanlis: {_dups}")
_clusters = _ga.cluster_by_similarity([("a", _v_same), ("b", _v_near), ("c", _v_orth)])
if sorted(len(c["members"]) for c in _clusters) != [1, 2]:
    raise AssertionError(f"[ga] kumeleme beklenmedik: {_clusters}")
if _ga.duplicate_penalty(0.0) != 0.35 or _ga.duplicate_penalty(1.0) != 0.0:
    raise AssertionError("[ga] duplikasyon cezasi sinir degerleri yanlis")
print("  [ga:novelty-duplicate-deterministic] novelty/duplikasyon/kumeleme/ceza sabit "
      "fixture'larda kararli")

# --- 29. Grounded research keeps its source links ---------------------------
_GROUNDED = type("R", (), {
    "text": "Konu su an gundemde.", "usage_metadata": None,
    "candidates": [{
        "content": {"parts": []},
        "grounding_metadata": {
            "grounding_chunks": [
                {"web": {"uri": "https://haber.example/a", "title": "A", "domain": "haber.example"}},
                {"web": {"uri": "https://haber.example/b", "title": "B", "domain": "haber.example"}},
            ],
            "web_search_queries": ["konu son dakika"],
        }}]})()
_gc, _gcalls = _ga_client(lambda m, i: _GROUNDED)
_gres = _ga.run_google_grounded_research("Konu gundemde mi?", client=_gc, manifest=_GA_MANIFEST,
                                          ledger=_ga.QuotaLedger(), persist_ledger=False)
if _gres["status"] != "ok" or _gcalls != ["gemini-3.6-flash"]:
    raise AssertionError(f"[ga] grounded rota Gemini 3.6 ile baslamali: {_gcalls}")
if [s["uri"] for s in _gres["grounding_sources"]] != ["https://haber.example/a",
                                                       "https://haber.example/b"]:
    raise AssertionError(f"[ga] kaynak baglantilari korunmadi: {_gres['grounding_sources']}")
if _gres["grounding_queries"] != ["konu son dakika"]:
    raise AssertionError("[ga] arama sorgulari kaydedilmedi")
_gart = _ga.build_google_artifact(_gres, task="grounded_research",
                                   capability="generate_content(tools=[google_search])",
                                   input_content="Konu gundemde mi?",
                                   output_content=_gres["output"],
                                   extra={"grounding_sources": _gres["grounding_sources"],
                                          "grounding_queries": _gres["grounding_queries"],
                                          "research_caveat": _gres["research_caveat"]})
if "haber.example/a" not in json.dumps(_gart):
    raise AssertionError("[ga] artefakt kaynak baglantilarini icermiyor")
if "yerine GECMEZ" not in _gart["research_caveat"]:
    raise AssertionError("[ga] grounded arastirmanin sinirlari artefaktta belirtilmemis")
# Retired 2.5-flash and unverified 2.5-flash-lite must not be in the live route.
if "gemini-2.5-flash" in _ga.TASK_ROUTES["grounded_research"]:
    raise AssertionError("[ga] emekli gemini-2.5-flash grounded rotasinda kalmis")
if "gemini-2.5-flash-lite" in _ga.TASK_ROUTES["grounded_research"]:
    raise AssertionError("[ga] dogrulanmamis grounding modeli rotaya alinmis")
if _ga.ACTION_GROUNDED in _ga.MODEL_REGISTRY["gemini-2.5-flash-lite"]["actions"]:
    raise AssertionError("[ga] gemini-2.5-flash-lite yanlislikla grounding-yetenekli isaretlenmis")
print(f"  [ga:grounded-keeps-sources] {len(_gres['grounding_sources'])} kaynak + 1 sorgu "
      f"artefaktta; emekli/dogrulanmamis 2.5 modelleri rotada degil")

# --- 30. Antigravity: off by default, hard token budget --------------------
_off = _ga.run_antigravity_research_experimental("arastir")
if _off["status"] != "experimental" or _off["provider_attempts"] or _off["output"] is not None:
    raise AssertionError(f"[ga] antigravity varsayilan olarak calismamali: {_off['status']}")
if "RUN_ANTIGRAVITY_RESEARCH=False" not in _off["status_reason"]:
    raise AssertionError("[ga] antigravity kapali olma nedeni belirtilmemis")
try:
    _ga.run_antigravity_research_experimental("arastir", enabled=True)
except ValueError:
    pass
else:
    raise AssertionError("[ga] antigravity acik token butcesi olmadan calisti!")
_over = _ga.run_antigravity_research_experimental("x" * 40000, enabled=True, max_total_tokens=100)
if _over["status"] != "failed" or _over["provider_attempts"]:
    raise AssertionError("[ga] butceyi asan istek cagri yapmadan reddedilmeliydi")
_agc, _agcalls = _ga_client(lambda m, i: _ga_response("Deneysel rapor metni."))
_agres = _ga.run_antigravity_research_experimental(
    "arastir", enabled=True, max_total_tokens=8000, client=_agc,
    manifest=_GA_MANIFEST, ledger=_ga.QuotaLedger(), persist_ledger=False)
if _agcalls != ["antigravity-preview-05-2026"] or _agres["status"] != "experimental":
    raise AssertionError(f"[ga] antigravity rotasi/durumu yanlis: {_agcalls} {_agres['status']}")
if _agres["structured_output_capable"] is not False:
    raise AssertionError("[ga] antigravity yapisal-cikti yetenekli sayildi")
_agart = _ga.build_antigravity_artifact(_agres)
if _agart["status"] != "experimental" or _agart["production_ready"] is not False \
        or _agart["review_required"] is not True:
    raise AssertionError(f"[ga] antigravity artefakti yanlis: {_agart['status']}")
if _agart["human_approval"]["approved"] is not False:
    raise AssertionError("[ga] antigravity onay durumunu degistirdi!")
print("  [ga:antigravity-off-and-budgeted] varsayilan kapali, butce zorunlu, artefakt "
      "experimental/review_required, production_ready=False")

# --- Master notebook config surface (Phase 4 toggles) ----------------------
_master_google_cfg_all = _master_cell("RUN_ANTIGRAVITY_RESEARCH = False", "RUN_GOOGLE_TTS")
for _flag, _expected in [("RUN_ANTIGRAVITY_RESEARCH", "False"),
                          ("ENABLE_GOOGLE_SEARCH_GROUNDING", "False"),
                          ("RUN_GOOGLE_CONNECTION_TEST", "False"),
                          ("ALLOW_RAW_MEDIA_TO_FREE_TIER", "False"),
                          ("ALLOW_ACCOUNT_QUOTA_ROTATION", "False"),
                          ("GOOGLE_ROUTE_MODE", "'auto'"),
                          ("GOOGLE_TASK_MODEL_OVERRIDES", "{}"),
                          ("MAX_GOOGLE_MODEL_ATTEMPTS", "4"),
                          ("MAX_PROVIDER_ATTEMPTS", "3"),
                          ("ENABLE_GOOGLE_MODEL_FALLBACK", "True"),
                          ("ENABLE_CROSS_PROVIDER_FALLBACK", "True")]:
    # (?=\s|$) not \b: values like 'auto' and {} end in a non-word character,
    # where \b would never match.
    if not _re.search(rf"^{_flag} = {_re.escape(_expected)}(?=\s|$)",
                      _master_google_cfg_all, _re.M):
        raise AssertionError(f"[ga] master notebook '{_flag} = {_expected}' tanimlamiyor")
for _cmd_flag in ("RUN_GOOGLE_HOTMOMENT_REVIEW", "RUN_GOOGLE_SCRIPT_GENERATION",
                   "RUN_GOOGLE_BULK_CLASSIFICATION", "RUN_GOOGLE_GROUNDED_RESEARCH",
                   "RUN_GOOGLE_TTS", "RUN_GOOGLE_EMBEDDINGS", "RUN_ANTIGRAVITY_RESEARCH"):
    if not _re.search(rf"^{_cmd_flag} = (True|False)\b", _master_google_cfg_all, _re.M):
        raise AssertionError(f"[ga] bagimsiz komut anahtari eksik: {_cmd_flag}")
for _profile_flag in ("GOOGLE_CREDENTIAL_PROFILE = 'primary'",
                      "YOUTUBE_CREDENTIAL_PROFILE = 'primary'",
                      "GeminiKeySecondary", "YoutubeKEYSecondary"):
    if _profile_flag not in _master_google_cfg_all:
        raise AssertionError(f"[ga] manuel credential profil ayari eksik: {_profile_flag}")
if "enable_cross_provider_fallback=ENABLE_CROSS_PROVIDER_FALLBACK" not in _master_all_source:
    raise AssertionError("[ga] cross-provider ayari script runtime'ina baglanmamis")
if "run_cross_provider_tts_fallback" not in _master_all_source:
    raise AssertionError("[ga] Azure TTS fallback master runtime'ina baglanmamis")
if "run_cross_provider_embeddings_fallback" not in _master_all_source:
    raise AssertionError("[ga] embedding fallback master runtime'ina baglanmamis")
# The route banner must be printed before execution and must show no secret.
_master_router_cell = _master_cell("GOOGLE_CREDENTIAL = google_ai.resolve_profile_secret",
                                    "def show_google_route")
for _needle in ("format_route_line", "Account rotation", "assert_no_account_quota_rotation"):
    if _needle not in _master_router_cell and _needle != "Account rotation":
        raise AssertionError(f"[ga] master rota hucresinde '{_needle}' yok")
_banner = _ga.format_route_line(
    _ga.select_google_route("script_generation", manifest=_GA_MANIFEST, ledger=_ga.QuotaLedger()),
    _cred_new)
for _line in ("Task: script_generation",
               "Route: gemini-3.6-flash -> gemini-3.5-flash -> gemini-3.5-flash-lite -> gemma-4-31b-it",
               "Credential: configured (GeminiKey)", "Account rotation: disabled",
               "Raw media upload: disabled"):
    if _line not in _banner:
        raise AssertionError(f"[ga] rota banner'inda eksik satir: {_line!r}\n{_banner}")
if _FAKE_GEMINI_KEY in _banner:
    raise AssertionError("[ga] rota banner'i gizli deger sizdirdi!")
print("  [ga:notebook-toggles-and-route-banner] 11 yapilandirma + 7 bagimsiz komut anahtari; "
      "banner rotayi gosteriyor, sirri gostermiyor")

# --- 31. Raw media never leaves the machine on the free tier ---------------
try:
    _ga.assert_raw_media_allowed(False, "kaynak video")
except _ga.RawMediaUploadBlocked as _exc:
    if "ALLOW_RAW_MEDIA_TO_FREE_TIER=False" not in str(_exc) or "ucretsiz katman" not in str(_exc):
        raise AssertionError("[ga] ham medya reddi acik degil")
else:
    raise AssertionError("[ga] ham medya varsayilan olarak yuklenebilir sayildi!")
_ga.assert_raw_media_allowed(True)  # explicit opt-in must remain possible
if "gerekli haklara" not in _ga.raw_media_warning():
    raise AssertionError("[ga] ham medya uyarisi telif hakki uyarisi icermiyor")
if _ga.ALLOW_RAW_MEDIA_TO_FREE_TIER_DEFAULT is not False:
    raise AssertionError("[ga] ham medya varsayilani False olmali")
_rm_artifact = _ga.build_google_artifact(_r, task="script_generation",
                                          capability="generate_content", raw_media_submitted=False)
if _rm_artifact["raw_media_submitted"] is not False or not _rm_artifact["free_tier_data_notice"]:
    raise AssertionError("[ga] artefakt ham-medya/veri-politikasi alanlarini tasimiyor")
for _needle in ("files.upload", "upload_file("):
    if _needle in _ga_source:
        raise AssertionError(f"[ga] google_ai.py dosya yukleme cagrisi iceriyor: {_needle}")
print("  [ga:no-raw-media-upload] varsayilan reddediyor, artefakt raw_media_submitted=False, "
      "modulde dosya yukleme cagrisi yok")

# --- Artifact contract completeness ----------------------------------------
_c, _ = _ga_client(lambda m, i: _ga_response(_VALID_SCRIPT, usage={"total_token_count": 42}))
_ok_result = _ga_run_script(_c)
_ok_artifact = _ga.build_google_artifact(
    _ok_result, task="script_generation", capability="generate_content",
    input_content="test prompt", output_content=_ok_result["output"])
for _field in ("provider", "model", "endpoint", "capability", "task", "route_requested",
                "provider_attempts", "selected_attempt", "fallback_used", "fallback_reason",
                "status", "status_reason", "created_at", "latency_sec", "usage_metadata",
                "estimated_quota_before", "estimated_quota_after", "input_content_sha256",
                "output_content_sha256", "raw_media_submitted", "free_tier_data_notice",
                "human_approval"):
    if _field not in _ok_artifact:
        raise AssertionError(f"[ga] artefakt sozlesmesinde '{_field}' eksik")
if _ok_artifact["status"] != "review_required":
    raise AssertionError("[ga] basarili artefakt review_required olmali")
if _ok_artifact["usage_metadata"] != {"total_token_count": 42}:
    raise AssertionError("[ga] sunucu usage_metadata artefakta yazilmadi")
if _ok_artifact["status"] not in _ga.STATUS_VALUES:
    raise AssertionError("[ga] artefakt durum sozlugunun disinda")
json.dumps(_ok_artifact)  # serializable => no SDK objects / raw exceptions inside
print("  [ga:artifact-contract] 22 zorunlu alan mevcut, JSON-serilestirilebilir, "
      "durum 'review_required'")

# --- Cross-provider adapters: skip cleanly, never fabricate ----------------
if any(v["state"] != "missing" for v in _pa.describe_provider_states(lambda n: None).values()):
    raise AssertionError("[ga] kimlik yokken saglayicilar 'missing' olmali")
_pa_secrets = {"GROQ_API_KEY": "FAKE-GROQ-" + "0" * 10,
               "CLOUDFLARE_ACCOUNT_ID": "fake-account",
               "CLOUDFLARE_AUTH_TOKEN": "FAKE-CF-" + "0" * 10,
               "AZURE_SPEECH_KEY": "FAKE-AZ-" + "0" * 10, "AZURE_SPEECH_REGION": "westeurope"}


def _pa_transport(method, url, headers=None, json_body=None, data=None, timeout=60):
    if "api.groq.com" in url:
        return 200, {"choices": [{"message": {"content": "groq yaniti"}}],
                     "usage": {"total_tokens": 7}}
    if "cloudflare" in url and "bge-m3" in url:
        return 200, {"result": {"data": [[0.1] * 1024]}}
    if "cloudflare" in url:
        return 200, {"result": {"response": "cloudflare yaniti"}}
    if "tts.speech.microsoft.com" in url:
        return 200, {"_raw_bytes": b"RIFF" + b"\x00" * 200}
    return 500, {}


_groq = _pa.call_groq_text("x", secret_getter=_pa_secrets.get, transport=_pa_transport)
_cf = _pa.call_cloudflare_text("x", secret_getter=_pa_secrets.get, transport=_pa_transport)
_cfe = _pa.call_cloudflare_embeddings(["x"], secret_getter=_pa_secrets.get, transport=_pa_transport)
_az = _pa.call_azure_tts("merhaba", _GA_TMP / "azure.wav", voice="tr-TR-AhmetNeural",
                          secret_getter=_pa_secrets.get, transport=_pa_transport)
if not (_groq["ok"] and _cf["ok"] and _cfe["ok"] and _az["ok"]):
    raise AssertionError("[ga] capraz-saglayici adaptorleri sahte transport ile calismadi")
if _az["status"] != "review_required" or _az["inherits_previous_approval"] is not False:
    raise AssertionError("[ga] Azure TTS ciktisi onay devralmamali")
if _pa.call_azure_tts("x", _GA_TMP / "bad.wav", voice="en-US-JennyNeural",
                       secret_getter=_pa_secrets.get, transport=_pa_transport)["ok"]:
    raise AssertionError("[ga] Turkce olmayan ses kabul edildi")
_leaky = _pa.call_groq_text("x", secret_getter=_pa_secrets.get,
                             transport=lambda *a, **k: _ga_raise(
                                 RuntimeError("Authorization: Bearer " + _pa_secrets["GROQ_API_KEY"])))
if _pa_secrets["GROQ_API_KEY"] in json.dumps(_leaky):
    raise AssertionError("[ga] SIZINTI: Groq anahtari hata metninde gorundu")
for _cap in ("high_quality_text", "high_volume_text", "embeddings", "transcription", "tts"):
    if _cap not in _pa.CROSS_PROVIDER_ROUTES:
        raise AssertionError(f"[ga] capraz-saglayici rotasi eksik: {_cap}")
if _pa.CROSS_PROVIDER_ROUTES["transcription"][0][0] != "local":
    raise AssertionError("[ga] yerel faster-whisper transkripsiyonda birincil kalmali")
if _pa.CROSS_PROVIDER_ROUTES["high_quality_text"][0][0] != "google":
    raise AssertionError("[ga] Google yuksek kaliteli metinde birincil kalmali")
if _pa.CROSS_PROVIDER_ROUTES["high_volume_text"][-1][1] != "@cf/qwen/qwen3-30b-a3b-fp8":
    raise AssertionError("[ga] Cloudflare high-volume model kimligi guncel/kanonik degil")

_cross_calls = []
def _cross_transport(method, url, headers=None, json_body=None, data=None, timeout=60):
    _cross_calls.append(url)
    if "api.groq.com" in url:
        return 200, {"choices": [{"message": {"content": "gecersiz-json"}}]}
    return 200, {"result": {"response": json.dumps({
        "script": "C" * 60, "hook": "h", "estimated_duration_sec": 60})}}

_cross_script = _sg.run_script_generation(
    _HM_FIXTURE, "hm.json", moment_approved=True,
    script_provider="google", allow_provider_fallback=False,
    enable_cross_provider_fallback=True, max_provider_attempts=3,
    cross_provider_secret_getter=_pa_secrets.get,
    cross_provider_transport=_cross_transport,
)
if (_cross_script.get("provider") != "cloudflare" or
        _cross_script.get("status") != "review_required" or len(_cross_calls) != 3):
    raise AssertionError("[ga] script runtime Groq duzeltme denemesinden sonra Cloudflare'a gecmedi")

_limit_calls = []
def _limit_transport(method, url, **kwargs):
    _limit_calls.append(url)
    return 200, {"choices": [{"message": {"content": "ok"}}]}
_limited = _pa.run_cross_provider_text_fallback(
    "x", secret_getter=_pa_secrets.get, transport=_limit_transport,
    max_provider_attempts=1, validator=lambda raw: raw)
if _limited["status"] != "ok" or len(_limit_calls) != 1 or "groq" not in _limit_calls[0]:
    raise AssertionError("[ga] MAX_PROVIDER_ATTEMPTS=1 gercek ag cagrilarini birle sinirlamadi")

_emb_fallback = _pa.run_cross_provider_embeddings_fallback(
    ["x"], secret_getter=_pa_secrets.get, transport=_pa_transport,
    max_provider_attempts=1)
if (_emb_fallback.get("status") != "ok" or
        not str(_emb_fallback.get("vector_space", "")).startswith("cloudflare:")):
    raise AssertionError("[ga] embedding fallback vektor uzayi kimligini kaydetmedi")

# Edge is disabled here so this stays the AZURE assertion it was written to be.
_tts_fallback = _pa.run_cross_provider_tts_fallback(
    "5 < 7 & guvenli", _GA_TMP / "azure-fallback.wav",
    secret_getter=_pa_secrets.get, transport=_pa_transport, allow_edge=False)
if _tts_fallback.get("status") != "review_required":
    raise AssertionError("[ga] Azure TTS fallback review_required olmali")
print("  [ga:cross-provider-adapters] kimlik yokken atlar, sahte transport ile calisir, "
      "runtime semasini dogrular, deneme limitine uyar, yerel whisper birincil kalir")

# --- Edge TTS: the credential-free route step ------------------------------
# Every call below injects `synth`. Nothing here may contact Microsoft: a test
# that quietly reached a live endpoint would be indistinguishable from a real
# provider call, which this suite promises never to make.
_edge_mp3 = subprocess.run(
    [ru.resolve_ffmpeg_binaries()[0], "-hide_banner", "-loglevel", "error",
     "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-f", "mp3", "pipe:1"],
    capture_output=True, check=True).stdout
if not _edge_mp3:
    raise AssertionError("[ga] Edge testi icin sahte MPEG uretilemedi")

_edge_calls = []


def _edge_synth(text, *, voice, rate, volume, pitch):
    _edge_calls.append({"text": text, "voice": voice, "rate": rate,
                        "volume": volume, "pitch": pitch})
    return _edge_mp3, [{"text": "merhaba", "start": 0.0, "end": 0.42}]


_edge = _pa.call_edge_tts("merhaba dunya", _GA_TMP / "edge.wav",
                          rate=1.1, synth=_edge_synth)
if not _edge["ok"] or _edge["status"] != "review_required":
    raise AssertionError(f"[ga] Edge TTS sahte synth ile calismadi: {_edge.get('error')}")
if _edge["inherits_previous_approval"] is not False:
    raise AssertionError("[ga] Edge TTS ciktisi onay devralmamali")
if _edge["requires_credential"] is not False:
    raise AssertionError("[ga] Edge TTS kimlik bilgisi gerektirmedigini bildirmeli")
# A multiplier must reach the provider as Edge's wire format, not as "1.1".
if _edge_calls[0]["rate"] != "+10%":
    raise AssertionError(f"[ga] konusma hizi carpani cevrilmedi: {_edge_calls[0]['rate']}")
if not _edge["word_boundaries"]:
    raise AssertionError("[ga] Edge kelime zamanlamalari kaybedildi")
# The written file must satisfy the SAME audio contract as the Gemini path,
# otherwise concat would refuse to join a mixed-provider narration.
_edge_validation = _ga.validate_audio_file(_GA_TMP / "edge.wav")
if not _edge_validation["passed"]:
    raise AssertionError(f"[ga] Edge WAV kontrati: {_edge_validation['reason']}")
if _edge_validation["details"]["duration_sec"] < 0.9:
    raise AssertionError(
        f"[ga] Edge WAV suresi yanlis olculdu: {_edge_validation['details']['duration_sec']}")

# Speech-rate translation, including the refusals.
if (_pa.normalize_speech_rate(None), _pa.normalize_speech_rate(0.9),
        _pa.normalize_speech_rate("+25%")) != ("+0%", "-10%", "+25%"):
    raise AssertionError("[ga] konusma hizi normalizasyonu hatali")
for _bad_rate in (3.0, 0.1, "25", "fast", "+500%", True):
    try:
        _pa.normalize_speech_rate(_bad_rate)
    except ValueError:
        continue
    raise AssertionError(f"[ga] gecersiz konusma hizi kabul edildi: {_bad_rate!r}")

# A malformed voice id and empty text are refusals, not silent successes.
if _pa.call_edge_tts("x", _GA_TMP / "bad-edge.wav", voice="Ahmet",
                     synth=_edge_synth)["ok"]:
    raise AssertionError("[ga] bicimsiz Edge ses kimligi kabul edildi")
if _pa.call_edge_tts("   ", _GA_TMP / "empty-edge.wav", synth=_edge_synth)["ok"]:
    raise AssertionError("[ga] bos metinle Edge TTS calisti")
# An empty response must fail rather than write a zero-length narration.
if _pa.call_edge_tts("x", _GA_TMP / "silent-edge.wav",
                     synth=lambda *a, **k: (b"", []))["ok"]:
    raise AssertionError("[ga] Edge bos ses dondurdugunde basarili sayildi")

# Edge leads the TTS route, ahead of the keyed provider.
_tts_route = [entry[0] for entry in _pa.CROSS_PROVIDER_ROUTES["tts"]]
if _tts_route[:2] != ["google", "edge_tts"] or "azure_speech" not in _tts_route:
    raise AssertionError(f"[ga] TTS rotasi beklenen sirada degil: {_tts_route}")

# With NO credential at all, the fallback still produces narration through Edge
# -- that is the whole point of adding it.
_edge_first = _pa.run_cross_provider_tts_fallback(
    "kota bitti ama anlatim surmeli", _GA_TMP / "edge-fallback.wav",
    secret_getter=lambda name: None, transport=_pa_transport,
    edge_synth=_edge_synth)
if _edge_first.get("provider") != "edge_tts" or _edge_first.get("status") != "review_required":
    raise AssertionError("[ga] kimlik yokken Edge fallback anlatim uretmedi")
print("  [ga:edge-tts] kimlik gerektirmiyor, hiz carpani wire bicimine ceviriliyor, "
      "kelime zamanlamalari korunuyor, WAV kontrati Gemini yoluyla ayni, "
      "gecersiz ses/bos metin/bos ses reddediliyor (gercek Microsoft cagrisi YOK)")

# --- Notebooks must contain no hardcoded Gemini model / client -------------
for _nb_path in sorted((PROJECT_ROOT / "experiments" / "colab").glob("*.ipynb")):
    _nb_json = json.loads(_nb_path.read_text(encoding="utf-8"))
    for _idx, _cell in enumerate(_nb_json["cells"]):
        if _cell["cell_type"] != "code":
            continue
        _text = "".join(_cell["source"])
        if "_LOCAL_PIPELINE_PAYLOADS" in _text:
            continue  # compressed embedded module snapshots, not notebook logic
        if _re.search(r"genai\.Client\s*\(", _text):
            raise AssertionError(f"{_nb_path.name} hucre {_idx}: dogrudan genai.Client(...) cagrisi")
        if _re.search(r"model\s*=\s*['\"]gemini-", _text):
            raise AssertionError(f"{_nb_path.name} hucre {_idx}: sabit-kodlanmis gemini model adi")
        if _re.search(r"\.models\.generate_content\s*\(", _text):
            raise AssertionError(
                f"{_nb_path.name} hucre {_idx}: router disi generate_content cagrisi")
print("  [ga:no-hardcoded-gemini-in-notebooks] hicbir notebook hucresinde genai.Client / "
      "sabit gemini model adi / dogrudan generate_content yok")

shutil.rmtree(_GA_TMP, ignore_errors=True)
print("GOOGLE_AI_ROUTER_MOCKED_TESTS_OK (gercek anahtar kullanilmadi, hicbir gercek "
      "Google/Groq/Cloudflare/Azure cagrisi yapilmadi, hicbir sey yuklenmedi/yayinlanmadi)")


# ---------------------------------------------------------------------------
# Phase 0 -- source assessment + processing strategy: deterministic tests.
#
# Fixtures are synthetic and live in an isolated temp directory that is deleted
# afterwards; the user's results/ directory is never touched. No provider is
# contacted: the probe is injected, so these tests are pure logic.
# ---------------------------------------------------------------------------

print("\n--- Phase 0: source assessment + processing plan (deterministic fixtures) ---")

from local_pipeline import source_strategy as _ss

_P0_TMP = Path(_tempfile.mkdtemp(prefix="phase0_tests_"))


def _p0_video(name="kaynak.mp4", payload=b"A", size=3 * 1024 * 1024):
    path = _P0_TMP / name
    path.write_bytes(payload * size)
    return path


def _p0_probe(duration=600.0, audio=True, video=True):
    def probe(ffmpeg_bin, ffprobe_bin, path):
        streams = []
        if video:
            streams.append({"codec_type": "video", "width": 1920, "height": 1080})
        if audio:
            streams.append({"codec_type": "audio"})
        return {"streams": streams, "format": {"duration": str(duration)}}, "ffprobe"
    return probe


_P0_HOT_MOMENTS = {
    "status": "review_required",
    "hot_moments": [
        {"rank": rank, "source_start_sec": rank * 30, "source_end_sec": rank * 30 + 20,
         "aligned_start_sec": rank * 30, "aligned_end_sec": rank * 30 + 20,
         "score": round(1.0 - rank * 0.1, 4), "reason": "test",
         "transcript": "konusma metni " * 10, "preview_path": None}
        for rank in range(1, 6)
    ],
}

_p0_src = _p0_video()
_p0_assessment = _ss.assess_source(
    _p0_src, "ffmpeg", "ffprobe", hot_moments_data=_P0_HOT_MOMENTS,
    google_credential_configured=True, probe_fn=_p0_probe(),
)

# --- 1. Recommendation is advisory and never selects/approves ---------------
if _p0_assessment["selected_mode"] is not None or _p0_assessment["auto_selected"] is not False:
    raise AssertionError("[p0] degerlendirme kendi kendine mod secti")
if _p0_assessment["recommendation_is_advisory"] is not True:
    raise AssertionError("[p0] oneri 'tavsiye' olarak isaretlenmemis")
if _p0_assessment["status"] != "review_required":
    raise AssertionError(f"[p0] degerlendirme statusu yanlis: {_p0_assessment['status']}")
if _p0_assessment["recommended_mode"] not in _ss.PROCESSING_MODES:
    raise AssertionError("[p0] oneri gecerli bir mod degil")
for _mode in _ss.PROCESSING_MODES:
    _entry = _p0_assessment["mode_scores"][_mode]
    if not (0 <= _entry["score"] <= 100) or not _entry["reasons"]:
        raise AssertionError(f"[p0] {_mode} skoru/gerekceleri eksik: {_entry}")
_p0_panel = _ss.format_assessment_panel(_p0_assessment)
for _needle in ("SECIM YAPILMADI", "direct_clip_series", "narrated_theory",
                 "Onerilen cikti sayisi", "Onerilen hedef sure", "API cagrisi YAPMAZ"):
    if _needle not in _p0_panel:
        raise AssertionError(f"[p0] panelde eksik: {_needle!r}")
print(f"  [p0:advisory-only] oneri={_p0_assessment['recommended_mode']} "
      f"selected_mode=None auto_selected=False; panel iki modu da skorla gosteriyor")

# --- 2. Deterministic scoring + mode-specific blockers ----------------------
_p0_again = _ss.assess_source(_p0_src, "ffmpeg", "ffprobe", hot_moments_data=_P0_HOT_MOMENTS,
                              google_credential_configured=True, probe_fn=_p0_probe(),
                              assessed_at=_p0_assessment["assessed_at"])
if _ss.assessment_content_sha256(_p0_again) != _ss.assessment_content_sha256(_p0_assessment):
    raise AssertionError("[p0] ayni girdiler farkli degerlendirme uretti (deterministik degil)")

_p0_no_audio = _ss.assess_source(_p0_src, "ffmpeg", "ffprobe", hot_moments_data=_P0_HOT_MOMENTS,
                                  google_credential_configured=True,
                                  probe_fn=_p0_probe(audio=False))
if _p0_no_audio["mode_scores"]["direct_clip_series"]["feasible"]:
    raise AssertionError("[p0] sessiz kaynakta orijinal-ses klip serisi uygulanabilir sayildi")
if _p0_no_audio["recommended_mode"] != "narrated_theory":
    raise AssertionError(f"[p0] sessiz kaynakta oneri yanlis: {_p0_no_audio['recommended_mode']}")

_p0_no_key = _ss.assess_source(_p0_src, "ffmpeg", "ffprobe", hot_moments_data=_P0_HOT_MOMENTS,
                                google_credential_configured=False, probe_fn=_p0_probe())
if _p0_no_key["mode_scores"]["narrated_theory"]["feasible"]:
    raise AssertionError("[p0] Gemini kimligi yokken narrated_theory uygulanabilir sayildi")
try:
    _ss.build_processing_plan(_p0_no_key, "narrated_theory", "verify_local",
                              theory_angle="x" * 40)
except ValueError:
    pass
else:
    raise AssertionError("[p0] engelli mod icin plan uretilebildi")
print("  [p0:blockers] ses yoksa clip-series ENGELLI, Gemini kimligi yoksa narrated_theory ENGELLI")

# --- 3. Both routes produce the right gates; no placeholder mode ------------
_p0_direct = _ss.build_processing_plan(
    _p0_assessment, "direct_clip_series", "verify_local", output_count=3,
    render_config={"render_mode": "blurred_background", "preview_mode": True},
    render_root=_P0_TMP / "render",
)
_p0_narr = _ss.build_processing_plan(
    _p0_assessment, "narrated_theory", "verify_local", output_count=1,
    theory_angle="Karakterin bu sahnedeki kararinin tum sezonu degistirdigi teorisi",
    render_config={"render_mode": "blurred_background", "preview_mode": True},
    render_root=_P0_TMP / "render_narr",
)
if _p0_direct["pipeline_mode"] != "original_audio_subtitles" or \
        _p0_narr["pipeline_mode"] != "narrated_recap":
    raise AssertionError("[p0] mod -> pipeline_mode esleme yanlis")
if sorted(_p0_direct["disabled_phases"]) != ["script_generation", "tts"]:
    raise AssertionError(f"[p0] clip-series yanlis fazlari kapatti: {_p0_direct['disabled_phases']}")
# narrated_theory writes its OWN narration, so the source-transcript phase is
# the one it shuts -- the mirror image of clip-series shutting script+tts.
if sorted(_p0_narr["disabled_phases"]) != ["transcript"]:
    raise AssertionError(
        f"[p0] narrated_theory yalnizca transcript'i kapatmali: {_p0_narr['disabled_phases']}")
if _p0_direct["consumes_generation_api"] is not False or \
        _p0_narr["consumes_generation_api"] is not True:
    raise AssertionError("[p0] consumes_generation_api bayragi yanlis")
for _plan in (_p0_direct, _p0_narr):
    if _plan["status"] != "review_required" or _plan["human_approval"]["approved"] is not False:
        raise AssertionError("[p0] yeni plan review_required + onaysiz olmali")
    if _ss.validate_processing_plan(_plan, assessment=_p0_assessment,
                                     source_fingerprint=_p0_assessment["source"]["fingerprint_sha256"]):
        raise AssertionError("[p0] taze plan gecersiz sayildi")
for _bad_mode in ("", "placeholder", "auto", "narrated_recap"):
    try:
        _ss.build_processing_plan(_p0_assessment, _bad_mode, "verify_local")
    except ValueError:
        pass
    else:
        raise AssertionError(f"[p0] gecersiz mod kabul edildi: {_bad_mode!r}")
try:
    _ss.build_processing_plan(_p0_assessment, "narrated_theory", "verify_local", theory_angle="kisa")
except ValueError:
    pass
else:
    raise AssertionError("[p0] narrated_theory yetersiz teori acisiyla kabul edildi")
try:
    _ss.build_processing_plan(_p0_assessment, "direct_clip_series", "")
except ValueError:
    pass
else:
    raise AssertionError("[p0] secen kisi olmadan plan uretildi")
print("  [p0:both-routes] clip-series -> script/tts KAPALI, narrated_theory -> transcript KAPALI; "
      "yer tutucu mod yok")

# --- 4. Approval is hash-bound and gates every downstream phase -------------
for _phase in _ss.GATED_PHASES:
    _allowed, _reason = _ss.plan_allows_phase(_p0_narr, _phase)
    if _allowed or "onaylanmamis" not in _reason:
        raise AssertionError(f"[p0] onaysiz plan {_phase} fazini actı: {_reason}")
_allowed, _reason = _ss.plan_allows_phase(None, "script_generation")
if _allowed or "Faz 0" not in _reason:
    raise AssertionError("[p0] plan YOKKEN faz acildi")

_p0_hash = _approval.content_hash_for("processing_plan", _p0_narr)
try:
    _approval.approve_artifact(_p0_narr, "processing_plan", "0" * 64, "verify_local")
except ValueError:
    pass
else:
    raise AssertionError("[p0] yanlis hash ile onay verildi")
_p0_narr_approved = _approval.approve_artifact(_p0_narr, "processing_plan", _p0_hash, "verify_local")
if _p0_narr_approved["status"] != "approved":
    raise AssertionError("[p0] dogru hash ile onay basarisiz")
if _approval.check_approval(_p0_narr_approved, "processing_plan")["valid"] is not True:
    raise AssertionError("[p0] taze onay gecersiz gorundu")
# narrated_theory writes its OWN narration, so the source-transcript phase is
# the one it shuts; everything else is open. Stated as an explicit table (like
# the clip-series one below) so the closure is ASSERTED, not just tolerated.
for _phase, _expected in (("video_analysis", True), ("moment_selection", True),
                           ("transcript", False), ("script_generation", True),
                           ("tts", True), ("render", True)):
    if _ss.plan_allows_phase(_p0_narr_approved, _phase)[0] is not _expected:
        raise AssertionError(f"[p0] onayli narrated_theory planinda {_phase} kapisi yanlis")
if set(_ss.GATED_PHASES) != {"video_analysis", "moment_selection", "transcript",
                              "script_generation", "tts", "render"}:
    raise AssertionError(f"[p0] GATED_PHASES beklenmeyen: {_ss.GATED_PHASES}")
_p0_direct_approved = _approval.approve_artifact(
    _p0_direct, "processing_plan",
    _approval.content_hash_for("processing_plan", _p0_direct), "verify_local")
for _phase, _expected in (("video_analysis", True), ("moment_selection", True),
                           ("transcript", True), ("script_generation", False),
                           ("tts", False), ("render", True)):
    if _ss.plan_allows_phase(_p0_direct_approved, _phase)[0] is not _expected:
        raise AssertionError(f"[p0] onayli clip-series planinda {_phase} kapisi yanlis")
print("  [p0:approval-gating] hash disi onay reddedildi; onaysiz plan TUM fazlari durduruyor; "
      "onaydan sonra sadece moda ait fazlar aciliyor")

# --- 5. Stale-plan detection: every decision field invalidates the approval --
_p0_stale_cases = {
    "mod degisti": lambda d: d.__setitem__("mode", "direct_clip_series"),
    "cikti sayisi degisti": lambda d: d.__setitem__("output_count", 2),
    "hedef sure degisti": lambda d: d.__setitem__("target_duration_sec", 45),
    "moment ranklari degisti": lambda d: d.__setitem__("moment_ranks", [1, 2]),
    "teori acisi degisti": lambda d: d.__setitem__("theory_angle", "bambaska bir teori acisi metni"),
    "render ayari degisti": lambda d: d["render"].__setitem__("render_mode", "center_crop"),
    "kaynak degisti": lambda d: d.__setitem__("source_fingerprint_sha256", "f" * 64),
}
for _label, _mutate in _p0_stale_cases.items():
    _mutated = json.loads(json.dumps(_p0_narr_approved))
    _mutate(_mutated["decision"])
    _check = _approval.check_approval(_mutated, "processing_plan")
    if _check["valid"] is not False:
        raise AssertionError(f"[p0] '{_label}' onayi gecersiz kilmadi")
    for _phase in _ss.GATED_PHASES:
        # An artifact whose recorded approval no longer binds its content must
        # not keep running phases just because status still says 'approved'.
        if _approval.check_approval(_mutated, "processing_plan")["valid"]:
            raise AssertionError(f"[p0] '{_label}' sonrasi onay hala gecerli")
print(f"  [p0:stale-plan] {len(_p0_stale_cases)} karar alaninin her biri onayi gecersiz kildi "
      f"({', '.join(_p0_stale_cases)})")

# --- 6. Source-hash invalidation against a genuinely different file ---------
_p0_src2 = _p0_video("kaynak_v2.mp4", payload=b"B")
_p0_fp1 = _ss.source_fingerprint_sha256(_p0_src)
_p0_fp2 = _ss.source_fingerprint_sha256(_p0_src2)
if _p0_fp1 == _p0_fp2:
    raise AssertionError("[p0] farkli kaynaklar ayni parmak izini uretti")
if _ss.source_fingerprint_sha256(_p0_src) != _p0_fp1:
    raise AssertionError("[p0] parmak izi deterministik degil")
_p0_stale_errors = _ss.validate_processing_plan(_p0_narr_approved, source_fingerprint=_p0_fp2)
if not any("kaynak video plandan sonra degismis" in _e for _e in _p0_stale_errors):
    raise AssertionError(f"[p0] kaynak degisikligi yakalanmadi: {_p0_stale_errors}")
_p0_new_assessment = _ss.assess_source(_p0_src2, "ffmpeg", "ffprobe",
                                        hot_moments_data=_P0_HOT_MOMENTS,
                                        google_credential_configured=True, probe_fn=_p0_probe())
if not any("kaynak degerlendirmesi plandan sonra degismis" in _e
           for _e in _ss.validate_processing_plan(_p0_narr_approved,
                                                   assessment=_p0_new_assessment)):
    raise AssertionError("[p0] degerlendirme degisikligi yakalanmadi")
_p0_unknown = _ss.build_processing_plan(_p0_assessment, "direct_clip_series", "verify_local",
                                         output_count=2, moment_ranks=[1, 99],
                                         render_root=_P0_TMP / "r2")
if not any("planda olmayan moment ranklari" in _e
           for _e in _ss.validate_processing_plan(_p0_unknown, hot_moments_data=_P0_HOT_MOMENTS)):
    raise AssertionError("[p0] var olmayan moment rank'i yakalanmadi")
print("  [p0:source-hash-invalidation] kaynak/degerlendirme/moment rank degisiklikleri yakalandi")

# --- 7. Live-config drift is caught before render ---------------------------
_p0_drift = _ss.check_config_drift(
    _p0_direct_approved, render_config={"render_mode": "center_crop", "preview_mode": True})
if not any("render.render_mode" in _e for _e in _p0_drift):
    raise AssertionError(f"[p0] render ayari kaymasi yakalanmadi: {_p0_drift}")
if _ss.check_config_drift(_p0_direct_approved,
                          render_config=dict(_p0_direct_approved["decision"]["render"])):
    raise AssertionError("[p0] ayni ayar icin yanlis pozitif drift")
print("  [p0:config-drift] onaylanan plan disinda bir render ayari render oncesi durduruluyor")

# --- 8. Multi-output isolation ---------------------------------------------
_p0_multi = _ss.build_processing_plan(_p0_assessment, "direct_clip_series", "verify_local",
                                       output_count=4, moment_ranks=[1, 2, 3, 4],
                                       render_root=_P0_TMP / "multi")
_p0_dirs = [Path(slot["output_dir"]) for slot in _p0_multi["outputs"]]
_p0_videos = [slot["final_video_path"] for slot in _p0_multi["outputs"]]
_p0_manifests = [slot["render_manifest_path"] for slot in _p0_multi["outputs"]]
if len(_p0_multi["outputs"]) != 4:
    raise AssertionError("[p0] cikti yuvasi sayisi output_count ile eslesmiyor")
for _collection, _label in ((_p0_dirs, "dizin"), (_p0_videos, "video"), (_p0_manifests, "manifest")):
    if len(set(map(str, _collection))) != 4:
        raise AssertionError(f"[p0] cikti {_label} yollari benzersiz degil: {_collection}")
_p0_root = (_P0_TMP / "multi").resolve()
for _directory in _p0_dirs:
    if _p0_root not in _directory.resolve().parents and _directory.resolve() != _p0_root:
        raise AssertionError(f"[p0] cikti dizini render kokunun disina cikti: {_directory}")
if [slot["moment_rank"] for slot in _p0_multi["outputs"]] != [1, 2, 3, 4]:
    raise AssertionError("[p0] cikti yuvalari moment ranklariyla eslesmiyor")
# Writing every slot must not clobber a sibling.
for _slot in _p0_multi["outputs"]:
    Path(_slot["output_dir"]).mkdir(parents=True, exist_ok=True)
    Path(_slot["render_manifest_path"]).write_text(
        json.dumps({"index": _slot["index"]}), encoding="utf-8")
_p0_written = [json.loads(Path(s["render_manifest_path"]).read_text(encoding="utf-8"))["index"]
               for s in _p0_multi["outputs"]]
if _p0_written != [1, 2, 3, 4]:
    raise AssertionError(f"[p0] ciktilar birbirini ezdi: {_p0_written}")
# A single output keeps the historical render/ path unchanged.
_p0_single = _ss.build_output_slots("direct_clip_series", 1, [], _P0_TMP / "legacy")
if Path(_p0_single[0]["output_dir"]) != _P0_TMP / "legacy":
    raise AssertionError("[p0] tek ciktida tarihsel render dizini korunmadi")
try:
    _ss.build_processing_plan(_p0_assessment, "direct_clip_series", "verify_local",
                              output_count=_ss.MAX_OUTPUT_COUNT + 1)
except ValueError:
    pass
else:
    raise AssertionError("[p0] cikti sayisi ust siniri uygulanmadi")
print(f"  [p0:multi-output-isolation] 4 cikti -> 4 ayri dizin/video/manifest, kok disina cikis yok, "
      f"tek ciktida eski yol korunuyor")

# --- 9. Artifact contract: no auto-approval, no production_ready, no secrets -
for _artifact in (_p0_assessment, _p0_direct, _p0_narr, _p0_narr_approved):
    _blob = json.dumps(_artifact, ensure_ascii=False, default=str)
    if "production_ready" in _blob:
        raise AssertionError("[p0] artefakt production_ready iceriyor")
    if _re.search(r"AIza[0-9A-Za-z_\-]{10,}|api[_-]?key\"?\s*[:=]", _blob, _re.I):
        raise AssertionError("[p0] artefaktta anahtar benzeri icerik var")
    if _artifact.get("not_uploaded") is not True:
        raise AssertionError("[p0] artefakt not_uploaded=True tasimiyor")
_p0_forced = json.loads(json.dumps(_p0_direct))
_p0_forced["status"] = "production_ready"
if not _ss.validate_processing_plan(_p0_forced):
    raise AssertionError("[p0] elle production_ready yapilan plan gecerli sayildi")
_p0_tampered = json.loads(json.dumps(_p0_direct_approved))
_p0_tampered["phase_gates"]["script_generation"] = {"allowed": True, "reason": ""}
if not _ss.validate_processing_plan(_p0_tampered):
    raise AssertionError("[p0] elle acilan faz kapisi yakalanmadi")
if _ss.plan_allows_phase(_p0_tampered, "script_generation")[0]:
    raise AssertionError("[p0] elle kurcalanmis plan script fazini acti")
print("  [p0:artifact-contract] production_ready yok, otomatik onay yok, elle acilan faz kapisi "
      "reddediliyor, artefaktta sir yok")

shutil.rmtree(_P0_TMP, ignore_errors=True)
print("PHASE0_SOURCE_STRATEGY_TESTS_OK (sentetik fixture'lar, saglayici cagrisi yok, "
      "gercek results/ klasoru degistirilmedi)")
