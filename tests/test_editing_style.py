import json
import tempfile
import unittest
from pathlib import Path

import editing_style


class EditingStyleTests(unittest.TestCase):
    def test_merge_ranges_merges_nearby_intervals(self):
        self.assertEqual(editing_style._merge_ranges([(2, 3), (1, 2.001), (4, 5)]), [(1.0, 3.0), (4.0, 5.0)])

    def test_profile_does_not_enable_automation_from_one_reference(self):
        profile = editing_style.default_profile()
        report = {
            "removed_segments": [{"classification": "quiet_candidate", "duration_ms": 500, "rms_db": -42}],
            "silence_observations": {"pre_roll_ms": 70, "post_roll_ms": 90},
            "subtitle_comparison": {"removed_words": []},
        }
        editing_style.update_profile(profile, report)
        self.assertEqual(profile["reference_count"], 1)
        self.assertFalse(profile["automation_enabled"])

    def test_reanalysis_of_same_timeline_does_not_increase_reference_count(self):
        profile = editing_style.default_profile()
        report = {"capcut_timeline": "same", "removed_segments": [], "silence_observations": {}, "subtitle_comparison": {"removed_words": []}}
        editing_style.update_profile(profile, report)
        editing_style.update_profile(profile, report)
        self.assertEqual(profile["reference_count"], 1)

    def test_manual_word_rules_are_explicit_and_persistent_in_profile(self):
        profile = editing_style.default_profile()
        added = editing_style.add_manual_removed_words(profile, "um\nuh\num")
        self.assertEqual(added, ["um", "uh"])
        self.assertEqual(profile["removed_words"]["um"]["source"], "manual")
        self.assertEqual(profile["removed_words"]["uh"]["confidence"], 1.0)

    def test_subtitle_comparison_records_found_and_removed_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, edited = root / "original.srt", root / "edited.srt"
            original.write_text("1\n00:00:00,000 --> 00:00:01,000\num hello world\n", encoding="utf-8")
            edited.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello world\n", encoding="utf-8")
            comparison = editing_style.compare_subtitles(original, edited)
        self.assertEqual(comparison["removed_words"], [{"word": "um", "found": 1, "removed": 1}])

    def test_ass_subtitles_are_read_for_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, edited = root / "original.ass", root / "edited.ass"
            header = "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            original.write_text(header + "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,um hello\n", encoding="utf-8")
            edited.write_text(header + "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,hello\n", encoding="utf-8")
            comparison = editing_style.compare_subtitles(original, edited)
        self.assertEqual(comparison["removed_words"][0], {"word": "um", "found": 1, "removed": 1})


if __name__ == "__main__":
    unittest.main()
