"""FCPXML 1.10 export (workstream E): the builder is pure; times are exact rational seconds."""
import unittest
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

from tests.test_nle import R25, R2997, R30, item, source
from volt import nle

GOLDEN = Path(__file__).parent / "golden" / "nle_fcpxml.xml"
R50, R5994, R60 = Fraction(50), Fraction(60000, 1001), Fraction(60)


def parse(content):
    return ET.fromstring(content.split("\n", 2)[2])


def seconds(text):
    t = text[:-1]
    return Fraction(t)


class Builder(unittest.TestCase):
    def build(self, *items, **kw):
        return nle.build_fcpxml(list(items), **kw)

    def test_golden_file(self):
        s = source(duration=1000)
        built = self.build(item(s, "01:00:02:00", "01:00:04:00", "s1", "A & B"), item(s, "01:00:10:00", "01:00:11:12", "s2", "Two"), name="Final <Four>")
        self.assertEqual(built.content, GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual((built.mime, built.extension), ("application/xml", "fcpxml"))

    def test_well_formed_and_shaped(self):
        s = source(duration=1000)
        root = parse(self.build(item(s, "01:00:02:00", "01:00:04:00")).content)
        self.assertEqual((root.tag, root.get("version")), ("fcpxml", "1.10"))
        self.assertEqual([c.tag for c in root], ["resources", "library"])
        self.assertEqual([c.tag for c in root.find("resources")], ["format", "asset"])
        self.assertEqual(len(root.findall(".//spine/asset-clip")), 1)
        self.assertIsNotNone(root.find(".//asset/media-rep[@kind='original-media']"))

    def test_times_are_exact_at_every_rate(self):
        for rate, drop, start in ((R25, False, "01:00:00:00"), (R2997, True, "01:00:00;00"), (R30, False, "01:00:00:00"),
                                  (R50, False, "01:00:00:00"), (R5994, True, "01:00:00;00"), (R60, False, "01:00:00:00")):
            s = source(rate=rate, drop=drop, start=start, duration=100000)
            first, last = item(s, start, start.replace(":00:00", ":00:01") if not drop else "01:00:01;00", "a"), None
            built = self.build(first)
            root = parse(built.content)
            clip = root.find(".//asset-clip")
            frame = 1 / rate
            self.assertEqual(seconds(root.find(".//format").get("frameDuration")), frame, rate)
            self.assertEqual(seconds(clip.get("duration")), first.length * frame, rate)
            self.assertEqual(seconds(clip.get("start")), first.frame_in * frame, rate)
            self.assertEqual(seconds(root.find(".//sequence").get("duration")), built.duration_frames * frame, rate)
            self.assertEqual(clip.get("tcFormat"), "DF" if drop else "NDF", rate)
            self.assertEqual(root.find(".//sequence").get("tcFormat"), "DF" if drop else "NDF", rate)

    def test_clips_are_butted_and_share_one_asset(self):
        s = source(duration=100000)
        root = parse(self.build(item(s, "01:00:00:00", "01:00:01:00", "a"), item(s, "01:05:00:00", "01:05:02:00", "b")).content)
        clips = root.findall(".//asset-clip")
        self.assertEqual([c.get("offset") for c in clips], ["0s", "1s"])
        self.assertEqual({c.get("ref") for c in clips}, {"r2"})
        self.assertEqual(len(root.findall(".//asset")), 1)

    def test_hostile_names_cannot_add_elements(self):
        s = source(duration=1000)
        bad = 'x"/><evil a="1'
        built = self.build(item(s, "01:00:00:00", "01:00:01:00", "a", bad), name="</project><evil/>")
        root = parse(built.content)
        self.assertEqual(root.findall(".//evil"), [])
        self.assertEqual(root.find(".//asset-clip").get("name"), bad)

    def test_no_audio_layout_is_flagged(self):
        s = source(duration=1000, channels=None)
        built = self.build(item(s, "01:00:00:00", "01:00:01:00"))
        self.assertTrue(any("audio" in w for w in built.warnings))
        self.assertIsNone(parse(built.content).find(".//asset").get("hasAudio"))

    def test_mixed_rates_and_empty_selection_are_refused(self):
        a, b = source(rate=R25), source(rate=R30, asset="bbbbbbbb-2222")
        with self.assertRaises(nle.NleError):
            self.build(item(a, "01:00:00:00", "01:00:01:00"), item(b, "01:00:00:00", "01:00:01:00"))
        with self.assertRaises(nle.NleError):
            nle.build_fcpxml([])

    def test_a_drive_path_and_a_unc_share_become_file_urls(self):
        d = source(duration=1000, path="D:\iso\cam 1.mxf")
        u = source(duration=1000, path=r"\\nas\iso\cam1.mxf", asset="bbbbbbbb-2222")
        src = lambda s: parse(self.build(item(s, "01:00:00:00", "01:00:01:00")).content).find(".//media-rep").get("src")
        self.assertEqual(src(d), "file:///D%3a/iso/cam%201.mxf")
        self.assertEqual(src(u), "file://nas/iso/cam1.mxf")

    def test_dispatch_and_request(self):
        s = source(duration=1000)
        self.assertEqual(nle.build("fcpxml", [item(s, "01:00:00:00", "01:00:01:00")]).extension, "fcpxml")
        self.assertIn("fcpxml", nle.FORMATS)
        with self.assertRaises(nle.NleError) as ctx:
            nle.build("aaf", [item(s, "01:00:00:00", "01:00:01:00")])
        self.assertIn("fcpxml", ctx.exception.reason if hasattr(ctx.exception, "reason") else str(ctx.exception))
        self.assertEqual(nle.parse_request({"format": "fcpxml", "spans": ["x"]})["format"], "fcpxml")
        self.assertEqual(nle.filename("Final Four", "fcpxml"), "Final_Four.fcpxml")


if __name__ == "__main__":
    unittest.main()
