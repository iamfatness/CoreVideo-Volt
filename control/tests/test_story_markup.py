"""Script markup, counts and read time: the pure functions in volt/story.py, driven by one golden fixture.

The same fixture drives the JavaScript twin (operator.js scriptStats) in test_operator_ui.py, so the two cannot drift.
"""

import json
import unittest
from pathlib import Path

from volt import story

FIXTURE = json.loads((Path(__file__).parent / "golden" / "script_markup.json").read_text(encoding="utf-8"))
CASES = {c["name"]: c for c in FIXTURE["cases"]}


class Markup(unittest.TestCase):
    def stats(self, name):
        c = CASES[name]
        return story.script_stats(c["text"], c["wpm"])

    def test_every_fixture_case_matches_stats_blocks_and_read_text(self):
        for name, c in CASES.items():
            with self.subTest(case=name):
                self.assertEqual(story.script_stats(c["text"], c["wpm"]), c["stats"])
                got = [{"kind": b.kind, "text": b.text, "para": b.para} for b in story.parse_script(c["text"])]
                self.assertEqual(got, c["blocks"])
                self.assertEqual(story.read_text(c["text"]), c["readText"])

    def test_instruction_not_counted(self):
        s = self.stats("instruction_not_counted")
        self.assertEqual((s["words"], s["instructions"]), (0, 1))

    def test_private_note_excluded(self):
        s = self.stats("private_note_excluded")
        self.assertEqual((s["words"], s["notes"]), (0, 1))
        self.assertEqual(story.read_text(CASES["private_note_excluded"]["text"]), "")
        self.assertNotIn("mayor", story.read_text("Hello.\n// call the mayor"))

    def test_unclosed_bracket_is_read_and_warned(self):
        s = self.stats("unclosed_bracket_is_read_and_warned")
        self.assertEqual(s["warnings"], ["unclosed_bracket"])
        self.assertEqual(s["words"], 4)

    def test_two_unclosed_brackets_warn_once(self):
        self.assertEqual(story.script_stats("[a\n[b")["warnings"], ["unclosed_bracket"])

    def test_paragraphs(self):
        blocks = story.parse_script(CASES["paragraphs"]["text"])
        self.assertEqual([b.para for b in blocks], [0, 0, 1])

    def test_read_seconds_rounds(self):
        self.assertEqual(self.stats("read_seconds_rounds_to_a_minute")["readSeconds"], 60)
        self.assertEqual(story.script_stats("one two three", 160)["readSeconds"], 1)   # 1.125 s
        self.assertEqual(self.stats("half_second_rounds_up")["readSeconds"], 1)         # exactly 0.5 s rounds up

    def test_crlf_and_blank_lines(self):
        blocks = story.parse_script(CASES["crlf_and_blank_lines"]["text"])
        self.assertEqual([(b.text, b.para) for b in blocks], [("A b", 0), ("C d", 1)])

    def test_empty_script_is_zero(self):
        self.assertEqual(story.script_stats(""), {"words": 0, "readSeconds": 0, "instructions": 0, "notes": 0, "warnings": []})
        self.assertEqual(story.script_stats("\n\n  \n"), story.script_stats(""))
        self.assertEqual(story.parse_script(""), [])

    def test_wpm_out_of_range_raises_value_error(self):
        for bad in (59, 401, 0, -1, 160.5, "160", None, True):
            with self.subTest(wpm=bad), self.assertRaises(ValueError):
                story.script_stats("hello", bad)
        for good in (60, 400):
            story.script_stats("hello", good)

    def test_defaults(self):
        self.assertEqual(story.DEFAULT_WPM, 160)
        self.assertEqual(story.WPM_RANGE, (60, 400))
        self.assertEqual(story.script_stats("a b c d")["readSeconds"], story.script_stats("a b c d", 160)["readSeconds"])

    def test_unicode_words_counted_by_whitespace(self):
        self.assertEqual(self.stats("unicode_words_by_whitespace")["words"], 4)

    def test_a_none_script_is_empty(self):
        self.assertEqual(story.script_stats(None)["words"], 0)


if __name__ == "__main__":
    unittest.main()
