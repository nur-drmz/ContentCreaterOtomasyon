from __future__ import annotations

import unittest
from unittest import mock

from local_pipeline import render_utils, subtitle_edit


SRT = """1
00:00:00,000 --> 00:00:01,500
First caption

2
00:00:01,500 --> 00:00:03,000
Second caption
"""


class SubtitleEditTests(unittest.TestCase):
    def test_srt_edit_preserves_timestamps(self):
        cues = subtitle_edit.parse_srt(SRT)
        public = subtitle_edit.public_cues(cues)
        edited, changed = subtitle_edit.apply_text_edits(
            cues, [{"id": public[0]["id"], "text": "Corrected caption"}])

        self.assertEqual(changed, [public[0]["id"]])
        self.assertEqual(edited[0], {
            "start": 0.0, "end": 1.5, "text": "Corrected caption"})
        self.assertEqual(edited[1], cues[1])

    def test_render_prefers_human_track(self):
        cues = subtitle_edit.parse_srt(SRT)
        with mock.patch.object(
                render_utils, "transcribe_for_subtitles",
                side_effect=AssertionError("human track must bypass transcription")):
            resolved, source = render_utils.resolve_narrated_subtitle_cues(
                "unused.wav", "unused script", 42, subtitle_cues_override=cues)

        self.assertEqual(resolved, cues)
        self.assertEqual(source, "human-edited-track")

    def test_empty_or_unknown_edits_are_rejected(self):
        cues = subtitle_edit.parse_srt(SRT)
        public = subtitle_edit.public_cues(cues)
        with self.assertRaises(subtitle_edit.SubtitleEditError):
            subtitle_edit.apply_text_edits(
                cues, [{"id": public[0]["id"], "text": ""}])
        with self.assertRaises(subtitle_edit.SubtitleEditError):
            subtitle_edit.apply_text_edits(
                cues, [{"id": "cue_unknown", "text": "x"}])


if __name__ == "__main__":
    unittest.main()
