"""MOS export of the rundown (docs/specs/mos.md): a roCreate message built from a show.

The format is structurally modelled on MOS 2.8.5 roCreate as documented. It is NOT validated against the MOS
XSD/DTD or any newsroom system (the full DTD was not obtainable), and no test here claims otherwise. What is
tested: exact output (golden file), well-formedness, escaping, element order against the DTD lines that were
readable, and the frame/second arithmetic. The API half runs on SQLite always and on Postgres with VOLT_TEST_PG_DSN."""

import hashlib
import http.client
import io
import json
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout, redirect_stderr
from fractions import Fraction
from http.server import ThreadingHTTPServer
from pathlib import Path

from volt import building, cli, mos, serve, shots
from volt.cues import add_cue
from volt.rundown import attach
from volt.store import Store

from tests.pgutil import PgCase

GOLDEN = Path(__file__).parent / "golden" / "mos_ro_create.xml"
R25 = Fraction(25)


def media(obj="span-1", slug="Wide", rate=R25, frames=250, tc_in="01:00:10:00", tc_out="01:00:20:00"):
    return mos.MediaRef(obj, slug, rate, frames, tc_in, tc_out)


def story(i=1, slug="S", status="ready", script="", m=None, cues=()):
    return mos.Story(f"item-{i}", i, slug, status, script, m, tuple(cues))


def build(stories, title="Show", **kw):
    kw.setdefault("mos_id", "volt.mos")
    kw.setdefault("ncs_id", "ncs.test")
    return mos.build_ro_create("show-1", title, stories, **kw)


def parse(built):
    return ET.fromstring(built.content.encode("utf-8"))


def local(tag):
    return tag.rsplit("}", 1)[-1]


class Golden(unittest.TestCase):
    def stories(self):
        return [
            story(1, "Cold open", script="Welcome back.\nSecond line.", m=media()),
            story(2, "Bumper", status="killed"),
            story(3, "Close", cues=[mos.CueRef("corevideo.pro", "overlay.take", "lower-third", "armed")]),
        ]

    def test_matches_the_golden_file(self):
        built = build(self.stories(), "Friday Night", include_cues=True)
        self.assertEqual(built.content, GOLDEN.read_text(encoding="utf-8"))

    def test_cues_are_left_out_by_default(self):
        built = build(self.stories(), "Friday Night")
        self.assertNotIn("overlay.take", built.content)
        self.assertNotIn("cue", built.content)

    def test_same_input_same_bytes(self):
        a, b = build(self.stories()), build(self.stories())
        self.assertEqual(a.content, b.content)


class Structure(unittest.TestCase):
    def test_element_order_follows_the_readable_dtd_lines(self):
        root = parse(build([story(1, "A", m=media())]))
        self.assertEqual([c.tag for c in root], ["mosID", "ncsID", "messageID", "roCreate"])
        ro = root.find("roCreate")
        self.assertEqual([c.tag for c in ro], ["roID", "roSlug", "roEdDur", "story"])
        st = ro.find("story")
        self.assertEqual([c.tag for c in st], ["storyID", "storySlug", "storyNum", "mosExternalMetadata", "item"])
        item = st.find("item")
        self.assertEqual([c.tag for c in item],
                         ["itemID", "itemSlug", "objID", "mosID", "itemEdStart", "itemEdDur", "mosExternalMetadata"])

    def test_empty_show(self):
        built = build([], "Nothing yet")
        root = parse(built)
        ro = root.find("roCreate")
        self.assertEqual([c.tag for c in ro], ["roID", "roSlug", "roEdDur"])
        self.assertEqual(ro.findtext("roEdDur"), "00:00:00")
        self.assertEqual((built.story_count, built.item_count, built.total_seconds), (0, 0, 0))

    def test_items_without_media_are_stories_with_no_item(self):
        root = parse(build([story(1, "A"), story(2, "B")]))
        stories = root.findall("roCreate/story")
        self.assertEqual(len(stories), 2)
        self.assertEqual([s.find("item") for s in stories], [None, None])
        self.assertEqual(root.findtext("roCreate/roEdDur"), "00:00:00")

    def test_ids_and_message_id(self):
        root = parse(build([], message_id=42, mos_id="a.b", ncs_id="c-d"))
        self.assertEqual((root.findtext("mosID"), root.findtext("ncsID"), root.findtext("messageID")),
                         ("a.b", "c-d", "42"))

    def test_bad_ids_are_refused(self):
        for bad in ("", "has space", "<x>", "a" * 129):
            with self.assertRaises(mos.MosError):
                build([], mos_id=bad)
            with self.assertRaises(mos.MosError):
                build([], ncs_id=bad)


class Escaping(unittest.TestCase):
    HOSTILE = [
        'Tom & Jerry <b>"quoted"</b> \'single\'',
        "</storySlug><injected/>",
        "]]> & &amp; &#x41;",
        "bell\x07 nul\x00 esc\x1b vt\x0b ff\x0c",
        "line\r\nbreak\ttab",
        "emoji \U0001F3AC and café",
        "lone surrogate \ud800 end",
        "￾￿",
    ]

    def test_hostile_slugs_stay_well_formed_and_round_trip(self):
        for slug in self.HOSTILE:
            with self.subTest(slug=slug):
                built = build([story(1, slug, script=slug, m=media(slug=slug))], title=slug)
                root = parse(built)                                  # raises if not well-formed
                self.assertEqual(len(root.findall("roCreate/story")), 1)
                self.assertIsNone(root.find("roCreate/injected"))
                self.assertIsNone(root.find("roCreate/story/injected"))
                self.assertEqual(len(root.findall("roCreate/story/item")), 1)
                for bad in "\x00\x07\x0b\x0c\x1b￾￿":
                    self.assertNotIn(bad, built.content)
                got = root.findtext("roCreate/roSlug")
                self.assertEqual(got, mos.clean_text(slug))

    def test_specific_escapes(self):
        built = build([story(1, 'A & B <c> "d"')], title="x&y")
        self.assertIn("<storySlug>A &amp; B &lt;c&gt; \"d\"</storySlug>", built.content)
        self.assertIn("<roSlug>x&amp;y</roSlug>", built.content)

    def test_control_chars_are_dropped_not_escaped(self):
        self.assertEqual(mos.clean_text("a\x00b\x1bc"), "abc")
        self.assertNotIn("&#", build([story(1, "a\x00b")]).content)

    def test_script_keeps_newlines_but_slug_folds_them(self):
        root = parse(build([story(1, "two\nlines", script="one\ntwo\r\nthree")]))
        self.assertEqual(root.findtext("roCreate/story/storySlug"), "two lines")
        script = [e for e in root.iter() if local(e.tag) == "script"][0].text
        self.assertEqual(script, "one\ntwo\nthree")

    def test_ids_with_markup_are_escaped_too(self):
        root = parse(build([mos.Story("i<d>&", 1, "s", "ready", "", media(obj="o\"<&"), ())]))
        self.assertEqual(root.findtext("roCreate/story/storyID"), "i<d>&")
        self.assertEqual(root.findtext("roCreate/story/item/objID"), 'o"<&')


class Arithmetic(unittest.TestCase):
    # (rate text, rate Fraction, objTB text, one hour in frames)
    CASES = [
        ("25", Fraction(25), "25", 90000),
        ("29.97", Fraction(30000, 1001), "29.97", 107892),
        ("30", Fraction(30), "30", 108000),
        ("50", Fraction(50), "50", 180000),
        ("59.94", Fraction(60000, 1001), "59.94", 215784),
        ("60", Fraction(60), "60", 216000),
    ]

    def test_timebase_text_and_duration_per_rate(self):
        for name, rate, tb, hour in self.CASES:
            with self.subTest(rate=name):
                built = build([story(1, "A", m=media(rate=rate, frames=hour))])
                root = parse(built)
                payload = {local(e.tag): e.text for e in root.iter() if local(e.tag) in ("objTB", "objDur")}
                self.assertEqual(payload, {"objTB": tb, "objDur": str(hour)})
                self.assertEqual(root.findtext("roCreate/story/item/itemEdDur"), str(hour))
                self.assertEqual(root.findtext("roCreate/roEdDur"), "01:00:00")
                self.assertEqual(built.total_seconds, 3600)

    def test_ten_seconds_is_the_nominal_frame_count_of_whole_rates(self):
        for rate, frames in ((25, 250), (30, 300), (50, 500), (60, 600)):
            built = build([story(1, "A", m=media(rate=Fraction(rate), frames=frames))])
            self.assertEqual(built.total_seconds, 10)
            self.assertEqual(parse(built).findtext("roCreate/roEdDur"), "00:00:10")

    def test_ntsc_rates_are_exact_not_nominal(self):
        # 300 frames at 29.97 is 10.01 s, not 10 s; 600 at 59.94 likewise. Rounded to whole seconds in roEdDur.
        built = build([story(1, "A", m=media(rate=Fraction(30000, 1001), frames=300))])
        self.assertEqual(built.total_seconds_exact, Fraction(300 * 1001, 30000))
        self.assertEqual(built.total_seconds, 10)
        # 1800 frames at 29.97 is 60.06 s: a minute, then rounded
        built = build([story(1, "A", m=media(rate=Fraction(30000, 1001), frames=1800))])
        self.assertEqual(parse(built).findtext("roCreate/roEdDur"), "00:01:00")

    def test_mixed_rates_sum_exact_seconds_once(self):
        # 29.97 for 1 s-ish twice: rounding each would give 1+1; summing exactly gives 2.002 -> 2. Choose a case
        # where per-item rounding and summing differ: 0.5 s + 0.5 s at 25 fps = 12.5 frames each is not whole, so
        # use 15 frames at 30 fps (0.5 s) x 3 = 1.5 s -> rounds half up to 2, per-item rounding would give 3.
        ms = [story(i, "A", m=media(f"s{i}", rate=Fraction(30), frames=15)) for i in (1, 2, 3)]
        built = build(ms)
        self.assertEqual(built.total_seconds_exact, Fraction(3, 2))
        self.assertEqual(built.total_seconds, 2)

    def test_hours_may_pass_99(self):
        built = build([story(1, "A", m=media(rate=Fraction(25), frames=25 * 3600 * 100))])
        self.assertEqual(parse(built).findtext("roCreate/roEdDur"), "100:00:00")

    def test_item_with_no_rate_is_listed_without_a_duration_and_warns(self):
        built = build([story(1, "A", m=mos.MediaRef("span-1", "Wide", None, None, "01:00:10:00", "01:00:20:00"))])
        root = parse(built)
        item = root.find("roCreate/story/item")
        self.assertIsNone(item.find("itemEdDur"))
        self.assertEqual(root.findtext("roCreate/roEdDur"), "00:00:00")
        self.assertEqual(len(built.warnings), 1)
        self.assertIn("item-1", built.warnings[0])

    def test_timebase_text_helper(self):
        self.assertEqual(mos.timebase_text(Fraction(24000, 1001)), "23.976")
        self.assertEqual(mos.timebase_text(Fraction(24)), "24")
        self.assertEqual(mos.timebase_text(Fraction(120000, 1001)), "119.88")

    def test_hms(self):
        self.assertEqual(mos.hms(0), "00:00:00")
        self.assertEqual(mos.hms(3599), "00:59:59")
        self.assertEqual(mos.hms(3600), "01:00:00")


# ---- from a database, the route and the CLI -------------------------------------------------------------------

class Behaviour:
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store = self.open_store()
        self.addCleanup(self.store.close)
        self._saved = (serve.DB_PATH, serve.TOKEN, serve.OUT_DIR)
        serve.DB_PATH, serve.TOKEN, serve.OUT_DIR = self.target, None, self.dir / "out"
        self.addCleanup(self._restore)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)

    def _restore(self):
        serve.DB_PATH, serve.TOKEN, serve.OUT_DIR = self._saved

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def get(self, path, token=None):
        h = {"Authorization": f"Bearer {token}"} if token else {}
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=30)
        try:
            conn.request("GET", path, headers=h)
            r = conn.getresponse()
            return r.status, json.loads(r.read())
        finally:
            conn.close()

    def post(self, path, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=30)
        try:
            conn.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json"})
            r = conn.getresponse()
            return r.status, json.loads(r.read())
        finally:
            conn.close()

    def span(self, rate="25", start="01:00:00:00", tc_in="01:00:10:00", tc_out="01:00:20:00", label="Wide"):
        a = self.store.mint_asset(owner="desk", camera="iso-1")
        self.store.add_essence(a.id, role="hi-res", location=str(self.dir / "hi.mxf"), frame_rate=rate,
                               timecode_start=start, duration_frames=500000, audio_layout="stereo")
        return shots.create(self.store, a.id, "clip", tc_in, tc_out, label=label)["id"]

    def make_show(self):
        show, _ = building.create_show(self.store, "Friday Night", "show-1")
        i1, _ = building.add_item(self.store, show, "Cold open", "Welcome\nback", "item-1")
        i2, _ = building.add_item(self.store, show, "Bumper", "", "item-2")
        i3, _ = building.add_item(self.store, show, "Close & out", "", "item-3")
        sid = self.span()
        attach(self.store, i1, sid)
        add_cue(self.store, i3, "corevideo.pro", "overlay.take", "lt")
        return show, sid


class FromDatabase(Behaviour):
    def test_export_reads_items_in_order_with_span_durations(self):
        show, sid = self.make_show()
        out = mos.export(self.store, show)
        self.assertTrue(out["ok"])
        root = ET.fromstring(out["content"].encode())
        self.assertEqual([e.text for e in root.findall("roCreate/story/storyID")], ["item-1", "item-2", "item-3"])
        self.assertEqual(root.findtext("roCreate/roID"), "show-1")
        self.assertEqual(root.findtext("roCreate/roSlug"), "Friday Night")
        item = root.find("roCreate/story/item")
        self.assertEqual((item.findtext("objID"), item.findtext("itemEdDur"), item.findtext("itemSlug")),
                         (sid, "250", "Wide"))
        self.assertEqual(root.findtext("roCreate/roEdDur"), "00:00:10")
        self.assertEqual(out["sha256"], hashlib.sha256(out["content"].encode()).hexdigest())
        self.assertEqual((out["stories"], out["items"], out["durationSeconds"], out["validated"]), (3, 1, 10, False))
        self.assertNotIn("overlay.take", out["content"])

    def test_cues_option(self):
        show, _ = self.make_show()
        out = mos.export(self.store, show, include_cues=True)
        self.assertIn("overlay.take", out["content"])

    def test_span_without_stored_frames_falls_back_to_timecode(self):
        show, sid = self.make_show()
        self.store.conn.execute("UPDATE span SET frame_in = NULL, frame_out = NULL WHERE id = ?", (sid,))
        self.store.conn.commit()
        root = ET.fromstring(mos.export(self.store, show)["content"].encode())
        self.assertEqual(root.findtext("roCreate/story/item/itemEdDur"), "250")

    def test_drop_frame_essence(self):
        show, _ = building.create_show(self.store, "NTSC", "show-n")
        item, _ = building.add_item(self.store, show, "A", "", "item-n")
        sid = self.span(rate="30000/1001", start="01:00:00;00", tc_in="01:00:00;02", tc_out="01:00:10;02")
        attach(self.store, item, sid)
        out = mos.export(self.store, show)
        root = ET.fromstring(out["content"].encode())
        self.assertEqual(root.findtext("roCreate/story/item/itemEdDur"), "300")
        self.assertEqual([e.text for e in root.iter() if local(e.tag) == "objTB"], ["29.97"])
        self.assertEqual(root.findtext("roCreate/roEdDur"), "00:00:10")

    def test_unknown_show(self):
        with self.assertRaises(mos.MosError) as cm:
            mos.export(self.store, "nope")
        self.assertEqual((cm.exception.status, cm.exception.code), (404, "not_found"))

    def test_export_writes_nothing(self):
        show, _ = self.make_show()
        before = [self.store.conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
                  for t in ("show", "item", "span", "cue", "audit")]
        mos.export(self.store, show)
        after = [self.store.conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
                 for t in ("show", "item", "span", "cue", "audit")]
        self.assertEqual(before, after)


class Route(Behaviour):
    def test_get_route(self):
        show, sid = self.make_show()
        s, p = self.get(f"/api/shows/{show}/mos")
        self.assertEqual(s, 200, p)
        self.assertEqual((p["format"], p["mime"], p["filename"]), ("mos-roCreate", "application/xml", "show-1.mos.xml"))
        self.assertTrue(p["content"].startswith('<?xml version="1.0" encoding="UTF-8"?>'))
        self.assertIs(p["validated"], False)
        ET.fromstring(p["content"].encode())

    def test_query_options(self):
        show, _ = self.make_show()
        s, p = self.get(f"/api/shows/{show}/mos?cues=1&mos_id=my.mos&ncs_id=my.ncs")
        self.assertEqual(s, 200, p)
        self.assertIn("<mosID>my.mos</mosID>", p["content"])
        self.assertIn("<ncsID>my.ncs</ncsID>", p["content"])
        self.assertIn("overlay.take", p["content"])

    def test_bad_query(self):
        show, _ = self.make_show()
        for q in ("mos_id=has%20space", "cues=maybe", "bogus=1", "cues=1&cues=0"):
            s, p = self.get(f"/api/shows/{show}/mos?{q}")
            self.assertEqual((s, p["error"]["code"]), (422, "bad_input"), q)

    def test_unknown_show_404(self):
        s, p = self.get("/api/shows/nope/mos")
        self.assertEqual((s, p["error"]["code"]), (404, "not_found"))

    def test_requires_the_token_when_one_is_set(self):
        show, _ = self.make_show()
        serve.TOKEN = "t" * 24
        s, _ = self.get(f"/api/shows/{show}/mos")
        self.assertEqual(s, 401)
        s, p = self.get(f"/api/shows/{show}/mos", token="t" * 24)
        self.assertEqual(s, 200, p)

    def test_is_read_only_and_not_a_post_route(self):
        show, _ = self.make_show()
        s, p = self.post(f"/api/shows/{show}/mos", {})
        self.assertEqual(s, 404)
        before = self.store.conn.execute("SELECT COUNT(*) AS n FROM audit").fetchone()["n"]
        self.get(f"/api/shows/{show}/mos")
        self.assertEqual(before, self.store.conn.execute("SELECT COUNT(*) AS n FROM audit").fetchone()["n"])


class Cli(Behaviour):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["mos", "export", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_stdout(self):
        show, _ = self.make_show()
        code, out, _ = self.run_cli(show, "--db", self.target)
        self.assertEqual(code, 0)
        ET.fromstring(out.encode())
        self.assertIn("<roID>show-1</roID>", out)

    def test_out_file_and_title_lookup(self):
        self.make_show()
        dest = self.dir / "ro.xml"
        code, out, _ = self.run_cli("Friday Night", "--db", self.target, "--out", str(dest), "--cues")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        text = dest.read_bytes().decode("utf-8")
        self.assertIn("overlay.take", text)
        self.assertNotIn("\r\n", text)
        ET.fromstring(text.encode())

    def test_unknown_show_exits_nonzero(self):
        code, _, err = self.run_cli("nope", "--db", self.target)
        self.assertEqual(code, 1)
        self.assertIn("no such show", err)

    def test_db_is_required(self):
        import os
        old = os.environ.pop("VOLT_DB", None)
        try:
            with self.assertRaises(SystemExit):
                self.run_cli("x")
        finally:
            if old is not None:
                os.environ["VOLT_DB"] = old


ALL = (FromDatabase, Route, Cli)


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
del Behaviour, FromDatabase, Route, Cli

if __name__ == "__main__":
    unittest.main()
