"""NLE export (slice 6 of docs/specs/shot-spans.md): xmeml and CMX3600 builders, and POST /api/exports/nle.

The arithmetic tests use hand-computed frame numbers (not the timecode library's own output) as the oracle where
they can: one hour of 29.97 drop-frame is 107892 frames, ten minutes is 17982, a drop-frame minute boundary
skips labels ;00 and ;01. No NLE is needed for any of them. Whether Premiere or Resolve accept the xmeml is
NOT tested here; see the manual check in the spec.

The API half runs on SQLite always and on Postgres when VOLT_TEST_PG_DSN is set."""

import hashlib
import http.client
import json
import os
import re
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from fractions import Fraction
from http.server import ThreadingHTTPServer
from pathlib import Path

from volt import nle, serve, shots, timecode
from volt.store import Store

from tests.pgutil import PgCase

R25, R24, R30, R2997, R2398 = Fraction(25), Fraction(24), Fraction(30), Fraction(30000, 1001), Fraction(24000, 1001)


def source(rate=R25, start="01:00:00:00", drop=False, duration=None, channels=2, asset="aaaaaaaa-1111",
           path="D:\\iso\\cam1.mxf", key=None):
    return nle.Source(key or f"{asset}:hi-res", asset, path, rate, drop,
                      timecode.tc_to_frames(start, rate, drop), duration, channels)


def item(src, tc_in, tc_out, sid="s1", name=None, text=""):
    fi = timecode.tc_to_frames(tc_in, src.rate, src.drop)
    fo = timecode.tc_to_frames(tc_out, src.rate, src.drop)
    return nle.Item(sid, src, fi, fo, name or sid, f"asset {src.asset_id} span {sid}", text)


def parse_xmeml(content):
    return ET.fromstring(content.split("\n", 2)[2])        # drop the XML declaration and DOCTYPE lines


def tx(el, path):
    found = el.find(path)
    return None if found is None else found.text


def video_clips(root):
    return root.findall("./sequence/media/video/track/clipitem")


def audio_tracks(root):
    return root.findall("./sequence/media/audio/track")


class Xmeml(unittest.TestCase):
    def build(self, items, **kw):
        built = nle.build_xmeml(items, **kw)
        return built, parse_xmeml(built.content)

    def test_well_formed_with_declaration_and_doctype(self):
        built, root = self.build([item(source(), "01:00:00:00", "01:00:02:00")])
        self.assertTrue(built.content.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4">'))
        self.assertEqual((root.tag, root.get("version")), ("xmeml", "4"))
        self.assertEqual(len(root.findall("sequence")), 1)

    def test_25fps_exact_in_out_and_butted_positions(self):
        src = source(R25, "01:00:00:00")
        built, root = self.build([item(src, "01:00:02:00", "01:00:04:12", "a"),     # 50 .. 112  (62 frames)
                                  item(src, "01:00:10:00", "01:00:10:01", "b"),     # 250 .. 251 (1 frame)
                                  item(src, "01:00:00:00", "01:00:01:00", "c")])    # 0 .. 25
        got = [(tx(c, "in"), tx(c, "out"), tx(c, "start"), tx(c, "end"), tx(c, "duration")) for c in video_clips(root)]
        self.assertEqual(got, [("50", "112", "0", "62", "62"), ("250", "251", "62", "63", "1"),
                               ("0", "25", "63", "88", "25")])
        self.assertEqual(tx(root, "sequence/duration"), "88")
        self.assertEqual(built.duration_frames, 88)
        self.assertEqual((tx(root, "sequence/rate/timebase"), tx(root, "sequence/rate/ntsc")), ("25", "FALSE"))
        self.assertEqual((tx(root, "sequence/timecode/string"), tx(root, "sequence/timecode/frame"),
                          tx(root, "sequence/timecode/displayformat")), ("01:00:00:00", "90000", "NDF"))

    def test_24_and_30_and_2398(self):
        for rate, base, ntsc, hour in ((R24, "24", "FALSE", 86400), (R30, "30", "FALSE", 108000),
                                       (R2398, "24", "TRUE", 86400)):
            with self.subTest(rate=str(rate)):
                src = source(rate)
                built, root = self.build([item(src, "01:00:01:05", "01:00:03:00")])
                n = base and int(base)
                c = video_clips(root)[0]
                self.assertEqual((tx(c, "in"), tx(c, "out")), (str(n + 5), str(3 * n)))
                self.assertEqual((tx(root, "sequence/rate/timebase"), tx(root, "sequence/rate/ntsc")), (base, ntsc))
                self.assertEqual(tx(root, "sequence/timecode/frame"), str(hour))
                self.assertEqual(tx(root, "sequence/timecode/displayformat"), "NDF")

    def test_2997_non_drop(self):
        src = source(R2997, "01:00:00:00", drop=False)
        built, root = self.build([item(src, "01:01:00:00", "01:01:00:15")])
        c = video_clips(root)[0]
        self.assertEqual((tx(c, "in"), tx(c, "out")), ("1800", "1815"))    # non-drop counts every label
        self.assertEqual((tx(root, "sequence/rate/timebase"), tx(root, "sequence/rate/ntsc")), ("30", "TRUE"))
        self.assertEqual(tx(root, "sequence/timecode/displayformat"), "NDF")
        self.assertEqual(tx(root, "sequence/timecode/frame"), "108000")

    def test_2997_drop_frame_is_exact(self):
        src = source(R2997, "01:00:00;00", drop=True)
        self.assertEqual(src.start, 107892)                                # 1 h of drop-frame, by hand
        built, root = self.build([
            item(src, "01:00:10;00", "01:00:12;15", "a"),                  # 300 .. 375
            item(src, "01:01:00;02", "01:01:05;00", "b"),                  # labels ;00 ;01 do not exist: 1800 .. 1948
            item(src, "01:10:00;00", "01:10:00;10", "c")])                 # tenth minute keeps ;00: 17982 .. 17992
        got = [(tx(c, "in"), tx(c, "out"), tx(c, "start"), tx(c, "end")) for c in video_clips(root)]
        self.assertEqual(got, [("300", "375", "0", "75"), ("1800", "1948", "75", "223"),
                               ("17982", "17992", "223", "233")])
        # default start_tc follows the essence: drop-frame sequence, `;` label, drop-aware frame count
        self.assertEqual((tx(root, "sequence/timecode/string"), tx(root, "sequence/timecode/frame"),
                          tx(root, "sequence/timecode/displayformat")), ("01:00:00;00", "107892", "DF"))
        self.assertEqual((tx(root, "sequence/rate/timebase"), tx(root, "sequence/rate/ntsc")), ("30", "TRUE"))
        f = root.find("./sequence/media/video/track/clipitem/file")
        self.assertEqual((tx(f, "timecode/string"), tx(f, "timecode/frame"), tx(f, "timecode/displayformat")),
                         ("01:00:00;00", "107892", "DF"))

    def test_drop_frame_minute_boundary_is_one_frame(self):
        src = source(R2997, "01:00:00;00", drop=True)
        # ;29 of one minute to ;02 of the next: the labels between do not exist, so this is a single frame
        built, root = self.build([item(src, "01:00:59;29", "01:01:00;02")])
        c = video_clips(root)[0]
        self.assertEqual((int(tx(c, "out")) - int(tx(c, "in")), tx(c, "duration")), (1, "1"))
        self.assertEqual(tx(c, "in"), "1799")

    def test_explicit_start_tc_decides_drop_frame_by_its_separator(self):
        src = source(R2997, "01:00:00;00", drop=True)
        i = item(src, "01:00:00;00", "01:00:01;00")
        _, root = self.build([i], start_tc="00:59:58:00")
        self.assertEqual((tx(root, "sequence/timecode/displayformat"), tx(root, "sequence/timecode/frame")),
                         ("NDF", str((59 * 60 + 58) * 30)))
        _, root = self.build([i], start_tc="10:00:00;00")
        self.assertEqual((tx(root, "sequence/timecode/displayformat"), tx(root, "sequence/timecode/frame")),
                         ("DF", "1078920"))                         # 10 h drop-frame = 1,078,920 frames
        with self.assertRaises(nle.NleError) as e:
            nle.build_xmeml([item(source(R25), "01:00:00:00", "01:00:01:00")], start_tc="01:00:00;00")
        self.assertEqual((e.exception.status, e.exception.code), (422, "bad_input"))
        with self.assertRaises(nle.NleError):
            nle.build_xmeml([i], start_tc="01:00:99:00")

    def test_single_frame_span(self):
        src = source()
        _, root = self.build([item(src, "01:00:05:10", "01:00:05:11")])
        c = video_clips(root)[0]
        self.assertEqual((tx(c, "in"), tx(c, "out"), tx(c, "start"), tx(c, "end"), tx(c, "duration")),
                         ("135", "136", "0", "1", "1"))

    def test_span_at_essence_end(self):
        src = source(duration=250)                                         # frames 90000 .. 90250
        _, root = self.build([item(src, "01:00:09:00", "01:00:10:00")])
        c = video_clips(root)[0]
        self.assertEqual((tx(c, "in"), tx(c, "out")), ("225", "250"))
        self.assertEqual(tx(c, "file/duration"), "250")

    def test_same_shot_twice_is_two_clips_and_one_file_definition(self):
        src = source()
        built, root = self.build([item(src, "01:00:01:00", "01:00:02:00", "x"),
                                  item(src, "01:00:01:00", "01:00:02:00", "x")])
        a, b = video_clips(root)
        self.assertEqual([(tx(c, "in"), tx(c, "out"), tx(c, "start"), tx(c, "end")) for c in (a, b)],
                         [("25", "50", "0", "25"), ("25", "50", "25", "50")])
        self.assertNotEqual(a.get("id"), b.get("id"))
        self.assertGreater(len(a.find("file")), 0)                         # first use: the full definition
        self.assertEqual(len(b.find("file")), 0)                           # later use: a bare reference
        self.assertEqual(a.find("file").get("id"), b.find("file").get("id"))
        self.assertEqual(tx(root, "sequence/duration"), "50")

    def test_two_sources_get_two_file_ids_defined_once(self):
        s1, s2 = source(asset="aaaaaaaa-1", path="D:\\iso\\a.mxf"), source(asset="bbbbbbbb-2", path="D:\\iso\\b.mxf")
        _, root = self.build([item(s1, "01:00:00:00", "01:00:01:00", "1"), item(s2, "01:00:00:00", "01:00:01:00", "2"),
                              item(s1, "01:00:02:00", "01:00:03:00", "3")])
        files = [c.find("file") for c in video_clips(root)]
        self.assertEqual([f.get("id") for f in files], ["file-1", "file-2", "file-1"])
        self.assertEqual([len(f) > 0 for f in files], [True, True, False])
        self.assertEqual([tx(f, "name") for f in files[:2]], ["a.mxf", "b.mxf"])

    def test_audio_tracks_mirror_channel_count_and_link_to_video(self):
        for channels in (1, 2, 6):
            with self.subTest(channels=channels):
                src = source(channels=channels)
                _, root = self.build([item(src, "01:00:00:00", "01:00:01:00", "a"),
                                      item(src, "01:00:03:00", "01:00:05:00", "b")])
                tracks = audio_tracks(root)
                self.assertEqual(len(tracks), channels)
                for ch, track in enumerate(tracks, 1):
                    clips = track.findall("clipitem")
                    self.assertEqual([(tx(c, "in"), tx(c, "out"), tx(c, "start"), tx(c, "end")) for c in clips],
                                     [("0", "25", "0", "25"), ("75", "125", "25", "75")])
                    self.assertEqual({tx(c, "sourcetrack/trackindex") for c in clips}, {str(ch)})
                self.assertEqual(tx(root, "sequence/media/audio/format/samplecharacteristics/samplerate"), "48000")

    def test_ids_are_unique_and_every_reference_resolves(self):
        src = source(channels=2)
        _, root = self.build([item(src, "01:00:00:00", "01:00:01:00", "a"), item(src, "01:00:03:00", "01:00:05:00", "b")])
        clips = root.findall(".//clipitem")
        ids = [c.get("id") for c in clips]
        self.assertEqual(len(ids), 6)
        self.assertEqual(len(set(ids)), 6)
        for c in clips:
            links = c.findall("link")
            self.assertEqual(len(links), 3)                                # itself + two audio channels
            for link in links:
                self.assertIn(tx(link, "linkclipref"), ids)
            self.assertIn(c.get("id"), [tx(link, "linkclipref") for link in links])
        # a link names the clip at its stated track and index
        by_id = {c.get("id"): c for c in clips}
        for c in clips:
            for link in c.findall("link"):
                target = by_id[tx(link, "linkclipref")]
                kind, track, idx = tx(link, "mediatype"), int(tx(link, "trackindex")), int(tx(link, "clipindex"))
                parent_track = [t for t in (root.findall("./sequence/media/video/track")
                                            if kind == "video" else audio_tracks(root)) if target in list(t)]
                self.assertEqual(len(parent_track), 1)
                tracks = root.findall(f"./sequence/media/{kind}/track")
                self.assertIs(tracks[track - 1], parent_track[0])
                self.assertIs(tracks[track - 1].findall("clipitem")[idx - 1], target)

    def test_unknown_audio_layout_writes_no_audio_and_warns(self):
        built, root = self.build([item(source(channels=None), "01:00:00:00", "01:00:01:00")])
        self.assertEqual(audio_tracks(root), [])
        self.assertIsNone(root.find("./sequence/media/audio"))
        self.assertTrue(any("audio" in w for w in built.warnings))

    def test_clip_names_comments_and_span_id_return_path(self):
        built, root = self.build([item(source(), "01:00:00:00", "01:00:01:00", "sp-9", name="Wide & <tight>",
                                       text="steal the ball")])
        c = video_clips(root)[0]
        self.assertEqual(tx(c, "name"), "Wide & <tight>")                  # parsed back: escaping round-trips
        self.assertEqual(tx(c, "comments/mastercomment1"), "asset aaaaaaaa-1111 span sp-9")
        self.assertEqual(tx(c, "comments/mastercomment2"), "steal the ball")
        self.assertEqual(tx(c, "file/pathurl"), "file://localhost/D%3a/iso/cam1.mxf")

    def test_control_characters_and_double_dash_cannot_break_the_document(self):
        nasty = "bad\x00name\x08 -- end\x1f\r\n\ttab"
        built, root = self.build([item(source(), "01:00:00:00", "01:00:01:00", name=nasty, text=nasty)],
                                 name=nasty, skipped=["VOLT SKIPPED x -- (legal hold) \x00"])
        self.assertNotIn("\x00", built.content)
        self.assertEqual(tx(root, "sequence/name"), "badname -- end tab")

    def test_clip_children_order(self):
        _, root = self.build([item(source(), "01:00:00:00", "01:00:01:00")])
        tags = [e.tag for e in video_clips(root)[0]]
        self.assertEqual(tags[:9], ["masterclipid", "name", "enabled", "duration", "rate", "start", "end", "in", "out"])
        self.assertLess(tags.index("file"), tags.index("link"))

    def test_skipped_spans_are_named_in_a_comment(self):
        built, _ = self.build([item(source(), "01:00:00:00", "01:00:01:00")],
                              skipped=["VOLT SKIPPED abc (not cleared)"])
        self.assertIn("<!-- VOLT SKIPPED abc (not cleared) -->", built.content)

    def test_mixed_rates_and_empty_refused(self):
        a, b = source(R25, asset="a"), source(R30, asset="b")
        with self.assertRaises(nle.NleError) as e:
            nle.build_xmeml([item(a, "01:00:00:00", "01:00:01:00"), item(b, "01:00:00:00", "01:00:01:00")])
        self.assertEqual((e.exception.status, e.exception.code), (422, "mixed_rates"))
        with self.assertRaises(nle.NleError):
            nle.build_xmeml([])

    def test_unwritable_rate_refused(self):
        src = source(Fraction(15))                                         # whole number: fine
        nle.build_xmeml([item(src, "01:00:00:00", "01:00:01:00")])
        odd = nle.Source("k", "a", "D:\\x.mxf", Fraction(2997, 100), False, 0, None, 2)
        with self.assertRaises(nle.NleError) as e:
            nle.build_xmeml([nle.Item("s", odd, 0, 10, "n")])
        self.assertEqual(e.exception.code, "unsupported_rate")


class Edl(unittest.TestCase):
    LINE = re.compile(r"^(\d{3})  (\S{1,8}) +(V|A|AA)\s+C\s+(\d\d:\d\d:\d\d[:;]\d\d) (\d\d:\d\d:\d\d[:;]\d\d) "
                      r"(\d\d:\d\d:\d\d[:;]\d\d) (\d\d:\d\d:\d\d[:;]\d\d)$")

    def events(self, content):
        return [self.LINE.match(l) for l in content.splitlines() if re.match(r"^\d{3} ", l)]

    def test_format_and_labels(self):
        src = source(R25, channels=2, asset="1234abcd-9999")
        built = nle.build_edl([item(src, "01:00:02:00", "01:00:04:12", "a", name="Wide"),
                               item(src, "01:00:10:00", "01:00:10:01", "b")], name="My selects")
        lines = built.content.splitlines()
        self.assertEqual(lines[:2], ["TITLE: My selects", "FCM: NON-DROP FRAME"])
        ev = self.events(built.content)
        self.assertTrue(all(ev), built.content)
        self.assertEqual([e.group(1) for e in ev], ["001", "002", "003", "004"])
        self.assertEqual({e.group(2) for e in ev}, {"1234ABCD"})
        self.assertEqual([e.group(3) for e in ev], ["V", "AA", "V", "AA"])
        # source in/out are the span labels; record side is butted from 01:00:00:00
        self.assertEqual([e.group(4, 5, 6, 7) for e in ev[:2]], [("01:00:02:00", "01:00:04:12", "01:00:00:00", "01:00:02:12")] * 2)
        self.assertEqual(ev[2].group(4, 5, 6, 7), ("01:00:10:00", "01:00:10:01", "01:00:02:12", "01:00:02:13"))
        self.assertIn("* FROM CLIP NAME: Wide", lines)
        self.assertIn("* COMMENT: asset 1234abcd-9999 span a", lines)
        self.assertEqual(built.content[-1], "\n")

    def test_mono_audio_event_and_no_audio(self):
        mono = nle.build_edl([item(source(channels=1), "01:00:00:00", "01:00:01:00")])
        self.assertEqual([e.group(3) for e in self.events(mono.content)], ["V", "A"])
        none = nle.build_edl([item(source(channels=None), "01:00:00:00", "01:00:01:00")])
        self.assertEqual([e.group(3) for e in self.events(none.content)], ["V"])

    def test_more_than_two_channels_warns(self):
        built = nle.build_edl([item(source(channels=6), "01:00:00:00", "01:00:01:00")])
        self.assertTrue(any("channels 1 and 2" in w for w in built.warnings))

    def test_drop_frame_labels_and_fcm(self):
        src = source(R2997, "01:00:00;00", drop=True, channels=None)
        built = nle.build_edl([item(src, "01:00:59;29", "01:01:00;02", "a"),       # one frame
                               item(src, "01:10:00;00", "01:10:00;10", "b")])
        self.assertEqual(built.content.splitlines()[1], "FCM: DROP FRAME")
        ev = self.events(built.content)
        self.assertEqual(ev[0].group(4, 5, 6, 7), ("01:00:59;29", "01:01:00;02", "01:00:00;00", "01:00:00;01"))
        self.assertEqual(ev[1].group(4, 5, 6, 7), ("01:10:00;00", "01:10:00;10", "01:00:00;01", "01:00:00;11"))

    def test_reel_names_are_eight_characters_and_unique(self):
        a = source(asset="12345678-aaaa-bbbb", path="D:\\a.mxf")
        b = source(asset="12345678-cccc-dddd", path="D:\\b.mxf")             # same first 8 characters
        c = source(asset="ab-c", path="D:\\c.mxf")                          # short: not padded, not an error
        built = nle.build_edl([item(a, "01:00:00:00", "01:00:01:00", "1"), item(b, "01:00:00:00", "01:00:01:00", "2"),
                               item(c, "01:00:00:00", "01:00:01:00", "3")])
        reels = [e.group(2) for e in self.events(built.content) if e.group(3) == "V"]
        self.assertEqual(reels, ["12345678", "12345601", "ABC"])
        self.assertTrue(all(len(r) <= 8 for r in reels))
        self.assertTrue(any("reel" in w for w in built.warnings))

    def test_single_line_comments_and_title(self):
        built = nle.build_edl([item(source(), "01:00:00:00", "01:00:01:00", name="two\nlines\r\nhere")],
                              name="a\nb", skipped=["VOLT SKIPPED z (legal hold)"])
        self.assertIn("TITLE: a b", built.content)
        self.assertIn("* FROM CLIP NAME: two lines here", built.content)
        self.assertIn("* VOLT SKIPPED z (legal hold)", built.content)

    def test_mixed_rates_refused(self):
        with self.assertRaises(nle.NleError) as e:
            nle.build_edl([item(source(R25, asset="a"), "01:00:00:00", "01:00:01:00"),
                           item(source(R2997, "01:00:00;00", True, asset="b"), "01:00:00;00", "01:00:01;00")])
        self.assertEqual(e.exception.code, "mixed_rates")


class Helpers(unittest.TestCase):
    def test_parse_layout(self):
        for text, want in (("stereo", 2), ("Mono", 1), ("2", 2), ("8ch", 8), ("4 channels", 4), ("5.1", 6),
                           ("7.1", 8), ("quad", 4), (None, None), ("", None), ("weird", None), ("0", None),
                           ("33", None), ("2 + 2", None)):
            self.assertEqual(nle.parse_layout(text), want, text)

    def test_file_url(self):
        for path, url in (("D:\\iso\\a b\\c#1.mxf", "file://localhost/D%3a/iso/a%20b/c%231.mxf"),
                          ("d:/iso/x.mxf", "file://localhost/d%3a/iso/x.mxf"),
                          ("\\\\nas\\iso\\x.mxf", "file://localhost//nas/iso/x.mxf"),
                          ("/Volumes/cam 1/x.mov", "file://localhost/Volumes/cam%201/x.mov")):
            self.assertEqual(nle.file_url(path), url)
        for path in ("s3://bucket/x.mxf", "https://h/x.mxf", "relative/x.mxf", "x.mxf"):
            with self.assertRaises(nle.NleError) as e:
                nle.file_url(path)
            self.assertEqual((e.exception.status, e.exception.code), (409, "not_local"))

    def test_path_map(self):
        pairs = nle.parse_path_map("D:\\iso=\\\\nas\\iso; E:\\cam=/Volumes/cam;;")
        self.assertEqual(nle.map_path("D:\\iso\\show\\a.mxf", pairs), "\\\\nas\\iso\\show\\a.mxf")
        self.assertEqual(nle.map_path("d:/ISO/show/a.mxf", pairs), "\\\\nas\\iso\\show\\a.mxf")     # case, slash
        self.assertEqual(nle.map_path("E:\\cam\\a.mxf", pairs), "/Volumes/cam/a.mxf")
        self.assertEqual(nle.map_path("D:\\iso2\\a.mxf", pairs), "D:\\iso2\\a.mxf")              # segment boundary
        self.assertEqual(nle.map_path("F:\\other\\a.mxf", pairs), "F:\\other\\a.mxf")            # no match: as stored
        self.assertEqual(nle.map_path("D:\\iso\\a.mxf", []), "D:\\iso\\a.mxf")
        long_first = nle.parse_path_map("D:\\iso=X:\\a;D:\\iso\\deep=Y:\\b")
        self.assertEqual(nle.map_path("D:\\iso\\deep\\f.mxf", long_first), "Y:\\b\\f.mxf")       # longest prefix wins
        for bad in ("D:\\iso", "=x", "a=", "a=b;nonsense"):
            with self.assertRaises(nle.NleError) as e:
                nle.parse_path_map(bad)
            self.assertEqual((e.exception.status, e.exception.code), (500, "bad_path_map"))

    def test_filename(self):
        self.assertEqual(nle.filename("Final Four / Watch: selects", "xml"), "Final_Four_Watch_selects.xml")
        self.assertEqual(nle.filename("...", "edl"), "volt-selects.edl")


# ---- the route ------------------------------------------------------------------------------------

class Behaviour:
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store = self.open_store()
        self.addCleanup(self.store.close)
        self._saved = (serve.DB_PATH, serve.TOKEN, serve.OUT_DIR, nle.LOG, os.environ.get("VOLT_EXPORT_PATH_MAP"))
        self.logged = []
        serve.DB_PATH, serve.TOKEN, serve.OUT_DIR, nle.LOG = self.target, None, self.dir / "out", self.logged.append
        os.environ.pop("VOLT_EXPORT_PATH_MAP", None)
        self.addCleanup(self._restore)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)       # port 0: the OS picks a free one
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)

    def _restore(self):
        serve.DB_PATH, serve.TOKEN, serve.OUT_DIR, nle.LOG, old = self._saved
        if old is None:
            os.environ.pop("VOLT_EXPORT_PATH_MAP", None)
        else:
            os.environ["VOLT_EXPORT_PATH_MAP"] = old

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def post(self, body, token=None, raw=None):
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=30)
        try:
            conn.request("POST", "/api/exports/nle", body=raw if raw is not None else json.dumps(body), headers=h)
            r = conn.getresponse()
            return r.status, json.loads(r.read())
        finally:
            conn.close()

    def refused(self, status, code, body, **kw):
        s, p = self.post(body, **kw)
        self.assertEqual((s, p["ok"], p["error"]["code"]), (status, False, code), p)
        return p

    def asset(self, rate="25", start="01:00:00:00", duration=2500, layout="stereo", proxy=False, cleared=True,
              hold=False, hi_open=False, camera="iso-3", location=None, proxy_kw=None):
        a = self.store.mint_asset(owner="desk", camera=camera)
        self.store.add_essence(a.id, role="hi-res", location=location or str(self.dir / "hi.mxf"), frame_rate=rate,
                               timecode_start=start, duration_frames=duration, audio_layout=layout, open_file=hi_open)
        if proxy:
            self.store.add_essence(a.id, role="proxy", location=str(self.dir / "px.mp4"), **(proxy_kw or {}))
        self.store.set_cleared(a.id, cleared)
        self.store.set_hold(a.id, hold)
        return a

    def span(self, a, tc_in, tc_out, kind="shot", **kw):
        return shots.create(self.store, a.id, kind, tc_in, tc_out, **kw)["id"]

    def body(self, spans, **kw):
        return {"format": "xmeml", "spans": spans, **kw}


class Export(Behaviour):
    def test_xmeml_three_spans_at_2997_drop_frame(self):
        a = self.asset(rate="30000/1001", start="01:00:00;00", layout="stereo", duration=20000)
        s1 = self.span(a, "01:00:10;00", "01:00:12;15", label="Wide")
        s2 = self.span(a, "01:01:00;02", "01:01:05;00")
        s3 = self.span(a, "01:10:00;00", "01:10:00;10", kind="clip", label="Reaction")
        s, p = self.post(self.body([s1, s2, s3], name="Final Four selects"))
        self.assertEqual(s, 200, p)
        self.assertEqual((p["ok"], p["format"], p["mime"], p["filename"]),
                         (True, "xmeml", "application/xml", "Final_Four_selects.xml"))
        self.assertEqual((p["rate"], p["durationFrames"], p["startTc"], p["dropFrame"]),
                         ("30000/1001", 233, "01:00:00;00", True))
        self.assertEqual(p["sha256"], hashlib.sha256(p["content"].encode()).hexdigest())
        self.assertEqual([x["status"] for x in p["spans"]], ["ready"] * 3)
        root = parse_xmeml(p["content"])
        got = [(tx(c, "name"), tx(c, "in"), tx(c, "out"), tx(c, "start"), tx(c, "end")) for c in video_clips(root)]
        self.assertEqual(got, [("Wide", "300", "375", "0", "75"), ("iso-3 01:01:00;02", "1800", "1948", "75", "223"),
                               ("Reaction", "17982", "17992", "223", "233")])
        self.assertEqual(len(audio_tracks(root)), 2)
        self.assertEqual(self.logged, [f"nle export xmeml spans={s1},{s2},{s3} sha256={p['sha256']}"])
        # stateless: the call changed nothing
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) AS n FROM span").fetchone()["n"], 3)

    def test_edl_matches_the_span_labels(self):
        a = self.asset(layout="mono")
        s1 = self.span(a, "01:00:02:00", "01:00:04:12", label="Wide")
        s2 = self.span(a, "01:00:10:00", "01:00:10:01")
        s, p = self.post({"format": "edl", "spans": [s2, s1], "start_tc": "10:00:00:00"})
        self.assertEqual((s, p["mime"], p["filename"]), (200, "text/plain", "Volt_selects.edl"))
        lines = p["content"].splitlines()
        events = [l for l in lines if re.match(r"^\d{3} ", l)]
        self.assertEqual(len(events), 4)                                    # V and A per span, in the order sent
        self.assertTrue(events[0].endswith("01:00:10:00 01:00:10:01 10:00:00:00 10:00:00:01"))
        self.assertTrue(events[2].endswith("01:00:02:00 01:00:04:12 10:00:00:01 10:00:02:13"))
        self.assertEqual(events[0].split()[1], a.id.replace("-", "").upper()[:8])

    def test_order_sent_is_order_written_and_a_shot_may_repeat(self):
        a = self.asset()
        s1, s2 = self.span(a, "01:00:00:00", "01:00:01:00"), self.span(a, "01:00:01:00", "01:00:03:00")
        _, p = self.post(self.body([s2, s1, s2]))
        root = parse_xmeml(p["content"])
        self.assertEqual([(tx(c, "in"), tx(c, "start")) for c in video_clips(root)], [("25", "0"), ("0", "50"), ("25", "75")])
        self.assertEqual(p["durationFrames"], 125)

    def test_not_cleared_and_legal_hold_block(self):
        ok = self.asset()
        nc = self.asset(cleared=False)
        lh = self.asset(hold=True)
        s_ok, s_nc, s_lh = (self.span(x, "01:00:00:00", "01:00:01:00") for x in (ok, nc, lh))
        p = self.refused(409, "blocked", self.body([s_ok, s_nc, s_lh]))
        self.assertEqual([(x["id"], x["status"], x.get("blockedBy")) for x in p["spans"]],
                         [(s_ok, "ready", None), (s_nc, "blocked", "not cleared"), (s_lh, "blocked", "legal hold")])
        self.assertEqual(self.logged, [])
        # skip_blocked leaves them out, names them in the file and in warnings
        s, p = self.post(self.body([s_ok, s_nc, s_lh], skip_blocked=True))
        self.assertEqual(s, 200, p)
        self.assertEqual(len(video_clips(parse_xmeml(p["content"]))), 1)
        self.assertIn(f"VOLT SKIPPED", p["content"])
        self.assertIn(s_nc, p["content"])
        self.assertTrue(any("left out" in w for w in p["warnings"]))
        # every span blocked: nothing to export, still 409 blocked
        p = self.refused(409, "blocked", self.body([s_nc, s_lh], skip_blocked=True))
        self.assertEqual([x["status"] for x in p["spans"]], ["blocked", "blocked"])

    def test_dry_run_gives_verdicts_and_preview_but_no_file(self):
        ok, nc = self.asset(), self.asset(cleared=False)
        s_ok, s_nc = self.span(ok, "01:00:00:00", "01:00:01:00"), self.span(nc, "01:00:00:00", "01:00:01:00")
        s, p = self.post(self.body([s_ok, s_nc], dry_run=True))
        self.assertEqual(s, 200, p)
        self.assertTrue(p["dryRun"])
        self.assertNotIn("content", p)
        self.assertEqual([x["status"] for x in p["spans"]], ["ready", "blocked"])
        self.assertEqual(p["preview"][0], '<?xml version="1.0" encoding="UTF-8"?>')
        self.assertLessEqual(len(p["preview"]), nle.PREVIEW_LINES)
        self.assertEqual(self.logged, [])
        s, p = self.post(self.body([s_nc], dry_run=True))                   # nothing ready: still a verdict
        self.assertEqual((s, p["preview"], p["spans"][0]["status"]), (200, [], "blocked"))

    def test_not_found(self):
        a = self.asset()
        s1 = self.span(a, "01:00:00:00", "01:00:01:00")
        self.refused(404, "not_found", self.body([s1, "no-such-span"]))

    def test_open_essence_refused_proxy_export_is_the_way_out(self):
        a = self.asset(hi_open=True, proxy=True)
        s1 = self.span(a, "01:00:00:00", "01:00:01:00")
        self.refused(409, "essence_open", self.body([s1]))
        s, p = self.post(self.body([s1], media="proxy"))
        self.assertEqual(s, 200, p)
        self.assertEqual(tx(video_clips(parse_xmeml(p["content"]))[0], "file/name"), "px.mp4")
        b = self.asset(proxy=True, proxy_kw={"open_file": True})
        self.refused(409, "essence_open", self.body([self.span(b, "01:00:00:00", "01:00:01:00")], media="proxy"))

    def test_no_proxy(self):
        a = self.asset()
        self.refused(409, "no_proxy", self.body([self.span(a, "01:00:00:00", "01:00:01:00")], media="proxy"))

    def test_proxy_that_disagrees_with_the_hi_res_is_refused(self):
        a = self.asset(proxy=True, proxy_kw={"frame_rate": "30"})
        self.refused(409, "proxy_mismatch", self.body([self.span(a, "01:00:00:00", "01:00:01:00")], media="proxy"))
        b = self.asset(proxy=True, proxy_kw={"timecode_start": "00:00:00:00"})
        self.refused(409, "proxy_mismatch", self.body([self.span(b, "01:00:00:00", "01:00:01:00")], media="proxy"))
        c = self.asset(proxy=True, proxy_kw={"frame_rate": "25", "timecode_start": "01:00:00:00"})
        s, _ = self.post(self.body([self.span(c, "01:00:00:00", "01:00:01:00")], media="proxy"))
        self.assertEqual(s, 200)

    def test_not_local_media(self):
        a = self.asset(location="s3://bucket/cam1.mxf")
        self.refused(409, "not_local", self.body([self.span(a, "01:00:00:00", "01:00:01:00")]))

    def test_mixed_rates(self):
        a, b = self.asset(rate="25"), self.asset(rate="30000/1001", start="01:00:00;00")
        p = self.refused(422, "mixed_rates", self.body([self.span(a, "01:00:00:00", "01:00:01:00"),
                                                        self.span(b, "01:00:00;00", "01:00:01;00")]))
        self.assertEqual(p["rates"], ["25", "30000/1001"])

    def test_blocked_spans_do_not_count_toward_mixed_rates_when_skipped(self):
        a, b = self.asset(rate="25"), self.asset(rate="30000/1001", start="01:00:00;00", cleared=False)
        s, _ = self.post(self.body([self.span(a, "01:00:00:00", "01:00:01:00"),
                                    self.span(b, "01:00:00;00", "01:00:01;00")], skip_blocked=True))
        self.assertEqual(s, 200)

    def test_span_outside_its_essence_is_refused(self):
        a = self.asset(duration=100)
        s1 = self.span(a, "01:00:02:00", "01:00:03:00")
        self.store.conn.execute("UPDATE essence SET duration_frames = 60 WHERE asset_id = ?", (a.id,))
        self.store.conn.commit()
        self.refused(409, "outside_source", self.body([s1]))

    def test_asset_without_a_timebase_is_refused_not_guessed(self):
        a = self.asset()
        s1 = self.span(a, "01:00:00:00", "01:00:01:00")
        self.store.conn.execute("UPDATE essence SET frame_rate = NULL WHERE asset_id = ?", (a.id,))
        self.store.conn.commit()
        self.refused(422, "bad_input", self.body([s1]))

    def test_span_without_frame_columns_is_placed_from_its_labels(self):
        a = self.asset()
        s1 = self.span(a, "01:00:02:00", "01:00:03:00")
        self.store.conn.execute("UPDATE span SET frame_in = NULL, frame_out = NULL WHERE id = ?", (s1,))
        self.store.conn.commit()
        _, p = self.post(self.body([s1]))
        c = video_clips(parse_xmeml(p["content"]))[0]
        self.assertEqual((tx(c, "in"), tx(c, "out")), ("50", "75"))

    def test_path_map_from_the_environment(self):
        a = self.asset(location="D:\\iso\\show\\cam1.mxf")
        s1 = self.span(a, "01:00:00:00", "01:00:01:00")
        _, p = self.post(self.body([s1]))
        self.assertEqual(tx(video_clips(parse_xmeml(p["content"]))[0], "file/pathurl"),
                         "file://localhost/D%3a/iso/show/cam1.mxf")
        os.environ["VOLT_EXPORT_PATH_MAP"] = "D:\\iso=\\\\nas\\iso"
        _, p = self.post(self.body([s1]))
        self.assertEqual(tx(video_clips(parse_xmeml(p["content"]))[0], "file/pathurl"),
                         "file://localhost//nas/iso/show/cam1.mxf")
        os.environ["VOLT_EXPORT_PATH_MAP"] = "garbage"
        self.refused(500, "bad_path_map", self.body([s1]))

    def test_bad_input(self):
        a = self.asset()
        s1 = self.span(a, "01:00:00:00", "01:00:01:00")
        for body in ({}, {"format": "edl"}, {"format": "aaf", "spans": [s1]}, {"format": "edl", "spans": []},
                     {"format": "edl", "spans": [s1, 7]}, {"format": "edl", "spans": s1},
                     {"format": "edl", "spans": [""]}, {"format": "edl", "spans": [s1] * 501},
                     self.body([s1], media="tape"), self.body([s1], name=""), self.body([s1], name=7),
                     self.body([s1], name="x" * 201), self.body([s1], start_tc="not a tc"),
                     self.body([s1], start_tc="01:00:00;00"), self.body([s1], start_tc=5),
                     self.body([s1], dry_run="yes"), self.body([s1], skip_blocked=1), self.body([s1], extra=1)):
            self.refused(422, "bad_input", body)
        s, p = self.post(self.body([s1] * 500))                              # exactly the limit is fine
        self.assertEqual((s, p["durationFrames"]), (200, 12500))

    def test_json_contract_and_auth(self):
        a = self.asset()
        s1 = self.span(a, "01:00:00:00", "01:00:01:00")
        self.refused(422, "bad_input", None, raw="[1]")
        self.refused(422, "bad_input", None, raw="{not json")
        serve.TOKEN = "t" * 24
        self.refused(401, "unauthorized", self.body([s1]))
        s, _ = self.post(self.body([s1]), token="t" * 24)
        self.assertEqual(s, 200)

    def test_proxy_export_reuses_hi_res_timebase_and_audio_layout(self):
        a = self.asset(layout="stereo", proxy=True)
        s1 = self.span(a, "01:00:01:00", "01:00:02:00")
        _, p = self.post(self.body([s1], media="proxy"))
        root = parse_xmeml(p["content"])
        c = video_clips(root)[0]
        self.assertEqual((tx(c, "in"), tx(c, "out"), tx(c, "file/duration")), ("25", "50", "2500"))
        self.assertEqual(len(audio_tracks(root)), 2)

    def test_default_name_for_unlabelled_span_is_camera_and_in_point(self):
        a = self.asset(camera="iso-7")
        s1 = self.span(a, "01:00:03:05", "01:00:04:00")
        _, p = self.post(self.body([s1]))
        self.assertEqual(tx(video_clips(parse_xmeml(p["content"]))[0], "name"), "iso-7 01:00:03:05")


ALL = (Export,)


class _Sqlite:
    def open_store(self):
        self.target = str(self.dir / "v.db")
        return Store(self.target)


class _Pg:
    def open_store(self):
        self.target = self.dsn
        return self.store()


def _build():
    g = globals()
    for base in ALL:
        g[f"Sqlite{base.__name__}"] = type(f"Sqlite{base.__name__}", (_Sqlite, base, unittest.TestCase), {})
        g[f"Postgres{base.__name__}"] = type(f"Postgres{base.__name__}", (_Pg, base, PgCase), {})


_build()
del Behaviour, Export

if __name__ == "__main__":
    unittest.main()
