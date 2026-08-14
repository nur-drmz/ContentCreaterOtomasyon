"""Scene Editor / EDL tests."""
from __future__ import annotations

import pytest

from local_pipeline import scene_editor


def test_default_edl_is_valid():
    edl = scene_editor.build_edl("source.mp4", 60.0, title="Test")
    assert edl.total_duration() == 60.0
    assert edl.is_valid({"source.mp4": 60.0})
    assert edl.clips[0].clip_id == "clip_001"


def test_edl_rejects_overtrim():
    edl = scene_editor.build_edl("source.mp4", 60.0)
    with pytest.raises(ValueError, match="kaynak suresini asamaz"):
        scene_editor.trim_clip(edl.clips[0], 10.0, 70.0, source_duration=60.0)


def test_crop_validates_relative_box():
    clip = scene_editor.build_edl("source.mp4", 60.0).clips[0]
    cropped = scene_editor.crop_clip(clip, 0.25, 0.25, 0.5, 0.5)
    assert cropped.crop.is_valid()


def test_crop_rejects_out_of_bounds():
    clip = scene_editor.build_edl("source.mp4", 60.0).clips[0]
    with pytest.raises(ValueError, match="crop alani"):
        scene_editor.crop_clip(clip, 0.5, 0.5, 0.6, 0.6)


def test_zoom_validates_scale_and_anchor():
    clip = scene_editor.build_edl("source.mp4", 60.0).clips[0]
    zoomed = scene_editor.zoom_clip(clip, 1.5, 0.5, 0.5)
    assert zoomed.zoom.is_valid()


def test_zoom_rejects_invalid_anchor():
    clip = scene_editor.build_edl("source.mp4", 60.0).clips[0]
    with pytest.raises(ValueError, match="anchor"):
        scene_editor.zoom_clip(clip, 1.2, 1.5, 0.5)


def test_caption_style_normalizes():
    clip = scene_editor.build_edl("source.mp4", 60.0).clips[0]
    style = scene_editor.CaptionStyle(preset="classic_white", font_size=28)
    updated = scene_editor.set_caption_style(clip, style)
    assert updated.caption_style.preset == "classic_white"


def test_caption_style_rejects_unknown():
    clip = scene_editor.build_edl("source.mp4", 60.0).clips[0]
    with pytest.raises(ValueError, match="bilinmeyen caption"):
        scene_editor.set_caption_style(clip,
                                       scene_editor.CaptionStyle(preset="funky"))


def test_b_roll_added():
    clip = scene_editor.build_edl("source.mp4", 60.0).clips[0]
    b_roll = scene_editor.BRoll(media_path="broll.mp4", start_sec=0.0, duration_sec=5.0)
    updated = scene_editor.add_b_roll(clip, b_roll)
    assert len(updated.b_roll) == 1


def test_music_track_set():
    edl = scene_editor.build_edl("source.mp4", 60.0)
    edl = scene_editor.set_music_track(edl, "music.mp3")
    assert edl.music_track == "music.mp3"


def test_preview_filtergraph_generated():
    edl = scene_editor.build_edl("source.mp4", 60.0)
    graph, total_inputs = scene_editor.build_preview_filtergraph(edl)
    assert "crop=" in graph
    assert "[vout]" in graph
    assert total_inputs == 1


def test_preview_filtergraph_multi_clip_and_broll():
    edl = scene_editor.build_edl("source.mp4", 60.0)
    clip2 = scene_editor.EDLClip(
        clip_id="clip_002", source_path="source2.mp4",
        source_start_sec=0.0, source_end_sec=30.0,
    )
    edl = scene_editor.add_clip_to_edl(edl, clip2)
    broll = scene_editor.BRoll(
        media_path="broll.mp4", start_sec=0.0, duration_sec=5.0,
    )
    edl.clips[0] = scene_editor.add_b_roll(edl.clips[0], broll)
    graph, total_inputs = scene_editor.build_preview_filtergraph(edl)
    assert "concat" in graph
    assert "overlay" in graph
    assert total_inputs == 3  # 2 clips + 1 broll


def test_preview_command_built():
    edl = scene_editor.build_edl("source.mp4", 60.0)
    cmd = scene_editor.build_preview_command(edl, "preview.mp4")
    assert "ffmpeg" in cmd
    assert "-filter_complex" in cmd
    assert "preview.mp4" in cmd[-1]


def test_render_preview_calls_ffmpeg(mocker, tmp_path):
    edl = scene_editor.build_edl("source.mp4", 60.0)
    out = tmp_path / "preview.mp4"
    fake = mocker.MagicMock()
    fake.returncode = 0
    fake.stdout = "done"
    fake.stderr = ""
    mocker.patch("subprocess.run", return_value=fake)
    result = scene_editor.render_preview(edl, out)
    assert result["returncode"] == 0
    assert result["command"][0] == "ffmpeg"
